/**
 * MoltMart x402 Facilitator
 * 
 * Verifies and settles x402 payments on Base (mainnet or testnet).
 * Based on arc-merchant facilitator by @ortegarod
 */

import { x402Facilitator } from "@x402/core/facilitator";
import {
  PaymentPayload,
  PaymentRequirements,
  SettleResponse,
  VerifyResponse,
} from "@x402/core/types";
import { toFacilitatorEvmSigner } from "@x402/evm";
import { registerExactEvmScheme } from "@x402/evm/exact/facilitator";
import dotenv from "dotenv";
import express from "express";
import { createPublicClient, createWalletClient, http, Abi, Chain } from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { base, baseSepolia } from "viem/chains";

dotenv.config();

// Global error handlers to prevent crashes
process.on('unhandledRejection', (reason, promise) => {
  console.error('❌ Unhandled Rejection:', reason);
});

process.on('uncaughtException', (error) => {
  console.error('❌ Uncaught Exception:', error);
});

// Network configuration - set USE_TESTNET=true for Base Sepolia
const USE_TESTNET = process.env.USE_TESTNET?.toLowerCase() === "true";

const PORT = process.env.PORT || "4022";

// Configure based on network
let RPC_URL: string;
let BASE_NETWORK: string;
let BASE_USDC: string;
let CHAIN: Chain;

if (USE_TESTNET) {
  // Base Sepolia (Testnet)
  RPC_URL = process.env.RPC_URL || "https://sepolia.base.org";
  BASE_NETWORK = "eip155:84532";
  BASE_USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"; // Circle's testnet USDC
  CHAIN = baseSepolia;
  console.log("🧪 Facilitator: Using Base Sepolia TESTNET");
} else {
  // Base Mainnet
  RPC_URL = process.env.RPC_URL || "https://mainnet.base.org";
  BASE_NETWORK = "eip155:8453";
  BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"; // USDC on Base
  CHAIN = base;
  console.log("🔴 Facilitator: Using Base MAINNET");
}

// Facilitator private key (for settling transactions)
const FACILITATOR_PRIVATE_KEY = process.env.FACILITATOR_PRIVATE_KEY;

if (!FACILITATOR_PRIVATE_KEY) {
  console.error("❌ FACILITATOR_PRIVATE_KEY environment variable is required");
  console.error("   This wallet will pay gas for settlement transactions");
  process.exit(1);
}

// Create viem clients
const account = privateKeyToAccount(FACILITATOR_PRIVATE_KEY as `0x${string}`);

const publicClient = createPublicClient({
  chain: CHAIN,
  transport: http(RPC_URL),
});

const walletClient = createWalletClient({
  account,
  chain: CHAIN,
  transport: http(RPC_URL),
});

console.log(`🌐 RPC: ${RPC_URL}`);

console.log(`🔐 Facilitator wallet: ${account.address}`);

// ERC20 ABI for USDC transfers
const ERC20_ABI = [
  {
    name: "transfer",
    type: "function",
    inputs: [
      { name: "to", type: "address" },
      { name: "amount", type: "uint256" },
    ],
    outputs: [{ name: "", type: "bool" }],
  },
  {
    name: "balanceOf",
    type: "function",
    inputs: [{ name: "account", type: "address" }],
    outputs: [{ name: "", type: "uint256" }],
  },
] as const;

// Helper to transfer USDC to seller (after we receive payment)
async function forwardToSeller(
  sellerWallet: string,
  amountMicroUsdc: bigint,
  feeBps: number = 300 // 3% default
): Promise<{ success: boolean; txHash?: string; error?: string }> {
  try {
    // Calculate fee and seller amount
    const feeAmount = (amountMicroUsdc * BigInt(feeBps)) / BigInt(10000);
    const sellerAmount = amountMicroUsdc - feeAmount;
    
    console.log(`💸 Forwarding to seller:`);
    console.log(`   Total: ${amountMicroUsdc} (${Number(amountMicroUsdc) / 1_000_000} USDC)`);
    console.log(`   Fee (${feeBps / 100}%): ${feeAmount} (${Number(feeAmount) / 1_000_000} USDC)`);
    console.log(`   Seller gets: ${sellerAmount} (${Number(sellerAmount) / 1_000_000} USDC)`);
    console.log(`   Seller wallet: ${sellerWallet}`);

    // Transfer USDC to seller
    const txHash = await walletClient.writeContract({
      address: BASE_USDC as `0x${string}`,
      abi: ERC20_ABI,
      functionName: "transfer",
      args: [sellerWallet as `0x${string}`, sellerAmount],
    });

    console.log(`✅ Forwarded to seller: ${txHash}`);
    return { success: true, txHash };
  } catch (error) {
    console.error("❌ Forward to seller failed:", error);
    return { 
      success: false, 
      error: error instanceof Error ? error.message : "Unknown error" 
    };
  }
}

