"""
Database layer for MoltMart
Uses SQLAlchemy with async support for PostgreSQL/SQLite
"""

import asyncio
import logging
import os
import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String, Text, func, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.future import select
from sqlalchemy.orm import declarative_base, sessionmaker

# Configure logging
logger = logging.getLogger(__name__)

# ============ DATABASE CONFIGURATION ============


def _get_database_url() -> str:
    """
    Get and normalize the database URL.

    Railway uses postgres:// but SQLAlchemy needs postgresql+asyncpg://
    Falls back to SQLite for local development.
    """
    url = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./moltmart.db")

    # Normalize PostgreSQL URLs for asyncpg
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgresql://") and "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)

    return url


def _sanitize_url(url: str) -> str:
    """Remove password from URL for safe logging."""
    return re.sub(r':([^:@]+)@', ':***@', url)


DATABASE_URL = _get_database_url()
IS_POSTGRES = "postgresql" in DATABASE_URL

logger.info(f"Database: {_sanitize_url(DATABASE_URL)}")


def _create_engine():
    """
    Create the async database engine with appropriate settings.

    PostgreSQL (production):
    - Small connection pool for Railway free tier
    - Connection recycling to prevent stale connections
    - Pre-ping to detect dead connections

    SQLite (development):
    - Simple settings, no pooling needed
    """
    if IS_POSTGRES:
        return create_async_engine(
            DATABASE_URL,
            echo=False,
            # Connection pool settings
            pool_pre_ping=True,      # Test connections before use
            pool_size=3,             # Small pool for Railway free tier
            max_overflow=2,          # Max 5 total connections
            pool_timeout=30,         # Wait max 30s for connection
            pool_recycle=300,        # Refresh connections every 5 min
            # asyncpg-specific settings
            connect_args={
                "command_timeout": 30,
                "server_settings": {"application_name": "moltmart"},
                "ssl": "prefer"  # Try SSL, fall back to non-SSL
            }
        )
    else:
        # SQLite for local development
        return create_async_engine(DATABASE_URL, echo=False)


engine = _create_engine()
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()


