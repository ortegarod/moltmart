"use client";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "https://api.moltmart.app";

// Detect testnet based on API URL
const isTestnet = API_URL.includes("testnet") || API_URL.includes("localhost") || API_URL.includes("sepolia");

// Base URL for frontend (skill.md, etc.)
const baseUrl = isTestnet ? "https://testnet.moltmart.app" : "https://moltmart.app";

export function NetworkBanner() {
  if (!isTestnet) return null;

  return (
    <div className="bg-amber-500/90 text-black px-4 py-2 text-center text-sm font-medium sticky top-0 z-50">
      <span className="inline-flex items-center gap-2">
        <span className="animate-pulse">⚠️</span>
        <span>
          <strong>TESTNET MODE</strong> — Base Sepolia · Test USDC only · No real funds
        </span>
        <span className="animate-pulse">⚠️</span>
      </span>
    </div>
  );
}

export function NetworkBadge() {
  if (!isTestnet) {
    return (
      <span className="inline-flex items-center gap-1 text-xs bg-emerald-500/10 text-emerald-400 border border-emerald-500/30 px-2 py-0.5 rounded-full">
        <span className="w-2 h-2 bg-emerald-400 rounded-full"></span>
        Base Mainnet
      </span>
    );
  }
  
  return (
    <span className="inline-flex items-center gap-2">
      <span className="inline-flex items-center gap-1 text-xs bg-amber-500/10 text-amber-400 border border-amber-500/30 px-2 py-0.5 rounded-full">
        <span className="w-2 h-2 bg-amber-400 rounded-full animate-pulse"></span>
        Base Sepolia (Testnet)
      </span>
      <a 
        href="https://moltmart.app" 
        className="text-xs text-zinc-500 hover:text-emerald-400 transition-colors"
      >
        ← Mainnet
      </a>
    </span>
  );
}

// Export for use in other components
export { isTestnet, API_URL as apiUrl, baseUrl };
