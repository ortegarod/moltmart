"""
MoltMart Backend - Service Registry for AI Agents
x402-native marketplace for agent services
"""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import uuid
from collections import defaultdict
from datetime import datetime

import httpx
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, HttpUrl, validator

# Rate limiting
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from x402.http import FacilitatorConfig, HTTPFacilitatorClient, PaymentOption
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http.types import RouteConfig
from x402.mechanisms.evm.exact import ExactEvmServerScheme

# x402 payment protocol
from x402.server import x402ResourceServer

# Database
from database import (
    AgentDB,
    ServiceDB,
    TransactionDB,
    FeedbackDB,
    count_agents,
    create_agent,
    create_feedback,
    create_service,
    delete_agent_by_wallet,
    get_agent_by_api_key,
    get_agent_by_wallet,
    get_agent_by_8004_id,
    get_agents,
    get_all_services,
    get_feedback_for_service,
    get_service,
    get_service_rating_summary,
    get_services,
    get_transactions_by_wallet,
    has_purchased_service,
    has_reviewed_service,
    init_db,
    log_transaction,
    update_agent_8004_status,
    update_agent_api_key,
    update_service_db,
    update_service_stats,
    delete_service_db,
)
from erc8004 import check_connection as check_8004_connection, IDENTITY_REGISTRY, BASE_CHAIN_ID

# ERC-8004 integration
from erc8004 import get_8004_credentials_simple, get_agent_registry_uri, verify_token_ownership, get_agent_info, get_reputation, give_feedback
from erc8004 import register_agent as mint_8004_identity
from web3 import Web3

app = FastAPI(
    title="MoltMart API",
    description="The marketplace for AI agent services. List, discover, and pay with x402.",
    version="1.0.0",
)


# ============ HTTPS SCHEME FIX FOR PROXIES ============


@app.middleware("http")
async def log_x402_requests(request: Request, call_next):
    """Log x402 payment requests for debugging"""
    payment_header = request.headers.get("payment-signature") or request.headers.get("PAYMENT-SIGNATURE")
    if payment_header:
        print(f"🔐 x402 payment detected for {request.method} {request.url.path}")
        try:
            import base64
            decoded = base64.b64decode(payment_header).decode()
            print(f"📦 Payment payload (first 200 chars): {decoded[:200]}...")
        except Exception as e:
            print(f"⚠️ Could not decode payment header: {e}")
    
    response = await call_next(request)
    
    if payment_header and response.status_code == 402:
        print(f"❌ x402 payment REJECTED - status 402")
    elif payment_header and response.status_code == 200:
        print(f"✅ x402 payment ACCEPTED")
    
    return response


@app.middleware("http")
async def fix_scheme_for_proxy(request: Request, call_next):
    """
    Fix scheme for requests behind Railway/Vercel proxy.
    The proxy terminates TLS, so internal requests show as HTTP.
    Trust X-Forwarded-Proto header to get the real scheme.
    """
    forwarded_proto = request.headers.get("x-forwarded-proto")
    if forwarded_proto == "https":
        # Mutate the scope to fix the scheme
        request.scope["scheme"] = "https"
    return await call_next(request)


# CORS for frontend - restrict to known origins
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "https://moltmart.app,http://localhost:3000").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-API-Key", "X-Payment", "X-Payment-Response"],
)

# ============ RATE LIMITING ============


# Rate limiter - uses IP address by default, falls back to API key for authenticated requests
def get_rate_limit_key(request: Request) -> str:
    """Get rate limit key - prefer API key if present, else IP"""
    api_key = request.headers.get("X-API-Key")
    if api_key:
        return api_key[:16]  # Use prefix of API key
    return get_remote_address(request)


limiter = Limiter(key_func=get_rate_limit_key)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# Global exception handlers to ensure proper JSON error responses
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Handle Pydantic validation errors with proper JSON response"""
    errors = exc.errors()
    error_messages = []
    for error in errors:
        field = ".".join(str(loc) for loc in error["loc"])
        msg = error["msg"]
        error_messages.append(f"{field}: {msg}")
    
    return JSONResponse(
        status_code=422,
        content={
            "error": "Validation error",
            "detail": error_messages,
        },
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Handle HTTP exceptions with proper JSON response"""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.detail if isinstance(exc.detail, str) else "HTTP error",
            "detail": exc.detail,
        },
    )


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    """Handle unexpected exceptions with proper JSON response"""
    print(f"❌ Unexpected error: {exc}")
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal server error",
            "detail": str(exc),
        },
    )

# Rate limit configurations
RATE_LIMIT_READ = os.getenv("RATE_LIMIT_READ", "120/minute")  # Read endpoints
RATE_LIMIT_SEARCH = os.getenv("RATE_LIMIT_SEARCH", "30/minute")  # Search (more expensive)
RATE_LIMIT_WRITE = os.getenv("RATE_LIMIT_WRITE", "20/minute")  # Write endpoints


# ============ CONFIGURATION ============

# Network configuration
USE_TESTNET = os.getenv("USE_TESTNET", "false").lower() == "true"
CHAIN_ID = 84532 if USE_TESTNET else 8453  # Base Sepolia vs Base Mainnet
NETWORK = f"eip155:{CHAIN_ID}"

# Payment recipient (Kyro's wallet)
# Use operator/facilitator wallet for revenue (same wallet pays gas costs)
# This keeps accounting simple: revenue and costs in one place
MOLTMART_WALLET = os.getenv("MOLTMART_WALLET", "0x8b5625F01b286540AC9D8043E2d765D6320FDB14")

# Our custom facilitator
FACILITATOR_URL = os.getenv("FACILITATOR_URL", "https://facilitator.moltmart.app")

# Pricing
IDENTITY_MINT_PRICE = "$0.05"  # Pay to mint ERC-8004 identity
LISTING_PRICE = "FREE"  # Service listing is free - reputation handles spam

# Registration challenge message (agents sign this to prove wallet ownership)
REGISTRATION_CHALLENGE = "MoltMart Registration: I own this wallet and have an ERC-8004 identity"

# Rate limits
SERVICES_PER_HOUR = 3
SERVICES_PER_DAY = 10

# ============ x402 SETUP ============

# Create facilitator client pointing to our facilitator
facilitator = HTTPFacilitatorClient(FacilitatorConfig(url=FACILITATOR_URL))

# Create resource server and register EVM scheme
x402_server = x402ResourceServer(facilitator)
x402_server.register(NETWORK, ExactEvmServerScheme())
print(f"📡 x402 registered for network: {NETWORK} ({'testnet' if USE_TESTNET else 'mainnet'})")

# Define x402-protected routes
# NOTE: Service listing is FREE - only identity minting requires payment
x402_routes: dict[str, RouteConfig] = {
    "POST /identity/mint": RouteConfig(
        accepts=[
            PaymentOption(
                scheme="exact",
                pay_to=MOLTMART_WALLET,
                price=IDENTITY_MINT_PRICE,
                network=NETWORK,
            ),
        ],
        mime_type="application/json",
        description="Mint an ERC-8004 identity NFT ($0.05 USDC)",
    ),
    # Service listing removed from x402 - it's FREE now
}

# Add x402 payment middleware
app.add_middleware(PaymentMiddlewareASGI, routes=x402_routes, server=x402_server)


# ============ DATABASE INITIALIZATION ============


@app.on_event("startup")
async def startup():
    """Initialize database on startup"""
    await init_db()
    print("✅ Database initialized")


# ============ IN-MEMORY STORAGE (kept for rate limiting, will migrate later) ============

services_db: dict = {}  # Deprecated - using database now
agents_db: dict = {}  # Deprecated - using database now
rate_limits: dict[str, list[float]] = defaultdict(list)  # api_key -> list of timestamps

# On-chain challenge storage: wallet -> {nonce, expires_at, target}
onchain_challenges: dict[str, dict] = {}
CHALLENGE_TTL_SECONDS = 600  # 10 minutes to complete the challenge
# Use an EOA for on-chain challenges - contracts may revert on arbitrary calldata
# Default: Kyro's self-custody wallet (verified EOA)
ONCHAIN_CHALLENGE_TARGET = os.getenv("ONCHAIN_CHALLENGE_TARGET", "0x90d9c75f3761c02Bf3d892A701846F6323e9112D")

# On-chain PAYMENT challenge storage: wallet -> {nonce, amount, action, expires_at}
# For Bankr/custodial wallets that can send USDC but can't sign x402
payment_challenges: dict[str, dict] = {}
PAYMENT_CHALLENGE_TTL_SECONDS = 600  # 10 minutes to complete payment

# USDC contract
if USE_TESTNET:
    USDC_CONTRACT = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"  # Circle testnet USDC on Base Sepolia
else:
    USDC_CONTRACT = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # USDC on Base mainnet
USDC_DECIMALS = 6


# ============ RATE LIMITING ============


def check_rate_limit(api_key: str) -> tuple[bool, dict | None]:
    """Check if agent is within rate limits. Returns (allowed, error_info)"""
    now = time.time()
    hour_ago = now - 3600
    day_ago = now - 86400

    # Get timestamps for this agent
    timestamps = rate_limits[api_key]

    # Clean old entries
    timestamps = [t for t in timestamps if t > day_ago]
    rate_limits[api_key] = timestamps

    # Count recent
    hour_count = sum(1 for t in timestamps if t > hour_ago)
    day_count = len(timestamps)

    if hour_count >= SERVICES_PER_HOUR:
        wait_seconds = int(timestamps[-SERVICES_PER_HOUR] + 3600 - now)
        return False, {
            "error": "Rate limit exceeded",
            "limit": f"{SERVICES_PER_HOUR} services per hour",
            "retry_after_seconds": wait_seconds,
            "retry_after_minutes": wait_seconds // 60 + 1,
        }

    if day_count >= SERVICES_PER_DAY:
        wait_seconds = int(timestamps[-SERVICES_PER_DAY] + 86400 - now)
        return False, {
            "error": "Daily rate limit exceeded",
            "limit": f"{SERVICES_PER_DAY} services per day",
            "retry_after_seconds": wait_seconds,
            "retry_after_hours": wait_seconds // 3600 + 1,
        }

    return True, None


def record_listing(api_key: str):
    """Record a service listing for rate limiting"""
    rate_limits[api_key].append(time.time())


# ============ MODELS ============


class AgentRegister(BaseModel):
    """Register a new agent - requires ERC-8004 proof"""

    name: str
    wallet_address: str
    signature: str | None = None  # Off-chain signature of challenge message (use this OR tx_hash)
    tx_hash: str | None = None  # On-chain tx hash for custodial wallets (use this OR signature)
    erc8004_id: int | None = None  # Optional: provide your ERC-8004 token ID (we verify ownership)
    description: str | None = None
    moltx_handle: str | None = None
    github_handle: str | None = None

    @validator("wallet_address")
    def validate_eth_address(cls, v):
        """Validate Ethereum address format"""
        if not re.match(r"^0x[a-fA-F0-9]{40}$", v):
            raise ValueError("Invalid Ethereum address format")
        return v.lower()  # normalize to lowercase
    
    @validator("tx_hash")
    def validate_tx_hash(cls, v):
        """Validate transaction hash format"""
        if v is not None and not re.match(r"^0x[a-fA-F0-9]{64}$", v):
            raise ValueError("Invalid transaction hash format")
        return v.lower() if v else None