# ============ SESSION MANAGEMENT ============


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Async context manager for database sessions.

    Ensures proper cleanup even if an error occurs.
    Usage:
        async with get_session() as session:
            result = await session.execute(query)
    """
    session = AsyncSessionLocal()
    try:
        yield session
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def init_db(max_retries: int = 3, retry_delay: float = 5.0) -> None:
    """
    Initialize database tables with retry logic.

    Railway PostgreSQL can be slow to accept connections after restart,
    so we retry a few times before giving up.
    """
    for attempt in range(1, max_retries + 1):
        logger.info(f"Connecting to database (attempt {attempt}/{max_retries})...")
        try:
            async with asyncio.timeout(30):
                async with engine.begin() as conn:
                    await conn.run_sync(Base.metadata.create_all)
                    logger.info("Database tables created")
            # Run migrations for new columns (separate connection)
            async with engine.begin() as conn:
                await run_migrations(conn)
            logger.info("Database initialized successfully")
            return
        except TimeoutError:
            logger.warning(f"Connection attempt {attempt} timed out")
            if attempt < max_retries:
                logger.info(f"Retrying in {retry_delay}s...")
                await asyncio.sleep(retry_delay)
            else:
                logger.error("Database connection failed - all retries exhausted")
                raise
        except Exception as e:
            logger.warning(f"Connection attempt {attempt} failed: {e}")
            if attempt < max_retries:
                logger.info(f"Retrying in {retry_delay}s...")
                await asyncio.sleep(retry_delay)
            else:
                logger.error("Database connection failed - all retries exhausted")
                raise


async def run_migrations(conn) -> None:
    """
    Run database migrations for new columns.
    Uses IF NOT EXISTS to be idempotent.
    """
    if not IS_POSTGRES:
        return  # SQLite auto-handles this via create_all

    migrations = [
        # CRITICAL: endpoint_url was in model but never migrated (fixed 2026-02-05)
        "ALTER TABLE services ADD COLUMN IF NOT EXISTS endpoint_url VARCHAR",
        # Service storefront fields (added 2026-02-04)
        "ALTER TABLE services ADD COLUMN IF NOT EXISTS usage_instructions TEXT",
        "ALTER TABLE services ADD COLUMN IF NOT EXISTS input_schema TEXT",
        "ALTER TABLE services ADD COLUMN IF NOT EXISTS output_schema TEXT",
        "ALTER TABLE services ADD COLUMN IF NOT EXISTS example_request TEXT",
        "ALTER TABLE services ADD COLUMN IF NOT EXISTS example_response TEXT",
        # Soft delete (added 2026-02-05)
        "ALTER TABLE services ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP",
    ]

    for sql in migrations:
        try:
            await conn.execute(text(sql))
            logger.info(f"Migration executed: {sql[:60]}...")
        except Exception as e:
            # Log at WARNING so we can see if migrations are failing
            logger.warning(f"Migration failed: {sql[:60]}... Error: {e}")


# ============ MODELS ============


class AgentDB(Base):
    """Registered agent on MoltMart."""
    __tablename__ = "agents"

    id = Column(String, primary_key=True)
    api_key = Column(String, unique=True, index=True)
    name = Column(String, nullable=False)
    wallet_address = Column(String, nullable=False, index=True)
    description = Column(Text)
    moltx_handle = Column(String)
    github_handle = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    services_count = Column(Integer, default=0)

    # ERC-8004 identity
    has_8004 = Column(Boolean, default=False)
    agent_8004_id = Column(Integer)
    agent_8004_registry = Column(String)
    scan_url = Column(String)


class ServiceDB(Base):
    """Service listed on the marketplace."""
    __tablename__ = "services"

    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    description = Column(Text)
    endpoint_url = Column(String)
    price_usdc = Column(Float, nullable=False)
    category = Column(String, nullable=False, index=True)
    provider_name = Column(String, nullable=False)
    provider_wallet = Column(String, nullable=False, index=True)
    secret_token_hash = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    calls_count = Column(Integer, default=0)
    revenue_usdc = Column(Float, default=0.0)
    # Service storefront fields (optional - help buyers understand how to use)
    usage_instructions = Column(Text, nullable=True)  # Markdown: how to call this service
    input_schema = Column(Text, nullable=True)  # JSON Schema for request body
    output_schema = Column(Text, nullable=True)  # JSON Schema for response
    example_request = Column(Text, nullable=True)  # Example request JSON
    example_response = Column(Text, nullable=True)  # Example response JSON
    deleted_at = Column(DateTime, nullable=True)  # Soft delete timestamp


class TransactionDB(Base):
    """Service call transaction log."""
    __tablename__ = "transactions"

    id = Column(String, primary_key=True)
    service_id = Column(String, index=True)
    service_name = Column(String)
    buyer_wallet = Column(String, index=True)
    buyer_name = Column(String)
    seller_wallet = Column(String, index=True)
    price_usdc = Column(Float)
    status = Column(String)  # pending, completed, failed, timeout
    created_at = Column(DateTime, default=datetime.utcnow)
    seller_response_code = Column(Integer)
    error = Column(Text)


class FeedbackDB(Base):
    """Service feedback/reputation."""
    __tablename__ = "feedback"

    id = Column(String, primary_key=True)
    service_id = Column(String, index=True)
    agent_id = Column(String, index=True)
    agent_name = Column(String)
    rating = Column(Integer)  # 1-5
    comment = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)


class MintCostDB(Base):
    """ERC-8004 minting cost tracking for unit economics."""
    __tablename__ = "mint_costs"

    id = Column(String, primary_key=True)
    recipient_wallet = Column(String, nullable=False, index=True)
    agent_id = Column(Integer)

    # Revenue
    revenue_usdc = Column(Float, nullable=False)

    # Mint transaction costs
    mint_tx_hash = Column(String)
    mint_gas_used = Column(Integer)
    mint_gas_price_wei = Column(String)
    mint_cost_eth = Column(Float)

    # Transfer transaction costs
    transfer_tx_hash = Column(String)
    transfer_gas_used = Column(Integer)
    transfer_gas_price_wei = Column(String)
    transfer_cost_eth = Column(Float)

    # Totals
    total_cost_eth = Column(Float)
    total_cost_usd = Column(Float)
    profit_usd = Column(Float)
    eth_price_usd = Column(Float)

    created_at = Column(DateTime, default=datetime.utcnow)
    status = Column(String, default="completed")


# ============ AGENT OPERATIONS ============


async def get_agent_by_api_key(api_key: str) -> AgentDB | None:
    """Get agent by API key."""
    async with get_session() as session:
        result = await session.execute(
            select(AgentDB).where(AgentDB.api_key == api_key)
        )
        return result.scalar_one_or_none()


async def get_agent_by_wallet(wallet: str) -> AgentDB | None:
    """Get agent by wallet address."""
    async with get_session() as session:
        result = await session.execute(
            select(AgentDB).where(AgentDB.wallet_address == wallet.lower())
        )
        return result.scalar_one_or_none()


async def get_agent_by_id(agent_id: str) -> AgentDB | None:
    """Get agent by ID."""
    async with get_session() as session:
        result = await session.execute(
            select(AgentDB).where(AgentDB.id == agent_id)
        )
        return result.scalar_one_or_none()


async def get_agent_by_8004_id(erc8004_id: int) -> AgentDB | None:
    """Get agent by their ERC-8004 token ID."""
    async with get_session() as session:
        result = await session.execute(
            select(AgentDB).where(AgentDB.agent_8004_id == erc8004_id)
        )
        return result.scalar_one_or_none()


async def update_agent_8004_status(wallet: str, has_8004: bool, agent_8004_id: int | None = None, agent_8004_registry: str | None = None, scan_url: str | None = None) -> None:
    """Update an agent's ERC-8004 verification status."""
    async with get_session() as session:
        result = await session.execute(
            select(AgentDB).where(AgentDB.wallet_address == wallet.lower())
        )
        agent = result.scalar_one_or_none()
        if agent:
            agent.has_8004 = has_8004
            agent.agent_8004_id = agent_8004_id
            agent.agent_8004_registry = agent_8004_registry
            agent.scan_url = scan_url
            await session.commit()