// Create EVM signer for x402
const evmSigner = toFacilitatorEvmSigner({
  address: account.address,

  // Read operations
  getCode: (args: { address: `0x${string}` }) => publicClient.getCode(args),

  readContract: (args: {
    address: `0x${string}`;
    abi: readonly unknown[];
    functionName: string;
    args?: readonly unknown[];
  }) =>
    publicClient.readContract({
      ...args,
      args: args.args || [],
    } as any),

  verifyTypedData: (args: {
    address: `0x${string}`;
    domain: Record<string, unknown>;
    types: Record<string, unknown>;
    primaryType: string;
    message: Record<string, unknown>;
    signature: `0x${string}`;
  }) => publicClient.verifyTypedData(args as any),

  // Write operations - with nonce management to avoid race conditions
  writeContract: async (args: {
    address: `0x${string}`;
    abi: readonly unknown[];
    functionName: string;
    args: readonly unknown[];
  }): Promise<`0x${string}`> => {
    // Get pending nonce to avoid "nonce too low" errors
    const nonce = await publicClient.getTransactionCount({
      address: account.address,
      blockTag: 'pending',
    });
    console.log(`📝 writeContract nonce (pending): ${nonce}`);
    
    const hash = await walletClient.writeContract({
      address: args.address,
      abi: args.abi as Abi,
      functionName: args.functionName,
      args: args.args as any[],
      nonce,
    });
    return hash;
  },

  sendTransaction: async (args: {
    to: `0x${string}`;
    data: `0x${string}`;
  }): Promise<`0x${string}`> => {
    // Get pending nonce to avoid "nonce too low" errors
    const nonce = await publicClient.getTransactionCount({
      address: account.address,
      blockTag: 'pending',
    });
    console.log(`📝 sendTransaction nonce (pending): ${nonce}`);
    
    const hash = await walletClient.sendTransaction({
      to: args.to,
      data: args.data,
      nonce,
    });
    return hash;
  },

  waitForTransactionReceipt: async (args: { hash: `0x${string}` }) => {
    return publicClient.waitForTransactionReceipt(args);
  },
});

// Initialize facilitator with logging hooks
const facilitator = new x402Facilitator()
  .onBeforeVerify(async (context) => {
    console.log("📝 Verifying payment...", JSON.stringify(context, null, 2));
  })
  .onAfterVerify(async (context) => {
    console.log("✅ Payment verified");
  })
  .onVerifyFailure(async (context) => {
    console.log("❌ Verify failed:", context);
  })
  .onBeforeSettle(async (context) => {
    console.log("💸 Settling payment...");
  })
  .onAfterSettle(async (context) => {
    console.log("✅ Payment settled");
  })
  .onSettleFailure(async (context) => {
    console.log("❌ Settlement failed:", context);
  });

// Register Base network with our signer
registerExactEvmScheme(facilitator, {
  signer: evmSigner,
  networks: BASE_NETWORK as `eip155:${number}`,
  deployERC4337WithEIP6492: false,
});

console.log(`📡 Registered network: ${BASE_NETWORK} (Base ${USE_TESTNET ? "Sepolia" : "Mainnet"})`);

// Express app
const app = express();
app.use(express.json());

// CORS for MoltMart frontend - restrict to known origins
const ALLOWED_ORIGINS = (process.env.ALLOWED_ORIGINS || "https://moltmart.app,http://localhost:3000").split(",");