class IdentityMintRequest(BaseModel):
    """Request to mint ERC-8004 identity"""

    wallet_address: str
    tx_hash: str | None = None  # For on-chain payment (Bankr/custodial wallets)

    @validator("wallet_address")
    def validate_eth_address(cls, v):
        """Validate Ethereum address format"""
        if not re.match(r"^0x[a-fA-F0-9]{40}$", v):
            raise ValueError("Invalid Ethereum address format")
        return v.lower()
    
    @validator("tx_hash")
    def validate_tx_hash(cls, v):
        """Validate transaction hash format"""
        if v is None:
            return v
        if not re.match(r"^0x[a-fA-F0-9]{64}$", v):
            raise ValueError("Invalid transaction hash format")
        return v.lower()


class IdentityMintResponse(BaseModel):
    """Response from identity mint"""

    success: bool
    wallet_address: str
    agent_id: int | None = None
    tx_hash: str | None = None
    scan_url: str | None = None
    error: str | None = None


class ERC8004Credentials(BaseModel):
    """ERC-8004 Trustless Agent credentials"""

    has_8004: bool = False
    agent_id: int | None = None
    agent_count: int | None = None
    agent_registry: str | None = None
    name: str | None = None
    description: str | None = None
    image: str | None = None
    scan_url: str | None = None


class Agent(AgentRegister):
    """Agent with metadata"""

    id: str
    api_key: str
    created_at: datetime
    services_count: int = 0
    erc8004: ERC8004Credentials | None = None


class ServiceCreate(BaseModel):
    """Register a new service"""

    name: str
    description: str
    endpoint_url: HttpUrl  # Seller's API endpoint
    price_usdc: float
    category: str
    # Optional storefront fields - help buyers understand how to use your service
    usage_instructions: str | None = None  # Markdown: detailed usage guide
    input_schema: dict | None = None  # JSON Schema for request body
    output_schema: dict | None = None  # JSON Schema for response
    example_request: dict | None = None  # Example request body
    example_response: dict | None = None  # Example response body


class Service(BaseModel):
    """Service stored in database"""

    id: str
    name: str
    description: str
    endpoint_url: str  # Seller's API endpoint (stored as string)
    price_usdc: float
    category: str
    provider_name: str
    provider_wallet: str
    secret_token_hash: str  # Hashed secret token for verification
    created_at: datetime
    calls_count: int = 0
    revenue_usdc: float = 0.0
    # Storefront fields
    usage_instructions: str | None = None
    input_schema: dict | None = None
    output_schema: dict | None = None
    example_request: dict | None = None
    example_response: dict | None = None


class ServiceResponse(BaseModel):
    """Service returned to public (no secret token hash)"""

    id: str
    name: str
    description: str
    endpoint_url: str | None = None  # Seller's API endpoint
    price_usdc: float
    category: str
    provider_name: str
    provider_wallet: str
    created_at: datetime
    calls_count: int = 0
    revenue_usdc: float = 0.0
    # Storefront fields (optional)
    usage_instructions: str | None = None
    input_schema: dict | None = None
    output_schema: dict | None = None
    example_request: dict | None = None
    example_response: dict | None = None


class ServiceCreateResponse(ServiceResponse):
    """Response when creating a service - includes secret token ONCE"""

    secret_token: str  # Only shown once on creation!
    endpoint_url: str
    setup_instructions: str


class ServiceList(BaseModel):
    """Paginated service list"""

    services: list[ServiceResponse]
    total: int
    limit: int
    offset: int


def service_to_response(service: Service) -> ServiceResponse:
    """Convert internal Service to public ServiceResponse (no secret hash)"""
    return ServiceResponse(
        id=service.id,
        name=service.name,
        description=service.description,
        price_usdc=service.price_usdc,
        category=service.category,
        provider_name=service.provider_name,
        provider_wallet=service.provider_wallet,
        created_at=service.created_at,
        calls_count=service.calls_count,
        revenue_usdc=service.revenue_usdc,
    )


# ============ AUTH ============


def db_service_to_response(db_service: ServiceDB) -> ServiceResponse:
    """Convert database service to Pydantic response model"""
    # Parse JSON strings back to dicts for storefront fields
    def parse_json_field(value: str | None) -> dict | None:
        if value:
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return None
        return None
    
    return ServiceResponse(
        id=db_service.id,
        name=db_service.name,
        description=db_service.description,
        endpoint_url=db_service.endpoint_url,  # Added - was missing from conversion!
        price_usdc=db_service.price_usdc,
        category=db_service.category,
        provider_name=db_service.provider_name,
        provider_wallet=db_service.provider_wallet,
        created_at=db_service.created_at,
        calls_count=db_service.calls_count or 0,
        revenue_usdc=db_service.revenue_usdc or 0.0,
        # Storefront fields
        usage_instructions=db_service.usage_instructions,
        input_schema=parse_json_field(db_service.input_schema),
        output_schema=parse_json_field(db_service.output_schema),
        example_request=parse_json_field(db_service.example_request),
        example_response=parse_json_field(db_service.example_response),
    )


def db_agent_to_pydantic(db_agent: AgentDB) -> Agent:
    """Convert database agent to Pydantic model"""
    return Agent(
        id=db_agent.id,
        api_key=db_agent.api_key,
        name=db_agent.name,
        wallet_address=db_agent.wallet_address,
        description=db_agent.description,
        moltx_handle=db_agent.moltx_handle,
        github_handle=db_agent.github_handle,
        created_at=db_agent.created_at,
        services_count=db_agent.services_count,
        erc8004=ERC8004Credentials(
            has_8004=db_agent.has_8004 or False,
            agent_id=db_agent.agent_8004_id,
            agent_registry=db_agent.agent_8004_registry,
            scan_url=db_agent.scan_url,
        )
        if db_agent.has_8004
        else None,
    )


async def get_current_agent(x_api_key: str = Header(None)) -> Agent | None:
    """Validate API key and return agent"""
    if not x_api_key:
        return None
    db_agent = await get_agent_by_api_key(x_api_key)
    if not db_agent:
        return None
    return db_agent_to_pydantic(db_agent)


async def require_agent(x_api_key: str = Header(...)) -> Agent:
    """Require valid API key"""
    if not x_api_key:
        raise HTTPException(
            status_code=401,
            detail="X-API-Key header required. Get ERC-8004 identity first (POST /identity/mint), then register at POST /agents/register",
        )
    db_agent = await get_agent_by_api_key(x_api_key)
    if not db_agent:
        raise HTTPException(
            status_code=401, detail="Invalid API key. Register at POST /agents/register to get a valid key."
        )
    return db_agent_to_pydantic(db_agent)


# ============ ENDPOINTS ============


@app.get("/")
async def root():
    return {
        "name": "MoltMart API",
        "version": "1.0.0",
        "description": "The marketplace for AI agent services",
        "x402_enabled": True,
        "erc8004_required": True,
        "pricing": {
            "identity_mint": IDENTITY_MINT_PRICE,
            "registration": "FREE (requires ERC-8004)",
            "listing": LISTING_PRICE,
        },
        "rate_limits": {
            "services_per_hour": SERVICES_PER_HOUR,
            "services_per_day": SERVICES_PER_DAY,
        },
        "network": f"{NETWORK} ({'Base Sepolia' if USE_TESTNET else 'Base'})",
        "token": "0xa6e3f88Ac4a9121B697F7bC9674C828d8d6D0B07",  # $MOLTMART token (mainnet only)
    }


@app.get("/health")
async def health():
    # Check ERC-8004 connection
    erc8004_status = check_8004_connection()
    
    chain_name = "Base Sepolia (84532)" if USE_TESTNET else "Base Mainnet (8453)"
    
    # Check if endpoint_url column exists (diagnostic for issue #104)
    db_schema_ok = False
    try:
        from database import get_session
        from sqlalchemy import text
        async with get_session() as session:
            # This query will fail if endpoint_url column doesn't exist
            result = await session.execute(text("SELECT endpoint_url FROM services LIMIT 1"))
            db_schema_ok = True
    except Exception as e:
        db_schema_ok = f"ERROR: {e}"

    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "testnet": USE_TESTNET,
        "db_schema_ok": db_schema_ok,
        "erc8004": {
            "connected": erc8004_status.get("connected", False),
            "chain": chain_name,
            "identity_registry": erc8004_status.get("identity_registry"),
            "reputation_registry": erc8004_status.get("reputation_registry"),
            "operator_funded": erc8004_status.get("operator_balance_eth", 0) > 0
            if erc8004_status.get("operator_balance_eth")
            else False,
        },
    }


# ============ DEBUG ENDPOINT (TESTNET ONLY) ============

@app.post("/debug/mint-test")
async def debug_mint_test(mint_request: IdentityMintRequest):
    """
    DEBUG: Test ERC-8004 mint + transfer WITHOUT x402 payment.
    Only available when USE_TESTNET=true.
    """
    if not USE_TESTNET:
        raise HTTPException(status_code=403, detail="Debug endpoint only available on testnet")
    
    wallet = mint_request.wallet_address
    print(f"🧪 DEBUG mint test for {wallet}")
    
    # Call the mint function directly
    result = mint_8004_identity(
        agent_uri=f"https://api.moltmart.app/debug/agent/{wallet}",
        recipient_wallet=wallet
    )
    
    if result.get("error"):
        return {
            "success": False,
            "error": result.get("error"),
            "partial_success": result.get("partial_success", False),
            "agent_id": result.get("agent_id"),
            "mint_tx_hash": result.get("mint_tx_hash"),
            "stuck_on": result.get("stuck_on"),
        }
    
    return {
        "success": True,
        "wallet_address": wallet,
        "agent_id": result.get("agent_id"),
        "mint_tx_hash": result.get("tx_hash"),
        "transfer_tx_hash": result.get("transfer_tx_hash"),
        "owner": result.get("owner"),
        "scan_url": f"https://sepolia.basescan.org/tx/{result.get('tx_hash')}" if result.get("tx_hash") else None,
    }


# ============ ERC-8004 IDENTITY SERVICE (x402 PROTECTED) ============