async def get_agents(limit: int = 50, offset: int = 0) -> list[AgentDB]:
    """Get agents with pagination, newest first."""
    import time
    t0 = time.time()
    async with get_session() as session:
        t1 = time.time()
        result = await session.execute(
            select(AgentDB)
            .order_by(AgentDB.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        t2 = time.time()
        agents = list(result.scalars().all())
        t3 = time.time()
        print(f"  📊 get_agents breakdown: session={int((t1-t0)*1000)}ms, query={int((t2-t1)*1000)}ms, fetch={int((t3-t2)*1000)}ms")
        return agents


async def count_agents() -> int:
    """Count total registered agents."""
    async with get_session() as session:
        result = await session.execute(select(func.count(AgentDB.id)))
        return result.scalar() or 0


async def create_agent(agent: AgentDB) -> AgentDB:
    """Create a new agent."""
    async with get_session() as session:
        session.add(agent)
        await session.commit()
        await session.refresh(agent)
        return agent


async def delete_agent_by_wallet(wallet: str) -> bool:
    """Delete agent by wallet address (admin only)."""
    async with get_session() as session:
        result = await session.execute(
            select(AgentDB).where(AgentDB.wallet_address == wallet.lower())
        )
        agent = result.scalar_one_or_none()
        if agent:
            await session.delete(agent)
            await session.commit()
            return True
        return False


async def update_agent_api_key(wallet: str, new_api_key: str) -> bool:
    """Update an agent's API key (for key recovery)."""
    async with get_session() as session:
        result = await session.execute(
            select(AgentDB).where(AgentDB.wallet_address == wallet.lower())
        )
        agent = result.scalar_one_or_none()
        if agent:
            agent.api_key = new_api_key
            await session.commit()
            return True
        return False


# ============ SERVICE OPERATIONS ============


async def get_service(service_id: str) -> ServiceDB | None:
    """Get service by ID."""
    async with get_session() as session:
        result = await session.execute(
            select(ServiceDB).where(ServiceDB.id == service_id)
        )
        return result.scalar_one_or_none()


async def get_services(
    category: str | None = None,
    provider_wallet: str | None = None,
    limit: int = 50,
    offset: int = 0
) -> list[ServiceDB]:
    """Get services with optional filters (excludes deleted)."""
    async with get_session() as session:
        query = select(ServiceDB).where(ServiceDB.deleted_at.is_(None))
        if category:
            query = query.where(ServiceDB.category == category)
        if provider_wallet:
            query = query.where(ServiceDB.provider_wallet == provider_wallet.lower())
        query = query.order_by(ServiceDB.created_at.desc()).limit(limit).offset(offset)
        result = await session.execute(query)
        return list(result.scalars().all())


async def get_all_services() -> list[ServiceDB]:
    """Get all services (for stats, excludes deleted)."""
    async with get_session() as session:
        result = await session.execute(
            select(ServiceDB).where(ServiceDB.deleted_at.is_(None))
        )
        return list(result.scalars().all())


async def count_services() -> int:
    """Count total services."""
    async with get_session() as session:
        result = await session.execute(select(func.count(ServiceDB.id)))
        return result.scalar() or 0


async def create_service(service: ServiceDB) -> ServiceDB:
    """Create a new service."""
    async with get_session() as session:
        session.add(service)
        await session.commit()
        await session.refresh(service)
        return service


async def update_service_db(service_id: str, update_data: dict) -> ServiceDB | None:
    """Update a service with the provided fields."""
    async with get_session() as session:
        result = await session.execute(
            select(ServiceDB).where(ServiceDB.id == service_id)
        )
        service = result.scalar_one_or_none()
        if not service:
            return None

        for key, value in update_data.items():
            setattr(service, key, value)

        await session.commit()
        await session.refresh(service)
        return service


async def delete_service_db(service_id: str) -> bool:
    """Soft delete a service by setting deleted_at timestamp."""
    async with get_session() as session:
        result = await session.execute(
            select(ServiceDB).where(ServiceDB.id == service_id)
        )
        service = result.scalar_one_or_none()
        if not service:
            return False

        service.deleted_at = datetime.utcnow()
        await session.commit()
        return True


async def update_service_stats(
    service_id: str,
    calls_delta: int = 0,
    revenue_delta: float = 0.0
) -> None:
    """Update service call count and revenue."""
    async with get_session() as session:
        result = await session.execute(
            select(ServiceDB).where(ServiceDB.id == service_id)
        )
        service = result.scalar_one_or_none()
        if service:
            service.calls_count = (service.calls_count or 0) + calls_delta
            service.revenue_usdc = (service.revenue_usdc or 0) + revenue_delta
            await session.commit()


# ============ TRANSACTION LOGGING ============


async def log_transaction(tx: TransactionDB) -> None:
    """Log a service call transaction."""
    async with get_session() as session:
        session.add(tx)
        await session.commit()


# ============ MINT COST TRACKING ============


async def log_mint_cost(mint_cost: MintCostDB) -> MintCostDB:
    """Log a mint transaction with its costs."""
    async with get_session() as session:
        session.add(mint_cost)
        await session.commit()
        await session.refresh(mint_cost)
        return mint_cost


async def get_mint_economics() -> dict:
    """Get aggregate mint economics."""
    async with get_session() as session:
        result = await session.execute(
            select(
                func.count(MintCostDB.id).label("total_mints"),
                func.sum(MintCostDB.revenue_usdc).label("total_revenue"),
                func.sum(MintCostDB.total_cost_usd).label("total_cost"),
                func.sum(MintCostDB.profit_usd).label("total_profit"),
                func.avg(MintCostDB.total_cost_usd).label("avg_cost"),
                func.avg(MintCostDB.profit_usd).label("avg_profit"),
            ).where(MintCostDB.status == "completed")
        )
        row = result.one()
        return {
            "total_mints": row.total_mints or 0,
            "total_revenue_usd": float(row.total_revenue or 0),
            "total_cost_usd": float(row.total_cost or 0),
            "total_profit_usd": float(row.total_profit or 0),
            "avg_cost_per_mint_usd": float(row.avg_cost or 0),
            "avg_profit_per_mint_usd": float(row.avg_profit or 0),
        }


async def get_recent_mints(limit: int = 10) -> list[MintCostDB]:
    """Get recent mint transactions."""
    async with get_session() as session:
        result = await session.execute(
            select(MintCostDB)
            .order_by(MintCostDB.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())


async def get_token_id_from_mint_cache(wallet_address: str) -> int | None:
    """
    Fast lookup of ERC-8004 token ID from our mint cache.

    This is MUCH faster than querying blockchain events.
    Only works for tokens we minted, but that's most of them.
    """
    async with get_session() as session:
        result = await session.execute(
            select(MintCostDB.agent_id)
            .where(MintCostDB.recipient_wallet == wallet_address.lower())
            .where(MintCostDB.agent_id.isnot(None))
            .order_by(MintCostDB.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


# ============ PURCHASE VERIFICATION ============


async def has_purchased_service(buyer_wallet: str, service_id: str) -> bool:
    """Check if a buyer has completed a purchase of a service."""
    async with get_session() as session:
        result = await session.execute(
            select(TransactionDB)
            .where(TransactionDB.buyer_wallet == buyer_wallet.lower())
            .where(TransactionDB.service_id == service_id)
            .where(TransactionDB.status == "completed")
            .limit(1)
        )
        return result.scalar_one_or_none() is not None


async def get_purchase_count(buyer_wallet: str, service_id: str) -> int:
    """Get number of completed purchases by a buyer for a service."""
    async with get_session() as session:
        result = await session.execute(
            select(func.count(TransactionDB.id))
            .where(TransactionDB.buyer_wallet == buyer_wallet.lower())
            .where(TransactionDB.service_id == service_id)
            .where(TransactionDB.status == "completed")
        )
        return result.scalar() or 0


# ============ FEEDBACK/REVIEWS ============


async def create_feedback(feedback: FeedbackDB) -> FeedbackDB:
    """Create a new feedback entry."""
    async with get_session() as session:
        session.add(feedback)
        await session.commit()
        await session.refresh(feedback)
        return feedback


async def get_feedback_for_service(service_id: str) -> list[FeedbackDB]:
    """Get all feedback for a service."""
    async with get_session() as session:
        result = await session.execute(
            select(FeedbackDB)
            .where(FeedbackDB.service_id == service_id)
            .order_by(FeedbackDB.created_at.desc())
        )
        return list(result.scalars().all())


async def has_reviewed_service(agent_id: str, service_id: str) -> bool:
    """Check if an agent has already reviewed a service."""
    async with get_session() as session:
        result = await session.execute(
            select(FeedbackDB)
            .where(FeedbackDB.agent_id == agent_id)
            .where(FeedbackDB.service_id == service_id)
            .limit(1)
        )
        return result.scalar_one_or_none() is not None


async def get_service_rating_summary(service_id: str) -> dict:
    """Get aggregate rating for a service."""
    async with get_session() as session:
        result = await session.execute(
            select(
                func.count(FeedbackDB.id).label("count"),
                func.avg(FeedbackDB.rating).label("avg_rating")
            )
            .where(FeedbackDB.service_id == service_id)
        )
        row = result.one()
        return {
            "review_count": row.count or 0,
            "average_rating": round(float(row.avg_rating), 1) if row.avg_rating else None
        }


async def get_transactions_by_wallet(wallet_address: str, limit: int = 20) -> list[TransactionDB]:
    """Get transactions where wallet is buyer or seller."""
    wallet_lower = wallet_address.lower()
    async with get_session() as session:
        result = await session.execute(
            select(TransactionDB)
            .where(
                (TransactionDB.buyer_wallet == wallet_lower) |
                (TransactionDB.seller_wallet == wallet_lower)
            )
            .order_by(TransactionDB.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())
