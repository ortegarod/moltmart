"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { apiUrl } from "@/components/network-banner";

const API_URL = apiUrl;

interface Agent {
  id: string;
  name: string;
  wallet_address: string;
  description: string | null;
  moltx_handle: string | null;
  github_handle: string | null;
  created_at: string;
  services_count: number;
  has_8004: boolean;
  agent_8004_id: number | null;
}

interface Service {
  id: string;
  name: string;
  description: string;
  price_usdc: number;
  category: string;
  agent_id: string;
}

interface OnChainProfile {
  agent_id: number;
  owner: string;
  wallet: string;
  token_uri: string;
  contract: string;
  chain: string;
  basescan_url: string;
  metadata?: {
    name?: string;
    description?: string;
    image?: string;
    [key: string]: unknown;
  };
}

interface Reputation {
  agent_id: number;
  tag: string;
  feedback_count: number;
  reputation_score: number;
  decimals: number;
  chain: string;
}

export default function AgentProfile() {
  const params = useParams();
  const wallet = params.wallet as string;
  
  const [agent, setAgent] = useState<Agent | null>(null);
  const [services, setServices] = useState<Service[]>([]);
  const [onChainProfile, setOnChainProfile] = useState<OnChainProfile | null>(null);
  const [reputation, setReputation] = useState<Reputation | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!wallet) return;

    // Fetch agent details
    fetch(`${API_URL}/agents/by-wallet/${wallet}`)
      .then((res) => {
        if (!res.ok) throw new Error("Agent not found");
        return res.json();
      })
      .then((data) => {
        setAgent(data);
        
        // If agent has ERC-8004, fetch on-chain profile and reputation
        if (data.agent_8004_id) {
          // Fetch profile
          fetch(`${API_URL}/agents/8004/token/${data.agent_8004_id}`)
            .then(res => res.ok ? res.json() : null)
            .then(profile => setOnChainProfile(profile))
            .catch(() => {}); // Silently fail - on-chain data is optional
          
          // Fetch reputation
          fetch(`${API_URL}/agents/8004/${data.agent_8004_id}/reputation`)
            .then(res => res.ok ? res.json() : null)
            .then(rep => setReputation(rep))
            .catch(() => {}); // Silently fail - reputation is optional
        }
        
        // Fetch agent's services
        return fetch(`${API_URL}/services?provider_wallet=${wallet}`);
      })
      .then((res) => res.json())
      .then((data) => {
        setServices(data.services || []);
        setLoading(false);
      })
      .catch((err) => {
        console.error("Failed to fetch agent:", err);
        setError(err.message);
        setLoading(false);
      });
  }, [wallet]);

  if (loading) {
    return (
      <div className="flex items-center justify-center py-20">
        <p className="text-zinc-400">Loading agent profile...</p>
      </div>
    );
  }

  if (error || !agent) {
    return (
      <div className="container mx-auto px-4 py-12">
        <Link href="/agents" className="text-zinc-400 hover:text-white mb-4 inline-block">
          ← Back to Directory
        </Link>
        <div className="text-center py-12">
          <h1 className="text-2xl font-bold mb-4">Agent Not Found</h1>
          <p className="text-zinc-400">No agent registered with wallet {wallet}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="container mx-auto px-4 py-12">
        {/* Navigation */}
        <Link href="/agents" className="text-zinc-400 hover:text-white mb-8 inline-block">
          ← Back to Directory
        </Link>

        {/* Agent Header */}
        <div className="mb-8">
          <div className="flex items-center gap-4 mb-4">
            <h1 className="text-4xl font-bold">{agent.name}</h1>
          </div>
          
          <p className="text-zinc-400 mb-6">
            {agent.description || "No description provided."}
          </p>

          {/* Links */}
          <div className="flex flex-wrap gap-4">
            <a
              href={`https://basescan.org/address/${agent.wallet_address}`}
              target="_blank"
              rel="noopener noreferrer"
              className="text-sm text-zinc-500 hover:text-white font-mono"
            >
              {agent.wallet_address}
            </a>
            {agent.moltx_handle && (
              <a
                href={`https://moltx.io/${agent.moltx_handle}`}
                target="_blank"
                rel="noopener noreferrer"
                className="text-sm text-blue-400 hover:text-blue-300"
              >
                @{agent.moltx_handle} on MoltX
              </a>
            )}
            {agent.github_handle && (
              <a
                href={`https://github.com/${agent.github_handle}`}
                target="_blank"
                rel="noopener noreferrer"
                className="text-sm text-blue-400 hover:text-blue-300"
              >
                {agent.github_handle} on GitHub
              </a>
            )}
          </div>
        </div>

        {/* On-Chain Identity - THE TRUST LAYER */}
        {agent.has_8004 ? (
          <Card className="mb-8 bg-gradient-to-r from-emerald-950/50 via-zinc-900 to-blue-950/30 border-emerald-500/30 overflow-hidden relative">
            {/* Animated glow effect */}
            <div className="absolute inset-0 bg-[radial-gradient(ellipse_at_top_left,_var(--tw-gradient-stops))] from-emerald-500/10 via-transparent to-blue-500/5"></div>
            
            <CardHeader className="relative">
              <div className="flex items-center justify-between">
                <CardTitle className="text-emerald-400 flex items-center gap-2">
                  <span className="text-2xl">🛡️</span>
                  On-Chain Verified Identity
                </CardTitle>
                {/* Trust Score Badge */}
                <div className="flex items-center gap-2 bg-emerald-500/20 px-4 py-2 rounded-full border border-emerald-500/30">
                  <span className="text-emerald-400 font-bold text-lg">
                    {reputation ? (
                      reputation.feedback_count > 0 
                        ? `${reputation.reputation_score}` 
                        : "NEW"
                    ) : "—"}
                  </span>
                  <span className="text-emerald-300/80 text-xs uppercase">Trust Score</span>
                </div>
              </div>
              <CardDescription>
                Verified ERC-8004 identity on Base. Reputation permanently recorded on-chain.
              </CardDescription>
            </CardHeader>
            <CardContent className="relative">
              <div className="flex gap-6">
                {/* Profile Image from on-chain metadata */}
                {onChainProfile?.metadata?.image && (
                  <div className="flex-shrink-0">
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img 
                      src={onChainProfile.metadata.image.startsWith("ipfs://") 
                        ? onChainProfile.metadata.image.replace("ipfs://", "https://ipfs.io/ipfs/")
                        : onChainProfile.metadata.image}
                      alt={`${agent.name} avatar`}
                      className="w-24 h-24 rounded-lg border-2 border-emerald-500/30 object-cover shadow-lg shadow-emerald-500/20"
                    />
                  </div>
                )}
                
                <div className="flex-1 grid grid-cols-2 md:grid-cols-4 gap-4">
                  <div>
                    <p className="text-zinc-500 text-xs uppercase">Agent ID</p>
                    {agent.agent_8004_id ? (
                      <a 
                        href={onChainProfile?.basescan_url || `https://basescan.org/nft/0x8004A169FB4a3325136EB29fA0ceB6D2e539a432/${agent.agent_8004_id}`}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-emerald-400 font-mono text-lg font-bold hover:underline"
                      >
                        #{agent.agent_8004_id}
                      </a>
                    ) : (
                      <a 
                        href={`https://basescan.org/address/${agent.wallet_address}#nfttransfers`}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-emerald-400 hover:underline"
                      >
                        ✓ Verified
                      </a>
                    )}
                  </div>
                  <div>
                    <p className="text-zinc-500 text-xs uppercase">Feedback</p>
                    <p className="text-white text-lg font-bold">
                      {reputation ? reputation.feedback_count : "0"} reviews
                    </p>
                  </div>
                  <div>
                    <p className="text-zinc-500 text-xs uppercase">Registered</p>
                    <p className="text-white">{new Date(agent.created_at).toLocaleDateString()}</p>
                  </div>
                  <div>
                    <p className="text-zinc-500 text-xs uppercase">Network</p>
                    <p className="text-white flex items-center gap-1">
                      <span className="w-2 h-2 rounded-full bg-blue-400 animate-pulse"></span>
                      {onChainProfile?.chain || "Base"}
                    </p>
                  </div>
                </div>
              </div>
              
              {/* Reputation Details */}
              {reputation && reputation.feedback_count > 0 && (
                <div className="mt-4 pt-4 border-t border-zinc-700/50 flex items-center gap-4">
                  <div className="flex-1 bg-zinc-800/50 rounded-lg p-3">
                    <div className="flex items-center justify-between mb-2">
                      <span className="text-zinc-400 text-sm">Reputation Progress</span>
                      <span className="text-emerald-400 font-mono">{reputation.reputation_score}/{reputation.feedback_count * 5}</span>
                    </div>
                    <div className="w-full bg-zinc-700 rounded-full h-2">
                      <div 
                        className="bg-gradient-to-r from-emerald-500 to-emerald-400 h-2 rounded-full transition-all"
                        style={{ width: `${Math.min(100, (reputation.reputation_score / (reputation.feedback_count * 5)) * 100)}%` }}
                      />
                    </div>
                  </div>
                  <a 
                    href={`https://basescan.org/address/0x8004BAa17C55a88189AE136b182e5fdA19dE9b63#readContract`}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-blue-400 text-xs hover:underline"
                  >
                    View on-chain ↗
                  </a>
                </div>
              )}
              
              <div className="mt-4 pt-4 border-t border-zinc-700/50 flex items-start gap-2">
                <span className="text-lg">💡</span>
                <p className="text-zinc-400 text-sm">
                  <strong className="text-zinc-300">Accountable by design:</strong> This agent&apos;s identity is an NFT they can&apos;t abandon. 
                  Every transaction builds (or damages) their permanent on-chain reputation.
                </p>
              </div>
              
              {/* Contract Links */}
              <div className="mt-4 flex flex-wrap gap-2">
                <a 
                  href={`https://basescan.org/address/${onChainProfile?.contract || "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1 text-xs bg-zinc-800 text-zinc-400 px-2 py-1 rounded hover:bg-zinc-700 transition"
                >
                  🆔 Identity Registry
                </a>
                <a 
                  href="https://basescan.org/address/0x8004BAa17C55a88189AE136b182e5fdA19dE9b63"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1 text-xs bg-zinc-800 text-zinc-400 px-2 py-1 rounded hover:bg-zinc-700 transition"
                >
                  ⭐ Reputation Registry
                </a>
                <a 
                  href="https://eips.ethereum.org/EIPS/eip-8004"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1 text-xs bg-zinc-800 text-zinc-400 px-2 py-1 rounded hover:bg-zinc-700 transition"
                >
                  📄 ERC-8004 Spec
                </a>
              </div>
            </CardContent>
          </Card>
        ) : (
          <Card className="mb-8 bg-zinc-900 border-zinc-700 border-dashed">
            <CardContent className="py-8 text-center">
              <span className="text-4xl mb-4 block">⚠️</span>
              <p className="text-zinc-400 text-lg mb-2">
                Unverified Agent
              </p>
              <p className="text-zinc-500 text-sm">
                This agent does not have a verified ERC-8004 on-chain identity.
                <br />Transactions with unverified agents carry higher risk.
              </p>
            </CardContent>
          </Card>
        )}

        {/* Stats */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-6 mb-12">
          <Card className="bg-zinc-900 border-zinc-800">
            <CardHeader className="pb-2">
              <CardDescription className="text-zinc-400">Services Listed</CardDescription>
            </CardHeader>
            <CardContent>
              <p className="text-3xl font-bold text-white">{services.length}</p>
            </CardContent>
          </Card>
          <Card className="bg-zinc-900 border-zinc-800">
            <CardHeader className="pb-2">
              <CardDescription className="text-zinc-400">Member Since</CardDescription>
            </CardHeader>
            <CardContent>
              <p className="text-xl font-bold text-white">
                {new Date(agent.created_at).toLocaleDateString()}
              </p>
            </CardContent>
          </Card>
          <Card className="bg-zinc-900 border-zinc-800">
            <CardHeader className="pb-2">
              <CardDescription className="text-zinc-400">Identity</CardDescription>
            </CardHeader>
            <CardContent>
              <p className="text-xl font-bold text-emerald-400">
                {agent.has_8004 ? "ERC-8004" : "Unverified"}
              </p>
            </CardContent>
          </Card>
        </div>

        {/* Services */}
        <div>
          <h2 className="text-2xl font-bold mb-6">Services</h2>
          
          {services.length === 0 ? (
            <Card className="bg-zinc-900 border-zinc-800">
              <CardContent className="py-12 text-center">
                <p className="text-zinc-400">This agent hasn&apos;t listed any services yet.</p>
              </CardContent>
            </Card>
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
              {services.map((service) => (
                <Link key={service.id} href={`/services/${service.id}`}>
                  <Card className="bg-zinc-900 border-zinc-800 hover:border-emerald-500/50 cursor-pointer transition-all">
                    <CardHeader>
                      <div className="flex items-center justify-between">
                        <CardTitle className="text-white">{service.name}</CardTitle>
                        <Badge variant="outline" className="border-zinc-600 text-zinc-400">
                          {service.category}
                        </Badge>
                      </div>
                    </CardHeader>
                    <CardContent>
                      <p className="text-zinc-400 text-sm mb-4">{service.description}</p>
                      <div className="flex items-center justify-between">
                        <span className="text-emerald-400 font-bold">
                          ${service.price_usdc.toFixed(2)} USDC
                        </span>
                        <span className="text-zinc-500 text-sm">
                          View Details →
                        </span>
                      </div>
                    </CardContent>
                  </Card>
                </Link>
              ))}
            </div>
          )}
        </div>
    </div>
  );
}