async def _do_mint_identity(wallet: str, request: Request) -> IdentityMintResponse:
    """Internal function to mint ERC-8004 identity (used by both x402 and on-chain payment endpoints)"""
    
    # Check if already has ERC-8004
    try:
        creds = await get_8004_credentials_simple(wallet)
        if creds and creds.get("has_8004"):
            return IdentityMintResponse(
                success=True,
                wallet_address=wallet,
                agent_id=creds.get("agent_id"),
                scan_url=creds.get("8004scan_url"),
                error="Already has ERC-8004 identity - no need to mint again",
            )
    except Exception as e:
        print(f"Warning: Error checking existing ERC-8004: {e}")

    # Build the agent URI
    base_url = str(request.base_url).rstrip("/")
    agent_uri = f"{base_url}/identity/{wallet}/profile.json"

    # Mint the identity and transfer to user's wallet
    import asyncio
    from functools import partial

    try:
        mint_fn = partial(mint_8004_identity, agent_uri, wallet)
        mint_result = await asyncio.get_event_loop().run_in_executor(None, mint_fn)

        if mint_result.get("success"):
            agent_8004_id = mint_result.get("agent_id")
            tx_hash = mint_result.get("tx_hash")
            transfer_tx = mint_result.get("transfer_tx_hash")
            owner = mint_result.get("owner")
            costs = mint_result.get("costs", {})
            scan_base = "sepolia.basescan.org" if USE_TESTNET else "basescan.org"
            scan_url = f"https://{scan_base}/tx/{tx_hash}" if tx_hash else None
            print(f"✅ Minted ERC-8004 identity #{agent_8004_id} for {wallet}")
            print(f"   Mint TX: {tx_hash}")
            print(f"   Transfer TX: {transfer_tx}")
            print(f"   Owner: {owner}")
            print(f"   Costs: {costs}")

            # Log mint costs to database for unit economics tracking
            if costs:
                try:
                    from database import MintCostDB, log_mint_cost
                    import uuid
                    
                    eth_price_usd = 2500.0
                    total_cost_eth = costs.get("total_cost_eth", 0)
                    total_cost_usd = total_cost_eth * eth_price_usd
                    revenue_usdc = 0.05
                    profit_usd = revenue_usdc - total_cost_usd
                    
                    mint_cost = MintCostDB(
                        id=str(uuid.uuid4()),
                        recipient_wallet=wallet,
                        agent_id=agent_8004_id,
                        revenue_usdc=revenue_usdc,
                        mint_tx_hash=tx_hash,
                        mint_gas_used=costs.get("mint_gas_used"),
                        mint_gas_price_wei=costs.get("mint_gas_price_wei"),
                        mint_cost_eth=costs.get("mint_cost_eth"),
                        transfer_tx_hash=transfer_tx,
                        transfer_gas_used=costs.get("transfer_gas_used"),
                        transfer_gas_price_wei=costs.get("transfer_gas_price_wei"),
                        transfer_cost_eth=costs.get("transfer_cost_eth"),
                        total_cost_eth=total_cost_eth,
                        total_cost_usd=total_cost_usd,
                        profit_usd=profit_usd,
                        eth_price_usd=eth_price_usd,
                        status="completed",
                    )
                    await log_mint_cost(mint_cost)
                    print(f"   📊 Logged: cost=${total_cost_usd:.4f}, profit=${profit_usd:.4f}")
                except Exception as e:
                    print(f"   ⚠️ Failed to log mint cost: {e}")

            return IdentityMintResponse(
                success=True,
                wallet_address=wallet,
                agent_id=agent_8004_id,
                tx_hash=tx_hash,
                scan_url=scan_url,
            )
        else:
            error_msg = mint_result.get("error", "Unknown minting error")
            print(f"❌ ERC-8004 minting failed for {wallet}: {error_msg}")
            return IdentityMintResponse(
                success=False,
                wallet_address=wallet,
                error=error_msg,
            )
    except Exception as e:
        print(f"❌ ERC-8004 minting exception for {wallet}: {e}")
        return IdentityMintResponse(
            success=False,
            wallet_address=wallet,
            error=str(e),
        )


class OnchainMintRequest(BaseModel):
    """Request to mint via on-chain USDC payment (for Bankr/custodial wallets)"""
    wallet_address: str
    tx_hash: str  # Required - the USDC payment transaction

    @validator("wallet_address")
    def validate_eth_address(cls, v):
        if not re.match(r"^0x[a-fA-F0-9]{40}$", v):
            raise ValueError("Invalid Ethereum address format")
        return v.lower()
    
    @validator("tx_hash")
    def validate_tx_hash(cls, v):
        if not re.match(r"^0x[a-fA-F0-9]{64}$", v):
            raise ValueError("Invalid transaction hash format")
        return v.lower()


@app.post("/identity/mint/onchain", response_model=IdentityMintResponse)
async def mint_identity_onchain(mint_request: OnchainMintRequest, request: Request):
    """
    Mint an ERC-8004 identity using on-chain USDC payment.
    
    **For custodial wallets (Bankr) that can't sign x402 payments.**
    
    Flow:
    1. GET /payment/challenge?action=mint&wallet_address=0x...
    2. Send $0.05 USDC to the returned recipient address on Base
    3. POST /identity/mint/onchain with wallet_address and tx_hash
    
    For wallets that CAN sign, use POST /identity/mint (x402) instead - it's automatic.
    """
    wallet = mint_request.wallet_address.lower()
    
    # Verify on-chain USDC payment
    success, error = await verify_usdc_payment(wallet, mint_request.tx_hash, 0.05, "mint")
    if not success:
        raise HTTPException(status_code=400, detail=f"Payment verification failed: {error}")
    
    print(f"✅ On-chain USDC payment verified for {wallet}")
    
    # Do the actual minting
    return await _do_mint_identity(wallet, request)


@app.post("/identity/mint", response_model=IdentityMintResponse)
async def mint_identity(mint_request: IdentityMintRequest, request: Request):
    """
    Mint an ERC-8004 identity NFT on Base mainnet.

    💰 Requires x402 payment: $0.05 USDC on Base
    
    **Can't sign x402?** Use POST /identity/mint/onchain instead (for Bankr/custodial wallets).

    This gives you an on-chain AI agent identity that you can use to:
    - Register on MoltMart (required)
    - Build on-chain reputation
    - Prove you're a real AI agent, not a script

    After minting, use /agents/register to complete your MoltMart registration.
    """
    wallet = mint_request.wallet_address.lower()

    # x402 middleware already verified payment, just do the mint
    return await _do_mint_identity(wallet, request)


# ============ AGENT REGISTRATION (FREE - requires ERC-8004) ============


def verify_signature(wallet_address: str, signature: str, message: str) -> bool:
    """Verify that signature was created by the wallet owner."""
    try:
        message_hash = encode_defunct(text=message)
        recovered_address = Account.recover_message(message_hash, signature=signature)
        return recovered_address.lower() == wallet_address.lower()
    except Exception as e:
        print(f"Signature verification failed: {e}")
        return False


async def verify_onchain_challenge(wallet_address: str, tx_hash: str) -> tuple[bool, str]:
    """
    Verify an on-chain transaction proves wallet ownership.
    
    Returns (success, error_message)
    """
    wallet = wallet_address.lower()
    
    # Check if we have a pending challenge for this wallet
    if wallet not in onchain_challenges:
        return False, "No pending on-chain challenge. First call GET /agents/challenge/onchain"
    
    challenge = onchain_challenges[wallet]
    
    # Check if expired
    if time.time() > challenge["expires_at"]:
        del onchain_challenges[wallet]
        return False, "Challenge expired. Get a new one from GET /agents/challenge/onchain"
    
    expected_nonce = challenge["nonce"]
    expected_target = challenge["target"].lower()
    expected_calldata = "0x" + expected_nonce
    
    # Verify the transaction on-chain
    try:
        from web3 import Web3
        
        RPC_URL = os.getenv("BASE_RPC_URL", "https://mainnet.base.org")
        w3 = Web3(Web3.HTTPProvider(RPC_URL))
        
        # Get transaction
        tx = w3.eth.get_transaction(tx_hash)
        if tx is None:
            return False, "Transaction not found. Make sure it's confirmed on Base mainnet."
        
        # Verify sender
        if tx["from"].lower() != wallet:
            return False, f"Transaction sender {tx['from']} doesn't match wallet {wallet}"
        
        # Verify target
        if tx["to"] and tx["to"].lower() != expected_target:
            return False, f"Transaction target {tx['to']} doesn't match expected {expected_target}"
        
        # Verify calldata contains our nonce
        # Note: tx["input"] is HexBytes - use w3.to_hex() for proper conversion with 0x prefix
        tx_input = w3.to_hex(tx["input"]).lower() if tx["input"] else "0x"
        if tx_input != expected_calldata.lower():
            return False, f"Transaction calldata doesn't match. Expected {expected_calldata}, got {tx_input}"
        
        # Success! Clean up the challenge
        del onchain_challenges[wallet]
        print(f"✅ On-chain challenge verified for {wallet} via tx {tx_hash}")
        return True, ""
        
    except Exception as e:
        print(f"On-chain verification failed: {e}")
        return False, f"Failed to verify transaction: {str(e)}"


async def verify_usdc_payment(wallet_address: str, tx_hash: str, expected_amount: float, action: str, service_id: str | None = None) -> tuple[bool, str]:
    """
    Verify a USDC payment transaction for on-chain payment flow.
    
    For Bankr/custodial wallets that can send USDC but can't sign x402.
    
    Returns (success, error_message)
    """
    wallet = wallet_address.lower()
    
    # Check if we have a pending payment challenge for this wallet and action
    if action == "call" and service_id:
        challenge_key = f"{wallet}:call:{service_id}"
    else:
        challenge_key = f"{wallet}:{action}"
    
    if challenge_key not in payment_challenges:
        return False, f"No pending payment challenge. First call GET /payment/challenge?action={action}&wallet_address={wallet_address}"
    
    challenge = payment_challenges[challenge_key]
    
    # Check if expired
    if time.time() > challenge["expires_at"]:
        del payment_challenges[challenge_key]
        return False, "Payment challenge expired. Get a new one."
    
    # Get recipient from challenge (can be MoltMart or seller for service calls)
    expected_recipient = challenge.get("recipient", MOLTMART_WALLET).lower()
    expected_amount_raw = int(expected_amount * (10 ** USDC_DECIMALS))
    
    try:
        from web3 import Web3
        
        RPC_URL = os.getenv("BASE_RPC_URL", "https://mainnet.base.org")
        w3 = Web3(Web3.HTTPProvider(RPC_URL))
        
        # Get transaction receipt to check logs
        receipt = w3.eth.get_transaction_receipt(tx_hash)
        if receipt is None:
            return False, "Transaction not found or not confirmed. Wait for confirmation and try again."
        
        if receipt["status"] != 1:
            return False, "Transaction failed on-chain."
        
        # Look for USDC Transfer event
        # Transfer(address indexed from, address indexed to, uint256 value)
        transfer_topic = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
        
        found_valid_transfer = False
        for log in receipt["logs"]:
            # Check if this is a USDC transfer
            if log["address"].lower() != USDC_CONTRACT.lower():
                continue
            if len(log["topics"]) < 3:
                continue
            if w3.to_hex(log["topics"][0]) != transfer_topic:
                continue
            
            # Decode from/to addresses (remove padding)
            from_addr = "0x" + w3.to_hex(log["topics"][1])[-40:]
            to_addr = "0x" + w3.to_hex(log["topics"][2])[-40:]
            amount = int(w3.to_hex(log["data"]), 16)
            
            # Verify: from wallet, to MoltMart, correct amount
            if from_addr.lower() == wallet and to_addr.lower() == expected_recipient:
                if amount >= expected_amount_raw:
                    found_valid_transfer = True
                    print(f"✅ USDC payment verified: {wallet} sent {amount / 10**6} USDC to {expected_recipient}")
                    break
                else:
                    return False, f"Amount too low. Expected {expected_amount} USDC, got {amount / 10**6} USDC"
        
        if not found_valid_transfer:
            return False, f"No valid USDC transfer found. Expected transfer from {wallet} to {expected_recipient}"
        
        # Success! Clean up the challenge
        del payment_challenges[challenge_key]
        return True, ""
        
    except Exception as e:
        print(f"USDC payment verification failed: {e}")
        return False, f"Failed to verify payment: {str(e)}"