app.use((req, res, next) => {
  const origin = req.headers.origin;
  if (origin && ALLOWED_ORIGINS.includes(origin)) {
    res.header("Access-Control-Allow-Origin", origin);
  }
  res.header("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
  res.header("Access-Control-Allow-Headers", "Content-Type, X-Payment, X-Payment-Response");
  if (req.method === "OPTIONS") {
    return res.sendStatus(200);
  }
  next();
});

// POST /verify
app.post("/verify", async (req, res) => {
  try {
    const { paymentPayload, paymentRequirements } = req.body as {
      paymentPayload: PaymentPayload;
      paymentRequirements: PaymentRequirements;
    };

    if (!paymentPayload || !paymentRequirements) {
      return res.status(400).json({
        error: "Missing paymentPayload or paymentRequirements",
      });
    }

    const response: VerifyResponse = await facilitator.verify(
      paymentPayload,
      paymentRequirements
    );
    res.json(response);
  } catch (error) {
    console.error("Verify error:", error);
    res.status(500).json({
      error: error instanceof Error ? error.message : "Unknown error",
    });
  }
});

// POST /settle
app.post("/settle", async (req, res) => {
  try {
    const { paymentPayload, paymentRequirements } = req.body;

    if (!paymentPayload || !paymentRequirements) {
      return res.status(400).json({
        error: "Missing paymentPayload or paymentRequirements",
      });
    }

    // Settle the payment (buyer → MoltMart)
    const response: SettleResponse = await facilitator.settle(
      paymentPayload as PaymentPayload,
      paymentRequirements as PaymentRequirements
    );

    // If settlement succeeded and there's a seller to forward to
    if (response.success && paymentRequirements.extra?.seller_wallet) {
      const sellerWallet = paymentRequirements.extra.seller_wallet as string;
      const feeBps = (paymentRequirements.extra.fee_bps as number) || 300;
      const amountMicroUsdc = BigInt(paymentRequirements.maxAmountRequired);

      console.log(`🔄 Settlement succeeded, forwarding to seller...`);
      
      const forwardResult = await forwardToSeller(sellerWallet, amountMicroUsdc, feeBps);
      
      if (!forwardResult.success) {
        console.error(`❌ Forward failed but settlement succeeded. Manual intervention needed.`);
        console.error(`   Seller: ${sellerWallet}, Amount: ${amountMicroUsdc}`);
        // Still return success since buyer payment went through
        // TODO: Add to a retry queue for forwarding
      }

      // Add forward info to response
      (response as any).forward = {
        seller_wallet: sellerWallet,
        seller_amount: Number(amountMicroUsdc - (amountMicroUsdc * BigInt(feeBps)) / BigInt(10000)) / 1_000_000,
        fee_amount: Number((amountMicroUsdc * BigInt(feeBps)) / BigInt(10000)) / 1_000_000,
        fee_percent: feeBps / 100,
        forward_tx: forwardResult.txHash,
        forward_success: forwardResult.success,
      };
    }

    res.json(response);
  } catch (error) {
    console.error("Settle error:", error);

    if (error instanceof Error && error.message.includes("Settlement aborted:")) {
      return res.json({
        success: false,
        errorReason: error.message.replace("Settlement aborted: ", ""),
        network: req.body?.paymentPayload?.network || "unknown",
      } as SettleResponse);
    }

    res.status(500).json({
      error: error instanceof Error ? error.message : "Unknown error",
    });
  }
});

// GET /supported
app.get("/supported", async (req, res) => {
  try {
    const response = facilitator.getSupported();
    res.json(response);
  } catch (error) {
    console.error("Supported error:", error);
    res.status(500).json({
      error: error instanceof Error ? error.message : "Unknown error",
    });
  }
});

// Simple ping - no dependencies, just confirms Express is routing
app.get("/ping", (_req, res) => {
  res.send("pong");
});

// Health check
app.get("/health", async (_req, res) => {
  res.json({
    status: "ok",
    network: BASE_NETWORK,
    wallet: account.address,
    usdc: BASE_USDC,
  });
});

// Root
app.get("/", async (_req, res) => {
  res.json({
    name: "MoltMart x402 Facilitator",
    network: BASE_NETWORK,
    endpoints: ["POST /verify", "POST /settle", "GET /supported", "GET /health"],
  });
});

// Start server - bind to 0.0.0.0 explicitly for Railway
app.listen(parseInt(PORT), "0.0.0.0", () => {
  console.log(`\n🚀 MoltMart x402 Facilitator running on http://localhost:${PORT}`);
  console.log(`   Network: ${BASE_NETWORK} (Base ${USE_TESTNET ? "Sepolia" : "Mainnet"})`);
  console.log(`   Wallet: ${account.address}`);
  console.log(`   USDC: ${BASE_USDC}`);
  console.log(`   Endpoints: POST /verify, POST /settle, GET /supported, GET /health\n`);
});