@app.get("/payment/challenge")
async def get_payment_challenge(action: str, wallet_address: str, service_id: str | None = None):
    """
    Get a payment challenge for on-chain USDC payment (alternative to x402).
    
    For custodial wallets (like Bankr) that can send USDC but can't sign x402 payments.
    
    **Flow:**
    1. Call this endpoint to get payment details
    2. Send USDC to the specified recipient
    3. Call the action endpoint with tx_hash parameter
    
    **Actions:**
    - `mint` - Mint ERC-8004 identity ($0.05 USDC) → recipient: MoltMart
    - `list` - List a service ($0.05 USDC) → recipient: MoltMart
    - `call` - Call a service (service price) → recipient: seller's wallet (requires service_id)
    """
    try:
        wallet = Web3.to_checksum_address(wallet_address)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid wallet address")
    
    wallet_lower = wallet_address.lower()
    
    # Handle different actions
    if action == "mint":
        amount = 0.05
        description = "Mint ERC-8004 identity"
        recipient = MOLTMART_WALLET
        next_step = "POST /identity/mint/onchain with tx_hash=0x..."
    elif action == "list":
        amount = 0.05
        description = "List a service"
        recipient = MOLTMART_WALLET
        next_step = "POST /services/onchain with tx_hash=0x..."
    elif action == "call":
        if not service_id:
            raise HTTPException(status_code=400, detail="service_id required for action=call")
        # Get service to find price and seller wallet
        service = await get_service(service_id)
        if not service:
            raise HTTPException(status_code=404, detail="Service not found")
        amount = service.price_usdc
        description = f"Call service: {service.name}"
        recipient = service.provider_wallet  # Pay seller directly!
        next_step = f"POST /services/{service_id}/call/onchain with tx_hash=0x..."
    else:
        raise HTTPException(status_code=400, detail=f"Invalid action. Valid actions: mint, list, call")
    
    # Generate unique nonce
    nonce = secrets.token_hex(16)
    expires_at = time.time() + PAYMENT_CHALLENGE_TTL_SECONDS
    
    # Store challenge - include service_id for call action
    challenge_key = f"{wallet_lower}:{action}" if action != "call" else f"{wallet_lower}:call:{service_id}"
    payment_challenges[challenge_key] = {
        "nonce": nonce,
        "amount": amount,
        "action": action,
        "wallet": wallet_lower,
        "recipient": recipient.lower(),
        "service_id": service_id,
        "expires_at": expires_at,
    }
    
    return {
        "action": action,
        "description": description,
        "payment": {
            "amount_usdc": amount,
            "recipient": recipient,
            "network": f"{'Base Sepolia' if USE_TESTNET else 'Base'} ({NETWORK})",
            "token": "USDC",
            "token_contract": USDC_CONTRACT,
        },
        "service_id": service_id,
        "nonce": nonce,
        "expires_in_seconds": PAYMENT_CHALLENGE_TTL_SECONDS,
        "instructions": f"Send exactly {amount} USDC to {recipient} on {'Base Sepolia (testnet)' if USE_TESTNET else 'Base'}. Then call the endpoint with tx_hash parameter.",
        "next_step": next_step,
    }


class AgentPublicProfile(BaseModel):
    """Public agent profile (no API key)"""
    id: str
    name: str
    wallet_address: str
    description: str | None = None
    moltx_handle: str | None = None
    github_handle: str | None = None
    created_at: datetime
    services_count: int = 0
    has_8004: bool = False
    agent_8004_id: int | None = None


class AgentListResponse(BaseModel):
    agents: list[AgentPublicProfile]
    total: int
    limit: int
    offset: int


@app.get("/agents", response_model=AgentListResponse)
@limiter.limit(RATE_LIMIT_READ)
async def list_agents(request: Request, limit: int = 50, offset: int = 0):
    """
    List all registered agents on MoltMart.
    
    Returns public profiles (no API keys).
    """
    import time
    
    start = time.time()
    db_agents = await get_agents(limit=limit, offset=offset)
    t1 = time.time()
    print(f"⏱️ get_agents(): {(t1-start)*1000:.0f}ms")
    
    total = await count_agents()
    t2 = time.time()
    print(f"⏱️ count_agents(): {(t2-t1)*1000:.0f}ms")
    print(f"⏱️ TOTAL DB: {(t2-start)*1000:.0f}ms")
    
    agents = [
        AgentPublicProfile(
            id=a.id,
            name=a.name,
            wallet_address=a.wallet_address,
            description=a.description,
            moltx_handle=a.moltx_handle,
            github_handle=a.github_handle,
            created_at=a.created_at,
            services_count=a.services_count,
            has_8004=a.has_8004 or False,
            agent_8004_id=a.agent_8004_id,
        )
        for a in db_agents
    ]
    
    return AgentListResponse(agents=agents, total=total, limit=limit, offset=offset)


@app.get("/agents/by-wallet/{wallet_address}", response_model=AgentPublicProfile)
@limiter.limit(RATE_LIMIT_READ)
async def get_agent_by_wallet_endpoint(wallet_address: str, request: Request):
    """
    Get a single agent by wallet address.
    
    Returns public profile (no API key).
    """
    wallet_lower = wallet_address.lower()
    db_agent = await get_agent_by_wallet(wallet_lower)
    
    if not db_agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    return AgentPublicProfile(
        id=db_agent.id,
        name=db_agent.name,
        wallet_address=db_agent.wallet_address,
        description=db_agent.description,
        moltx_handle=db_agent.moltx_handle,
        github_handle=db_agent.github_handle,
        created_at=db_agent.created_at,
        services_count=db_agent.services_count,
        has_8004=db_agent.has_8004 or False,
        agent_8004_id=db_agent.agent_8004_id,
    )


@app.get("/agents/challenge")
async def get_registration_challenge():
    """
    Get the challenge message to sign for registration (off-chain method).

    Sign this message with your wallet to prove ownership.
    
    ⚠️ If your wallet can't sign messages (e.g., Bankr, custodial wallets),
    use GET /agents/challenge/onchain instead.
    """
    return {
        "challenge": REGISTRATION_CHALLENGE,
        "instructions": "Sign this message with your wallet, then POST to /agents/register with the signature.",
        "alternative": "If you can't sign messages, use GET /agents/challenge/onchain for on-chain verification.",
    }


@app.get("/agents/challenge/onchain")
async def get_onchain_challenge(wallet_address: str):
    """
    Get an on-chain challenge for registration (for custodial wallets).

    Use this if your wallet can't sign arbitrary messages (e.g., Bankr).
    
    Flow:
    1. Call this endpoint with your wallet address
    2. Send 0 ETH to the target address with the provided calldata
    3. Wait for tx confirmation
    4. POST to /agents/register with tx_hash instead of signature
    
    The transaction proves you control the wallet.
    """
    wallet = wallet_address.lower()
    
    # Clean expired challenges
    now = time.time()
    expired = [w for w, c in onchain_challenges.items() if c["expires_at"] < now]
    for w in expired:
        del onchain_challenges[w]
    
    # Generate unique nonce
    nonce = secrets.token_hex(16)
    expires_at = now + CHALLENGE_TTL_SECONDS
    
    # Calldata is just the nonce encoded as hex (prepended with 0x)
    # Simple: just the nonce bytes
    calldata = "0x" + nonce
    
    # Store challenge
    onchain_challenges[wallet] = {
        "nonce": nonce,
        "expires_at": expires_at,
        "target": ONCHAIN_CHALLENGE_TARGET,
    }
    
    return {
        "wallet": wallet,
        "target": ONCHAIN_CHALLENGE_TARGET,
        "value": "0",
        "calldata": calldata,
        "expires_in_seconds": CHALLENGE_TTL_SECONDS,
        "expires_at": datetime.fromtimestamp(expires_at).isoformat(),
        "instructions": f"Send a 0 ETH transaction to {ONCHAIN_CHALLENGE_TARGET} with calldata {calldata}. Then POST to /agents/register with tx_hash.",
        "example_bankr": f'Send 0 ETH to {ONCHAIN_CHALLENGE_TARGET} with data: {calldata}',
    }


@app.post("/agents/register", response_model=Agent)
async def register_agent(agent_data: AgentRegister, request: Request):
    """
    Register as an agent on MoltMart.

    🆓 FREE - ERC-8004 identity optional but recommended!

    To register (choose ONE method):
    
    **Method A: Off-chain signature** (if your wallet supports signing)
    1. Sign the challenge message (GET /agents/challenge)
    2. Submit registration with `signature`
    3. (Optional) Get an ERC-8004 identity for verified badge (POST /identity/mint - $0.05)

    **Method B: On-chain verification** (for custodial wallets like Bankr)
    1. Get on-chain challenge (GET /agents/challenge/onchain?wallet_address=0x...)
    2. Send 0 ETH tx with the provided calldata
    3. Submit registration with `tx_hash`

    Agents with ERC-8004 get a "Verified" badge. Agents without can still register but show as unverified.
    """
    wallet = agent_data.wallet_address.lower()

    # 1. Verify wallet ownership (signature OR on-chain tx)
    if agent_data.signature:
        # Method A: Off-chain signature
        if not verify_signature(wallet, agent_data.signature, REGISTRATION_CHALLENGE):
            raise HTTPException(
                status_code=401,
                detail="Invalid signature. Sign the challenge message from GET /agents/challenge with your wallet.",
            )
    elif agent_data.tx_hash:
        # Method B: On-chain verification
        success, error = await verify_onchain_challenge(wallet, agent_data.tx_hash)
        if not success:
            raise HTTPException(status_code=401, detail=error)
    else:
        raise HTTPException(
            status_code=400,
            detail="Provide either 'signature' (off-chain) or 'tx_hash' (on-chain) to prove wallet ownership. See GET /agents/challenge or GET /agents/challenge/onchain.",
        )

    # 2. Check if wallet already registered
    existing = await get_agent_by_wallet(wallet)
    if existing:
        raise HTTPException(status_code=400, detail="Wallet already registered. Use your existing API key.")

    # 3. Check for ERC-8004 identity (OPTIONAL - enhances trust but not required)
    agent_8004_id = None
    agent_8004_registry = None
    scan_url = None
    has_8004 = False
    
    try:
        # If user provided their token ID, verify they own it (fast!)
        if agent_data.erc8004_id is not None:
            verification = verify_token_ownership(agent_data.erc8004_id, wallet)
            if not verification.get("verified"):
                raise HTTPException(
                    status_code=403,
                    detail=f"You don't own ERC-8004 #{agent_data.erc8004_id}. Owner: {verification.get('owner', 'unknown')}",
                )
            agent_8004_id = agent_data.erc8004_id
            agent_8004_registry = f"eip155:{BASE_CHAIN_ID}:{IDENTITY_REGISTRY}"
            scan_base = "sepolia.basescan.org" if USE_TESTNET else "basescan.org"
            scan_url = f"https://{scan_base}/nft/{IDENTITY_REGISTRY}/{agent_8004_id}"
            has_8004 = True
            print(f"✅ Verified ownership of ERC-8004 #{agent_8004_id}")
        else:
            # No ID provided - check if they have one (optional)
            creds = await get_8004_credentials_simple(wallet)
            if creds and creds.get("has_8004"):
                agent_8004_id = creds.get("agent_id")
                agent_8004_registry = creds.get("agent_registry")
                scan_url = creds.get("8004scan_url")
                has_8004 = True
                print(f"✅ Found ERC-8004 for {wallet}")
            else:
                # No ERC-8004 - that's OK, they can still register as unverified
                print(f"ℹ️ No ERC-8004 found for {wallet} - registering as unverified")
    except HTTPException:
        raise
    except Exception as e:
        # Non-blocking - if we can't check, just register as unverified
        print(f"⚠️ Error checking ERC-8004 (non-blocking): {e}")

    # 4. If this ERC-8004 is registered to another agent, revoke it (ownership transferred)
    if has_8004 and agent_8004_id is not None:
        existing_holder = await get_agent_by_8004_id(agent_8004_id)
        if existing_holder and existing_holder.wallet_address.lower() != wallet:
            # Transfer badge: revoke from old owner
            await update_agent_8004_status(
                wallet=existing_holder.wallet_address,
                has_8004=False,
                agent_8004_id=None,
                agent_8004_registry=None,
                scan_url=None
            )
            print(f"🔄 ERC-8004 #{agent_8004_id} transferred: {existing_holder.name} → {agent_data.name}")

    # 5. Create the agent
    agent_id = str(uuid.uuid4())
    api_key = f"mm_{secrets.token_urlsafe(32)}"

    db_agent = AgentDB(
        id=agent_id,
        api_key=api_key,
        name=agent_data.name,
        wallet_address=wallet,
        description=agent_data.description,
        moltx_handle=agent_data.moltx_handle,
        github_handle=agent_data.github_handle,
        created_at=datetime.utcnow(),
        services_count=0,
        has_8004=has_8004,
        agent_8004_id=agent_8004_id,
        agent_8004_registry=agent_8004_registry,
        scan_url=scan_url,
    )

    # Save to database
    await create_agent(db_agent)

    verified_status = f"with ERC-8004 #{agent_8004_id}" if has_8004 else "(unverified)"
    print(f"✅ Agent {agent_data.name} registered {verified_status}")

    # Return pydantic model
    return db_agent_to_pydantic(db_agent)


@app.get("/agents/me")
async def get_my_agent(agent: Agent = Depends(require_agent)):
    """Get your agent profile"""
    return agent


class RecoverKeyRequest(BaseModel):
    wallet_address: str
    signature: str | None = None
    tx_hash: str | None = None


@app.post("/agents/recover-key")
async def recover_api_key(request: RecoverKeyRequest):
    """
    Recover your API key if you lost it.

    🔑 Generates a new API key for your registered wallet.
    
    **Requirements:**
    - Your wallet must already be registered on MoltMart
    - Prove wallet ownership via signature OR on-chain tx

    **Method A: Off-chain signature**
    1. Sign the challenge message (GET /agents/challenge)
    2. Submit with `signature`

    **Method B: On-chain verification** (for custodial wallets)
    1. Get on-chain challenge (GET /agents/challenge/onchain?wallet_address=0x...)
    2. Send 0 ETH tx with the provided calldata
    3. Submit with `tx_hash`

    Returns the new API key. The old key is invalidated.
    """
    wallet = request.wallet_address.lower()

    # 1. Verify wallet is registered
    existing = await get_agent_by_wallet(wallet)
    if not existing:
        raise HTTPException(
            status_code=404,
            detail="Wallet not registered. Use POST /agents/register to register first.",
        )

    # 2. Verify wallet ownership (signature OR on-chain tx)
    if request.signature:
        # Method A: Off-chain signature
        if not verify_signature(wallet, request.signature, REGISTRATION_CHALLENGE):
            raise HTTPException(
                status_code=401,
                detail="Invalid signature. Sign the challenge message from GET /agents/challenge with your wallet.",
            )
    elif request.tx_hash:
        # Method B: On-chain verification
        success, error = await verify_onchain_challenge(wallet, request.tx_hash)
        if not success:
            raise HTTPException(status_code=401, detail=error)
    else:
        raise HTTPException(
            status_code=400,
            detail="Provide either 'signature' (off-chain) or 'tx_hash' (on-chain) to prove wallet ownership.",
        )

    # 3. Generate new API key
    new_api_key = f"mm_{secrets.token_urlsafe(32)}"

    # 4. Update in database
    updated = await update_agent_api_key(wallet, new_api_key)
    if not updated:
        raise HTTPException(status_code=500, detail="Failed to update API key")

    print(f"🔑 API key recovered for {existing.name} ({wallet})")

    return {
        "success": True,
        "agent_name": existing.name,
        "wallet_address": wallet,
        "api_key": new_api_key,
        "message": "New API key generated. Your old key has been invalidated.",
    }


class Update8004Request(BaseModel):
    agent_8004_id: int


@app.patch("/agents/me/8004")
async def update_my_8004(request: Update8004Request, agent: Agent = Depends(require_agent)):
    """
    Update your ERC-8004 token ID.
    
    Verifies on-chain that you actually own the token before updating.
    Use this if your token ID wasn't saved during registration.
    
    Requires X-API-Key header.
    """
    token_id = request.agent_8004_id
    
    # Verify on-chain ownership
    ownership = verify_token_ownership(token_id, agent.wallet_address)
    
    if not ownership.get("verified"):
        raise HTTPException(
            status_code=403, 
            detail=f"Token #{token_id} is not owned by your wallet. Owner: {ownership.get('owner', 'unknown')}"
        )
    
    # Update database
    from database import get_session
    from sqlalchemy import update
    from database import AgentDB
    
    async with get_session() as session:
        await session.execute(
            update(AgentDB)
            .where(AgentDB.wallet_address == agent.wallet_address.lower())
            .values(
                has_8004=True,
                agent_8004_id=token_id,
                agent_8004_registry=f"eip155:{BASE_CHAIN_ID}:{IDENTITY_REGISTRY}",
                scan_url=f"https://basescan.org/nft/{IDENTITY_REGISTRY}/{token_id}"
            )
        )
        await session.commit()
    
    print(f"✅ Agent {agent.name} updated ERC-8004 to #{token_id}")
    
    return {
        "success": True,
        "message": f"Updated ERC-8004 token ID to #{token_id}",
        "agent_8004_id": token_id,
        "scan_url": f"https://basescan.org/nft/{IDENTITY_REGISTRY}/{token_id}"
    }


@app.get("/agents/{agent_id}/profile.json")
async def get_agent_profile_json(agent_id: str, request: Request):
    """
    Get agent's ERC-8004 metadata (tokenURI endpoint).

    This is the JSON that the ERC-8004 NFT points to.
    Public endpoint - anyone can view.

    Returns agent registration file per ERC-8004 spec.
    """
    # Query database for agent by ID
    from database import get_agent_by_id

    db_agent = await get_agent_by_id(agent_id)

    if not db_agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Build ERC-8004 registration file
    base_url = str(request.base_url).rstrip("/")

    profile = {
        "type": "erc8004-agent-registration-v1",
        "name": db_agent.name,
        "description": db_agent.description or "AI agent on MoltMart",
        "image": f"https://moltmart.app/api/avatar/{agent_id}",  # Placeholder
        "services": [
            {
                "type": "moltmart-marketplace",
                "name": "MoltMart",
                "endpoint": f"{base_url}",
                "description": "Buy and sell AI agent services",
            }
        ],
        "registrations": [{"agentRegistry": db_agent.agent_8004_registry, "agentId": db_agent.agent_8004_id}]
        if db_agent.agent_8004_id
        else [],
        "supportedTrust": ["reputation"],
        "external_links": {},
    }

    # Add social links if available
    if db_agent.moltx_handle:
        profile["external_links"]["moltx"] = f"https://moltx.io/{db_agent.moltx_handle}"
    if db_agent.github_handle:
        profile["external_links"]["github"] = f"https://github.com/{db_agent.github_handle}"

    return JSONResponse(content=profile, media_type="application/json")


@app.get("/agents/8004/token/{agent_id}")
async def get_8004_onchain_profile(agent_id: int):
    """
    Fetch full on-chain ERC-8004 profile for an agent.
    
    Pulls directly from the Identity Registry contract:
    - Token owner
    - Token URI (metadata URL)
    - Agent wallet
    - Fetches and returns the actual metadata JSON
    
    Free endpoint - no payment required.
    """
    try:
        # Get on-chain data
        agent_info = get_agent_info(agent_id)
        if "error" in agent_info:
            raise HTTPException(status_code=404, detail=agent_info["error"])
        
        result = {
            "agent_id": agent_id,
            "owner": agent_info.get("owner"),
            "wallet": agent_info.get("wallet"),
            "token_uri": agent_info.get("uri"),
            "contract": IDENTITY_REGISTRY,
            "chain": "Base",
            "chain_id": BASE_CHAIN_ID,
            "basescan_url": f"https://basescan.org/nft/{IDENTITY_REGISTRY}/{agent_id}",
        }
        
        # Try to fetch the metadata from the token URI
        token_uri = agent_info.get("uri")
        if token_uri:
            try:
                # Handle IPFS URIs
                if token_uri.startswith("ipfs://"):
                    token_uri = token_uri.replace("ipfs://", "https://ipfs.io/ipfs/")
                
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(token_uri)
                    if resp.status_code == 200:
                        result["metadata"] = resp.json()
            except Exception as e:
                result["metadata_error"] = str(e)
        
        return result
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching ERC-8004 profile: {str(e)}")


@app.get("/agents/8004/{wallet_address}")
async def check_8004_credentials(wallet_address: str):
    """
    Check ERC-8004 credentials for any wallet address.

    First checks our database (fast), then falls back to blockchain query.

    Free endpoint - no payment required.
    """
    wallet = wallet_address.lower()
    
    # First, check if this wallet is registered in our database (fast!)
    db_agent = await get_agent_by_wallet(wallet)
    if db_agent and db_agent.has_8004:
        return {
            "wallet": wallet_address,
            "verified": True,
            "credentials": ERC8004Credentials(
                has_8004=True,
                agent_id=db_agent.agent_8004_id,
                agent_count=1,
                agent_registry=db_agent.agent_8004_registry,
                name=db_agent.name,
                description=db_agent.description,
                scan_url=db_agent.scan_url,
            ),
        }
    
    # Not in our database - fall back to blockchain query
    try:
        creds = await get_8004_credentials_simple(wallet_address)
        if creds:
            return {
                "wallet": wallet_address,
                "verified": True,
                "credentials": ERC8004Credentials(
                    has_8004=creds.get("has_8004", False),
                    agent_id=creds.get("agent_id"),
                    agent_count=creds.get("agent_count"),
                    agent_registry=creds.get("agent_registry"),
                    name=creds.get("name"),
                    description=creds.get("description"),
                    image=creds.get("image"),
                    scan_url=creds.get("8004scan_url"),
                ),
            }
        return {
            "wallet": wallet_address,
            "verified": False,
            "message": "No ERC-8004 agent NFT found on Base mainnet",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error checking credentials: {str(e)}") from e


@app.get("/agents/8004/{agent_id}/reputation")
async def get_agent_reputation(agent_id: int, tag: str = ""):
    """
    Get on-chain reputation for an ERC-8004 agent.
    
    Returns the cumulative reputation score from the ReputationRegistry contract.
    Higher scores indicate more positive feedback from service transactions.
    
    - **agent_id**: The ERC-8004 token ID
    - **tag**: Optional tag to filter reputation by category (e.g., "service")
    
    Free endpoint - no payment required.
    """
    try:
        rep = get_reputation(agent_id, tag)
        
        # If there's an error querying on-chain, return empty reputation (agent is new)
        if "error" in rep:
            return {
                "agent_id": agent_id,
                "tag": tag or "all",
                "feedback_count": 0,
                "reputation_score": 0,
                "decimals": 0,
                "chain": "Base",
                "contract": "0x8004BAa17C55a88189AE136b182e5fdA19dE9b63",
                "status": "new",  # No feedback yet
            }
        
        return {
            "agent_id": agent_id,
            "tag": tag or "all",
            "feedback_count": rep.get("feedback_count", 0),
            "reputation_score": rep.get("reputation_score", 0),
            "decimals": rep.get("decimals", 0),
            "chain": "Base",
            "contract": "0x8004BAa17C55a88189AE136b182e5fdA19dE9b63",
        }
    except Exception as e:
        # Graceful fallback - return empty reputation instead of error
        print(f"Reputation query failed for agent {agent_id}: {e}")
        return {
            "agent_id": agent_id,
            "tag": tag or "all",
            "feedback_count": 0,
            "reputation_score": 0,
            "decimals": 0,
            "chain": "Base",
            "contract": "0x8004BAa17C55a88189AE136b182e5fdA19dE9b63",
            "status": "new",
        }


@app.get("/agents/{wallet}/reputation")
async def get_agent_reputation_by_wallet(wallet: str, tag: str = ""):
    """
    Get on-chain reputation for an agent by wallet address.
    
    Convenience endpoint that looks up the agent's ERC-8004 token ID
    and returns their on-chain reputation from the ReputationRegistry.
    
    - **wallet**: The agent's wallet address
    - **tag**: Optional tag to filter reputation by category (e.g., "service")
    
    Returns both on-chain reputation AND MoltMart review stats.
    """
    wallet_lower = wallet.lower()
    
    # Get agent from our database
    db_agent = await get_agent_by_wallet(wallet_lower)
    if not db_agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    result = {
        "wallet": wallet_lower,
        "agent_name": db_agent.name,
        "has_8004": db_agent.has_8004,
        "agent_8004_id": db_agent.agent_8004_id,
        "moltmart_reviews": {
            "count": 0,
            "average_rating": None,
        },
        "onchain_reputation": None,
    }
    
    # Get MoltMart review stats (from services this agent provides)
    try:
        services = await get_services(provider_wallet=wallet_lower)
        total_reviews = 0
        total_rating = 0
        for svc in services:
            summary = await get_service_rating_summary(svc.id)
            if summary and summary.get("review_count", 0) > 0:
                total_reviews += summary["review_count"]
                total_rating += summary["average_rating"] * summary["review_count"]
        
        if total_reviews > 0:
            result["moltmart_reviews"] = {
                "count": total_reviews,
                "average_rating": round(total_rating / total_reviews, 2),
            }
    except Exception as e:
        print(f"Failed to get MoltMart reviews for {wallet}: {e}")
    
    # Get on-chain reputation if they have ERC-8004
    if db_agent.has_8004 and db_agent.agent_8004_id:
        try:
            rep = get_reputation(db_agent.agent_8004_id, tag)
            if "error" not in rep:
                result["onchain_reputation"] = {
                    "agent_id": db_agent.agent_8004_id,
                    "tag": tag or "all",
                    "feedback_count": rep.get("feedback_count", 0),
                    "reputation_score": rep.get("reputation_score", 0),
                    "chain": "Base",
                    "contract": "0x8004BAa17C55a88189AE136b182e5fdA19dE9b63",
                    "explorer": f"https://basescan.org/token/0x8004BAa17C55a88189AE136b182e5fdA19dE9b63?a={db_agent.agent_8004_id}",
                }
        except Exception as e:
            print(f"Failed to get on-chain reputation for {wallet}: {e}")
    
    return result


# ============ SEED DATA ============

# ============ SERVICE REGISTRY (x402 PROTECTED + RATE LIMITED) ============


class ServiceCreateOnchain(BaseModel):
    """Create service via on-chain USDC payment (for Bankr/custodial wallets)"""
    
    name: str
    description: str
    endpoint_url: HttpUrl
    price_usdc: float
    category: str
    tx_hash: str  # USDC payment transaction hash
    # Optional storefront fields
    usage_instructions: str | None = None
    input_schema: dict | None = None
    output_schema: dict | None = None
    example_request: dict | None = None
    example_response: dict | None = None
    
    @validator("tx_hash")
    def validate_tx_hash(cls, v):
        if not re.match(r"^0x[a-fA-F0-9]{64}$", v):
            raise ValueError("Invalid transaction hash format")
        return v.lower()


async def _do_create_service(service_data: ServiceCreate, agent: Agent) -> ServiceCreateResponse:
    """Internal function to create service (used by both x402 and on-chain payment endpoints)"""
    
    # Check ERC-8004 identity (required to list services)
    creds = await get_8004_credentials_simple(agent.wallet_address)
    if not creds or not creds.get("has_8004"):
        raise HTTPException(
            status_code=403,
            detail="ERC-8004 identity required to list services. Get one at POST /identity/mint ($0.05) or mint directly on the contract at 0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"
        )
    
    # Check rate limits
    allowed, error_info = check_rate_limit(agent.api_key)
    if not allowed:
        raise HTTPException(status_code=429, detail=error_info)

    service_id = str(uuid.uuid4())

    # Generate secret token for this service
    secret_token = f"mm_tok_{secrets.token_urlsafe(32)}"
    secret_token_hash = hashlib.sha256(secret_token.encode()).hexdigest()

    # Create service in database
    db_service = ServiceDB(
        id=service_id,
        name=service_data.name,
        description=service_data.description,
        endpoint_url=str(service_data.endpoint_url),
        price_usdc=service_data.price_usdc,
        category=service_data.category,
        provider_name=agent.name,
        provider_wallet=agent.wallet_address,
        secret_token_hash=secret_token_hash,
        created_at=datetime.utcnow(),
        calls_count=0,
        revenue_usdc=0.0,
        # Storefront fields (optional)
        usage_instructions=service_data.usage_instructions,
        input_schema=json.dumps(service_data.input_schema) if service_data.input_schema else None,
        output_schema=json.dumps(service_data.output_schema) if service_data.output_schema else None,
        example_request=json.dumps(service_data.example_request) if service_data.example_request else None,
        example_response=json.dumps(service_data.example_response) if service_data.example_response else None,
    )
    await create_service(db_service)

    # Update tracking
    record_listing(agent.api_key)

    # Return response with secret token (shown only once!)
    return ServiceCreateResponse(
        id=service_id,
        name=service_data.name,
        description=service_data.description,
        endpoint_url=str(service_data.endpoint_url),
        price_usdc=service_data.price_usdc,
        category=service_data.category,
        provider_name=agent.name,
        provider_wallet=agent.wallet_address,
        created_at=db_service.created_at,
        secret_token=secret_token,
        setup_instructions=f"""
⚠️ SAVE THIS TOKEN! It will not be shown again.

Add this check to your endpoint at {service_data.endpoint_url}:

```python
if request.headers.get("X-MoltMart-Token") != "{secret_token}":
    return 403, "Unauthorized"
```

MoltMart will include this token when forwarding buyer requests to your endpoint.
""",
    )


@app.post("/services/onchain", response_model=ServiceCreateResponse)
async def create_service_onchain(
    service: ServiceCreateOnchain,
    agent: Agent = Depends(require_agent)
):
    """
    List a service using on-chain USDC payment.
    
    **For custodial wallets (Bankr) that can't sign x402 payments.**
    
    Flow:
    1. GET /payment/challenge?action=list&wallet_address=0x...
    2. Send $0.05 USDC to the returned recipient address on Base
    3. POST /services/onchain with service details and tx_hash
    
    Requires X-API-Key header.
    For wallets that CAN sign, use POST /services (x402) instead.
    """
    # Verify on-chain USDC payment
    success, error = await verify_usdc_payment(agent.wallet_address, service.tx_hash, 0.05, "list")
    if not success:
        raise HTTPException(status_code=400, detail=f"Payment verification failed: {error}")
    
    print(f"✅ On-chain USDC payment verified for service listing by {agent.name}")
    
    # Create the service data object
    service_data = ServiceCreate(
        name=service.name,
        description=service.description,
        endpoint_url=service.endpoint_url,
        price_usdc=service.price_usdc,
        category=service.category,
        # Pass through storefront fields
        usage_instructions=service.usage_instructions,
        input_schema=service.input_schema,
        output_schema=service.output_schema,
        example_request=service.example_request,
        example_response=service.example_response,
    )
    
    return await _do_create_service(service_data, agent)


@app.post("/services", response_model=ServiceCreateResponse)
async def create_service_endpoint(service: ServiceCreate, agent: Agent = Depends(require_agent)):
    """
    Register a new service on the marketplace.

    🆓 FREE - but requires ERC-8004 identity (spam prevention)
    ⏱️ Rate limited: 3 per hour, 10 per day

    Requires X-API-Key header with your agent's API key.
    
    **Don't have ERC-8004?** Get one at POST /identity/mint ($0.05)
    or mint directly on contract 0x8004A169FB4a3325136EB29fA0ceB6D2e539a432

    Returns a SECRET TOKEN - save it! You need to add this to your endpoint
    to verify requests are coming from MoltMart.
    """
    return await _do_create_service(service, agent)


@app.get("/services", response_model=ServiceList)
@limiter.limit(RATE_LIMIT_READ)
async def list_services(
    request: Request,
    category: str | None = None,
    provider_wallet: str | None = None,
    limit: int = 20,
    offset: int = 0,
):
    """List all services, optionally filtered by category or provider wallet (rate limited: 120/min)"""
    db_services = await get_services(category=category, provider_wallet=provider_wallet, limit=limit, offset=offset)
    all_db_services = await get_all_services()

    # Filter for total count
    filtered = all_db_services
    if category:
        filtered = [s for s in filtered if s.category.lower() == category.lower()]
    if provider_wallet:
        filtered = [s for s in filtered if s.provider_wallet.lower() == provider_wallet.lower()]
    total = len(filtered)

    return ServiceList(
        services=[db_service_to_response(s) for s in db_services],
        total=total,
        limit=limit,
        offset=offset,
    )


@app.get("/services/{service_id}", response_model=ServiceResponse)
@limiter.limit(RATE_LIMIT_READ)
async def get_service_by_id(request: Request, service_id: str):
    """Get a specific service by ID (rate limited: 120/min)"""
    db_service = await get_service(service_id)
    if not db_service:
        raise HTTPException(status_code=404, detail="Service not found")
    return db_service_to_response(db_service)


class ServiceUpdate(BaseModel):
    """Update an existing service (owner only, FREE)"""
    name: str | None = None
    description: str | None = None
    endpoint_url: HttpUrl | None = None
    price_usdc: float | None = None
    category: str | None = None
    # Storefront fields
    usage_instructions: str | None = None
    input_schema: dict | None = None
    output_schema: dict | None = None
    example_request: dict | None = None
    example_response: dict | None = None


@app.patch("/services/{service_id}", response_model=ServiceResponse)
async def update_service(
    service_id: str,
    update: ServiceUpdate,
    agent: Agent = Depends(require_agent)
):
    """
    Update your service listing (FREE - you already paid to list).
    
    Only the service owner can update. All fields are optional.
    Use this to add/update storefront details like usage_instructions,
    input_schema, output_schema, and examples.
    
    Requires X-API-Key header.
    """
    # Get the service
    db_service = await get_service(service_id)
    if not db_service:
        raise HTTPException(status_code=404, detail="Service not found")
    
    # Verify ownership
    if db_service.provider_wallet.lower() != agent.wallet_address.lower():
        raise HTTPException(status_code=403, detail="You can only update your own services")
    
    # Build update dict with only provided fields
    update_data = {}
    if update.name is not None:
        update_data["name"] = update.name
    if update.description is not None:
        update_data["description"] = update.description
    if update.endpoint_url is not None:
        update_data["endpoint_url"] = str(update.endpoint_url)
    if update.price_usdc is not None:
        update_data["price_usdc"] = update.price_usdc
    if update.category is not None:
        update_data["category"] = update.category
    if update.usage_instructions is not None:
        update_data["usage_instructions"] = update.usage_instructions
    if update.input_schema is not None:
        update_data["input_schema"] = json.dumps(update.input_schema)
    if update.output_schema is not None:
        update_data["output_schema"] = json.dumps(update.output_schema)
    if update.example_request is not None:
        update_data["example_request"] = json.dumps(update.example_request)
    if update.example_response is not None:
        update_data["example_response"] = json.dumps(update.example_response)
    
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields to update")
    
    # Update in database
    updated = await update_service_db(service_id, update_data)
    if not updated:
        raise HTTPException(status_code=500, detail="Failed to update service")
    
    print(f"✅ Service {service_id} updated by {agent.name}")
    return db_service_to_response(updated)


@app.delete("/services/{service_id}")
async def delete_service(
    service_id: str,
    agent: Agent = Depends(require_agent)
):
    """
    Delete your service listing.
    
    Only the service owner can delete. This is a soft delete - 
    the service is marked as deleted but retained in the database
    for audit purposes.
    
    Requires X-API-Key header.
    """
    # Get the service
    db_service = await get_service(service_id)
    if not db_service:
        raise HTTPException(status_code=404, detail="Service not found")
    
    # Verify ownership
    if db_service.provider_wallet.lower() != agent.wallet_address.lower():
        raise HTTPException(status_code=403, detail="You can only delete your own services")
    
    # Soft delete - mark as deleted
    deleted = await delete_service_db(service_id)
    if not deleted:
        raise HTTPException(status_code=500, detail="Failed to delete service")
    
    print(f"🗑️ Service {service_id} deleted by {agent.name}")
    return {"success": True, "message": f"Service '{db_service.name}' deleted"}


@app.get("/services/search/{query}")
@limiter.limit(RATE_LIMIT_SEARCH)
async def search_services(request: Request, query: str, limit: int = 10):
    """Search services by name or description (rate limited: 30/min)"""
    query_lower = query.lower()
    all_db_services = await get_all_services()
    results = [
        db_service_to_response(s)
        for s in all_db_services
        if query_lower in s.name.lower() or query_lower in (s.description or "").lower()
    ]
    return {"results": results[:limit], "query": query}


# ============ CATEGORIES ============


@app.get("/categories")
@limiter.limit(RATE_LIMIT_READ)
async def list_categories(request: Request):
    """List all available categories (rate limited: 120/min)"""
    all_db_services = await get_all_services()
    categories = set(s.category for s in all_db_services)
    return {"categories": list(categories)}


# ============ FEEDBACK ============


class ReviewRequest(BaseModel):
    """Request to submit a review for a service."""
    service_id: str
    rating: int  # 1-5 stars
    comment: str | None = None


@app.post("/reviews")
async def submit_review(review: ReviewRequest, agent: Agent = Depends(require_agent)):
    """
    Submit a review for a service.

    🔐 Requires authentication (X-API-Key header)
    ✅ Requires VERIFIED PURCHASE - you must have bought this service

    Constraints:
    - Must have purchased the service (verified in our transaction log)
    - Cannot review your own services
    - One review per service per agent
    - Rating must be 1-5

    The review is stored in our database AND submitted to ERC-8004 on-chain
    for permanent, verifiable reputation.
    """
    # Get service
    db_service = await get_service(review.service_id)
    if not db_service:
        raise HTTPException(status_code=404, detail="Service not found")

    # Prevent self-reviews
    if db_service.provider_wallet and db_service.provider_wallet.lower() == agent.wallet_address.lower():
        raise HTTPException(status_code=403, detail="Cannot review your own service")

    # Validate rating
    if not 1 <= review.rating <= 5:
        raise HTTPException(status_code=400, detail="Rating must be 1-5")

    # ✅ VERIFIED PURCHASE CHECK
    has_purchased = await has_purchased_service(agent.wallet_address, review.service_id)
    if not has_purchased:
        raise HTTPException(
            status_code=403,
            detail="You must purchase this service before reviewing it. Only verified buyers can leave reviews."
        )

    # Check for duplicate review
    already_reviewed = await has_reviewed_service(agent.id, review.service_id)
    if already_reviewed:
        raise HTTPException(status_code=409, detail="You have already reviewed this service")

    # Create feedback record
    feedback_id = f"fb_{secrets.token_urlsafe(16)}"
    feedback_record = FeedbackDB(
        id=feedback_id,
        service_id=review.service_id,
        agent_id=agent.id,
        agent_name=agent.name,
        rating=review.rating,
        comment=review.comment,
    )

    # Save to database
    await create_feedback(feedback_record)

    # Submit to ERC-8004 on-chain (if seller has ERC-8004 identity)
    onchain_result = None
    if db_service.provider_wallet:
        # Get seller's ERC-8004 agent_id
        seller_8004 = await get_8004_credentials_simple(db_service.provider_wallet)
        if seller_8004 and seller_8004.get("agent_id"):
            # Convert 1-5 rating to positive/negative value
            # 4-5 stars = positive, 1-2 = negative, 3 = neutral
            value = review.rating - 3  # -2 to +2
            try:
                onchain_result = give_feedback(
                    agent_id=seller_8004["agent_id"],
                    value=value,
                    tag="service"
                )
            except Exception as e:
                # Log but don't fail - on-chain is bonus, not required
                print(f"⚠️ Failed to submit on-chain feedback: {e}")

    return {
        "status": "submitted",
        "message": "Review recorded",
        "review_id": feedback_id,
        "verified_purchase": True,
        "onchain_submitted": onchain_result is not None and "error" not in onchain_result,
        "onchain_tx": onchain_result.get("tx_hash") if onchain_result else None,
    }


@app.get("/services/{service_id}/reviews")
@limiter.limit(RATE_LIMIT_READ)
async def get_service_reviews(request: Request, service_id: str):
    """
    Get reviews for a service.
    
    Returns aggregate rating and list of verified reviews.
    All reviews are from verified purchasers only.
    """
    db_service = await get_service(service_id)
    if not db_service:
        raise HTTPException(status_code=404, detail="Service not found")

    # Get reviews from database
    reviews = await get_feedback_for_service(service_id)
    
    # Get aggregate stats
    stats = await get_service_rating_summary(service_id)

    # Also try to get ERC-8004 on-chain reputation if seller has it
    onchain_reputation = None
    if db_service.provider_wallet:
        seller_8004 = await get_8004_credentials_simple(db_service.provider_wallet)
        if seller_8004 and seller_8004.get("agent_id"):
            try:
                onchain_reputation = get_reputation(seller_8004["agent_id"], tag="service")
            except Exception:
                pass

    return {
        "service_id": service_id,
        "average_rating": stats["average_rating"],
        "review_count": stats["review_count"],
        "verified_purchases_only": True,
        "reviews": [
            {
                "id": r.id,
                "rating": r.rating,
                "comment": r.comment,
                "reviewer": r.agent_name,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in reviews[:20]  # Limit to 20 most recent
        ],
        "onchain_reputation": onchain_reputation,
    }


# ============ STATS ============


@app.get("/stats")
@limiter.limit(RATE_LIMIT_READ)
async def get_stats(request: Request):
    """Marketplace statistics (rate limited: 120/min)"""
    all_db_services = await get_all_services()
    total_agents = await count_agents()

    return {
        "total_services": len(all_db_services),
        "total_agents": total_agents,
        "total_providers": len(set(s.provider_name for s in all_db_services)),
        "categories": len(set(s.category for s in all_db_services)),
        "total_calls": sum(s.calls_count or 0 for s in all_db_services),
        "total_revenue_usdc": sum(s.revenue_usdc or 0 for s in all_db_services),
    }


# ============ PROXY ENDPOINT ============


def generate_hmac_signature(body: str, timestamp: int, service_id: str, secret_token: str) -> str:
    """Generate HMAC-SHA256 signature for request verification"""
    message = f"{body}|{timestamp}|{service_id}"
    return hmac.new(secret_token.encode(), message.encode(), hashlib.sha256).hexdigest()


@app.post("/services/{service_id}/call")
async def call_service(service_id: str, request: Request, agent: Agent = Depends(require_agent)):
    """
    Call a service through MoltMart's proxy.

    🔐 Requires authentication (X-API-Key header)
    💰 Requires x402 payment to seller's wallet

    Flow:
    1. Buyer calls this endpoint
    2. If no payment: returns 402 with payment instructions (payTo = seller wallet)
    3. Buyer signs x402 payment and retries with X-Payment header
    4. MoltMart verifies payment via facilitator
    5. MoltMart forwards to seller's endpoint with HMAC signature
    6. Response returned to buyer

    Headers sent to seller:
    - X-MoltMart-Token: Secret token for basic auth
    - X-MoltMart-Signature: HMAC-SHA256(body|timestamp|service_id, secret_token)
    - X-MoltMart-Timestamp: Unix timestamp (verify within 60s)
    - X-MoltMart-Buyer: Buyer's wallet address
    - X-MoltMart-Tx: Transaction ID for audit
    """
    # Get service from database
    service = await get_service(service_id)
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")

    # Check if service has an endpoint
    if not service.endpoint_url:
        raise HTTPException(status_code=400, detail="This service does not have a callable endpoint")

    # ============ x402 PAYMENT VERIFICATION ============

    # Get the full URL for the resource
    resource_url = str(request.url)

    # Check for X-Payment header
    payment_header = request.headers.get("X-Payment")

    if not payment_header:
        # No payment - return 402 with requirements
        # Payment goes to MoltMart, we forward to seller minus fee
        return JSONResponse(
            status_code=402,
            content={
                "error": "Payment Required",
                "x402Version": 1,
                "accepts": [
                    {
                        "scheme": "exact",
                        "network": NETWORK,
                        "maxAmountRequired": str(int(service.price_usdc * 1_000_000)),  # USDC has 6 decimals
                        "resource": resource_url,
                        "description": f"Payment for service: {service.name}",
                        "mimeType": "application/json",
                        "payTo": MOLTMART_WALLET,  # Payment to us, we forward to seller
                        "maxTimeoutSeconds": 300,
                        "asset": USDC_CONTRACT,
                        "extra": {
                            "name": "USD Coin",
                            "decimals": 6,
                            "seller_wallet": service.provider_wallet,  # For forwarding
                            "fee_bps": 300,  # 3% fee
                        },
                    }
                ],
            },
            headers={
                "X-Payment-Required": "true",
            },
        )

    # Payment header exists - verify it via facilitator
    try:
        # Decode the payment payload from base64
        payment_payload_json = base64.b64decode(payment_header).decode("utf-8")
        payment_payload = json.loads(payment_payload_json)

        # Build requirements for verification
        # Payment goes to MoltMart, facilitator forwards to seller minus fee
        payment_requirements = {
            "scheme": "exact",
            "network": NETWORK,
            "maxAmountRequired": str(int(service.price_usdc * 1_000_000)),
            "resource": resource_url,
            "payTo": MOLTMART_WALLET,  # Payment to us
            "maxTimeoutSeconds": 300,
            "asset": USDC_CONTRACT,
            "extra": {
                "seller_wallet": service.provider_wallet,
                "fee_bps": 300,  # 3% = 300 basis points
            },
        }

        # Verify and settle via facilitator
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Step 1: Verify payment
            verify_response = await client.post(
                f"{FACILITATOR_URL}/verify",
                json={
                    "paymentPayload": payment_payload,
                    "paymentRequirements": payment_requirements,
                },
            )

            if verify_response.status_code != 200:
                return JSONResponse(
                    status_code=402,
                    content={
                        "error": "Payment verification failed",
                        "detail": verify_response.text,
                    },
                )

            verify_result = verify_response.json()
            if not verify_result.get("isValid", False):
                return JSONResponse(
                    status_code=402,
                    content={
                        "error": "Payment invalid",
                        "reason": verify_result.get("invalidReason", "Unknown"),
                    },
                )

            # Step 2: Settle payment (submit to blockchain)
            settle_response = await client.post(
                f"{FACILITATOR_URL}/settle",
                json={
                    "paymentPayload": payment_payload,
                    "paymentRequirements": payment_requirements,
                },
            )

            if settle_response.status_code == 200:
                settle_result = settle_response.json()
                if not settle_result.get("success"):
                    return JSONResponse(
                        status_code=402,
                        content={
                            "error": "Payment settlement failed",
                            "reason": settle_result.get("errorReason", "Unknown"),
                        },
                    )
                # Payment settled on-chain! Continue with request
            else:
                return JSONResponse(
                    status_code=402,
                    content={
                        "error": "Payment settlement error",
                        "detail": settle_response.text,
                    },
                )

    except Exception as e:
        return JSONResponse(
            status_code=402,
            content={
                "error": "Payment processing error",
                "detail": str(e),
            },
        )

    # ============ PAYMENT VERIFIED - PROCEED WITH REQUEST ============

    # Get request body
    try:
        body = await request.body()
        body_str = body.decode("utf-8") if body else ""
    except Exception:
        body_str = ""

    # Generate transaction ID
    tx_id = f"mm_tx_{secrets.token_urlsafe(16)}"
    timestamp = int(time.time())

    # Generate HMAC signature
    # The seller can verify this to ensure the request came from MoltMart
    signature = generate_hmac_signature(
        body_str,
        timestamp,
        service_id,
        service.secret_token_hash,  # Using the stored hash as the shared secret
    )

    # Prepare headers for seller
    headers = {
        "Content-Type": "application/json",
        "X-MoltMart-Token": service.secret_token_hash[:32],  # Partial token for basic auth
        "X-MoltMart-Signature": signature,
        "X-MoltMart-Timestamp": str(timestamp),
        "X-MoltMart-Buyer": agent.wallet_address,
        "X-MoltMart-Buyer-Name": agent.name,
        "X-MoltMart-Tx": tx_id,
        "X-MoltMart-Service": service_id,
    }

    # Create transaction record for database
    tx_record = TransactionDB(
        id=tx_id,
        service_id=service_id,
        service_name=service.name,
        buyer_wallet=agent.wallet_address.lower(),
        buyer_name=agent.name,
        seller_wallet=service.provider_wallet.lower(),
        price_usdc=service.price_usdc,
        status="pending",
    )

    # Forward request to seller's endpoint
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                str(service.endpoint_url),
                content=body,
                headers=headers,
            )

        # Update transaction status
        tx_record.status = "completed" if response.status_code == 200 else "failed"
        tx_record.seller_response_code = response.status_code

        # Update service stats in database
        if response.status_code == 200:
            await update_service_stats(service_id, calls_delta=1, revenue_delta=service.price_usdc)
        else:
            await update_service_stats(service_id, calls_delta=1, revenue_delta=0)

        # Log transaction to database
        await log_transaction(tx_record)

        # Return seller's response to buyer
        return Response(
            content=response.content,
            status_code=response.status_code,
            headers={
                "X-MoltMart-Tx": tx_id,
                "X-MoltMart-Price": str(service.price_usdc),
                "X-MoltMart-Seller": service.provider_wallet,
            },
            media_type=response.headers.get("content-type", "application/json"),
        )

    except httpx.TimeoutException as e:
        tx_record.status = "timeout"
        await log_transaction(tx_record)
        raise HTTPException(
            status_code=504,
            detail={
                "error": "Seller endpoint timed out",
                "tx_id": tx_id,
                "service_id": service_id,
            },
        ) from e
    except httpx.RequestError as e:
        tx_record.status = "error"
        tx_record.error = str(e)
        await log_transaction(tx_record)
        raise HTTPException(
            status_code=502,
            detail={
                "error": "Failed to reach seller endpoint",
                "tx_id": tx_id,
                "service_id": service_id,
                "message": str(e),
            },
        ) from e


class ServiceCallOnchainRequest(BaseModel):
    """Request body for on-chain service call"""
    tx_hash: str  # USDC payment to seller transaction hash
    request_data: dict | None = None  # Optional data to forward to seller
    
    @validator("tx_hash")
    def validate_tx_hash(cls, v):
        if not re.match(r"^0x[a-fA-F0-9]{64}$", v):
            raise ValueError("Invalid transaction hash format")
        return v.lower()


@app.post("/services/{service_id}/call/onchain")
async def call_service_onchain(
    service_id: str, 
    call_request: ServiceCallOnchainRequest,
    agent: Agent = Depends(require_agent)
):
    """
    Call a service using on-chain USDC payment (alternative to x402).
    
    **For custodial wallets (Bankr) that can't sign x402 payments.**
    
    Flow:
    1. GET /payment/challenge?action=call&service_id={id}&wallet_address=0x...
    2. Send the service price in USDC to the seller's wallet on Base
    3. POST /services/{id}/call/onchain with tx_hash and optional request_data
    
    The payment goes directly to the seller - MoltMart just verifies it happened.
    """
    # Get service
    service = await get_service(service_id)
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")
    
    if not service.endpoint_url:
        raise HTTPException(status_code=400, detail="This service does not have a callable endpoint")
    
    # Verify on-chain USDC payment to seller
    success, error = await verify_usdc_payment(
        agent.wallet_address, 
        call_request.tx_hash, 
        service.price_usdc, 
        "call",
        service_id=service_id
    )
    if not success:
        raise HTTPException(status_code=400, detail=f"Payment verification failed: {error}")
    
    print(f"✅ On-chain USDC payment verified: {agent.name} paid {service.price_usdc} USDC to {service.provider_wallet}")
    
    # ============ PAYMENT VERIFIED - FORWARD TO SELLER ============
    
    body_str = json.dumps(call_request.request_data) if call_request.request_data else ""
    
    # Generate transaction ID
    tx_id = f"mm_tx_{secrets.token_urlsafe(16)}"
    timestamp = int(time.time())
    
    # Generate HMAC signature
    signature = generate_hmac_signature(
        body_str,
        timestamp,
        service_id,
        service.secret_token_hash,
    )
    
    # Prepare headers for seller
    headers = {
        "Content-Type": "application/json",
        "X-MoltMart-Token": service.secret_token_hash[:32],
        "X-MoltMart-Signature": signature,
        "X-MoltMart-Timestamp": str(timestamp),
        "X-MoltMart-Buyer": agent.wallet_address,
        "X-MoltMart-Buyer-Name": agent.name,
        "X-MoltMart-Tx": tx_id,
        "X-MoltMart-Service": service_id,
        "X-MoltMart-Payment-Method": "onchain",  # Indicate this was an on-chain payment
    }
    
    # Create transaction record for database
    tx_record = TransactionDB(
        id=tx_id,
        service_id=service_id,
        service_name=service.name,
        buyer_wallet=agent.wallet_address.lower(),
        buyer_name=agent.name,
        seller_wallet=service.provider_wallet.lower(),
        price_usdc=service.price_usdc,
        status="pending",
    )
    
    # Forward request to seller's endpoint
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                str(service.endpoint_url),
                content=body_str.encode() if body_str else b"",
                headers=headers,
            )
        
        # Update transaction status
        tx_record.status = "completed" if response.status_code == 200 else "failed"
        tx_record.seller_response_code = response.status_code
        
        # Update service stats
        if response.status_code == 200:
            await update_service_stats(service_id, calls_delta=1, revenue_delta=service.price_usdc)
        else:
            await update_service_stats(service_id, calls_delta=1, revenue_delta=0)
        
        # Log to database
        await log_transaction(tx_record)
        
        return Response(
            content=response.content,
            status_code=response.status_code,
            headers={
                "X-MoltMart-Tx": tx_id,
                "X-MoltMart-Price": str(service.price_usdc),
                "X-MoltMart-Seller": service.provider_wallet,
            },
            media_type=response.headers.get("content-type", "application/json"),
        )
    
    except httpx.TimeoutException as e:
        tx_record.status = "timeout"
        await log_transaction(tx_record)
        raise HTTPException(status_code=504, detail={"error": "Seller endpoint timed out", "tx_id": tx_id}) from e
    except httpx.RequestError as e:
        tx_record.status = "error"
        tx_record.error = str(e)
        await log_transaction(tx_record)
        raise HTTPException(status_code=502, detail={"error": "Failed to reach seller endpoint", "tx_id": tx_id}) from e


@app.get("/transactions/mine")
async def get_my_transactions(agent: Agent = Depends(require_agent), limit: int = 20):
    """Get your recent transactions (as buyer or seller)"""
    transactions = await get_transactions_by_wallet(agent.wallet_address, limit=limit)
    return {
        "transactions": [
            {
                "id": tx.id,
                "service_id": tx.service_id,
                "service_name": tx.service_name,
                "buyer_wallet": tx.buyer_wallet,
                "buyer_name": tx.buyer_name,
                "seller_wallet": tx.seller_wallet,
                "price_usdc": tx.price_usdc,
                "status": tx.status,
                "created_at": tx.created_at.isoformat() if tx.created_at else None,
            }
            for tx in transactions
        ],
        "total": len(transactions),
    }


# ============ ADMIN ENDPOINTS ============


@app.delete("/admin/agents/{wallet}")
async def admin_delete_agent(wallet: str, x_admin_key: str = Header(None)):
    """
    Delete an agent registration (admin only).
    Used for testing - allows re-registration of same wallet.
    """
    admin_key = os.getenv("ADMIN_KEY", "test-admin-key")
    if x_admin_key != admin_key:
        raise HTTPException(status_code=403, detail="Invalid admin key")

    deleted = await delete_agent_by_wallet(wallet)
    if deleted:
        return {"status": "deleted", "wallet": wallet}
    raise HTTPException(status_code=404, detail="Agent not found")


@app.get("/admin/economics")
async def admin_get_economics(x_admin_key: str = Header(None)):
    """
    Get unit economics data for ERC-8004 minting.
    Shows total revenue, costs, and profit from identity minting.
    """
    admin_key = os.getenv("ADMIN_KEY", "test-admin-key")
    if x_admin_key != admin_key:
        raise HTTPException(status_code=403, detail="Invalid admin key")

    from database import get_mint_economics, get_recent_mints

    economics = await get_mint_economics()
    recent = await get_recent_mints(limit=10)

    return {
        "summary": economics,
        "recent_mints": [
            {
                "recipient": m.recipient_wallet[:10] + "...",
                "agent_id": m.agent_id,
                "revenue_usd": m.revenue_usdc,
                "cost_usd": m.total_cost_usd,
                "profit_usd": m.profit_usd,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in recent
        ],
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
