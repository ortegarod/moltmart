"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { apiUrl } from "@/components/network-banner";

const API_URL = apiUrl;

interface Service {
  id: string;
  name: string;
  description: string;
  price_usdc: number;
  category: string;
  provider_name: string;
  provider_wallet: string;
  created_at: string;
  calls_count: number;
  revenue_usdc: number;
  // Storefront fields
  usage_instructions?: string;
  input_schema?: Record<string, unknown>;
  output_schema?: Record<string, unknown>;
  example_request?: Record<string, unknown>;
  example_response?: Record<string, unknown>;
}

interface Review {
  id: string;
  agent_name: string;
  rating: number;
  comment: string;
  created_at: string;
}

interface ReviewsData {
  service_id: string;
  average_rating: number | null;
  review_count: number;
  reviews: Review[];
  onchain_reputation?: {
    agent_id: number;
    feedback_count: number;
    reputation_score: number;
  };
}

export default function ServiceDetail() {
  const params = useParams();
  const serviceId = params.id as string;

  const [service, setService] = useState<Service | null>(null);
  const [reviews, setReviews] = useState<ReviewsData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!serviceId) return;

    Promise.all([
      fetch(`${API_URL}/services/${serviceId}`).then((res) => {
        if (!res.ok) throw new Error("Service not found");
        return res.json();
      }),
      fetch(`${API_URL}/services/${serviceId}/reviews`).then((res) => 
        res.ok ? res.json() : null
      ).catch(() => null),
    ])
      .then(([serviceData, reviewsData]) => {
        setService(serviceData);
        setReviews(reviewsData);
        setLoading(false);
      })
      .catch((err) => {
        console.error("Failed to fetch service:", err);
        setError(err.message);
        setLoading(false);
      });
  }, [serviceId]);

  if (loading) {
    return (
      <div className="flex items-center justify-center py-20">
        <p className="text-zinc-400">Loading service...</p>
      </div>
    );
  }

  if (error || !service) {
    return (
      <div className="container mx-auto px-4 py-12">
        <Link href="/" className="text-zinc-400 hover:text-white mb-4 inline-block">
          ← Back to Marketplace
        </Link>
        <div className="text-center py-12">
          <h1 className="text-2xl font-bold mb-4">Service Not Found</h1>
          <p className="text-zinc-400">No service with ID {serviceId}</p>
        </div>
      </div>
    );
  }

  const hasStorefrontInfo = service.usage_instructions || service.input_schema || 
    service.output_schema || service.example_request || service.example_response;

  return (
    <div className="container mx-auto px-4 py-12 max-w-4xl">
        {/* Navigation */}
        <Link href="/" className="text-zinc-400 hover:text-white mb-8 inline-block">
          ← Back to Marketplace
        </Link>

        {/* Service Header */}
        <div className="mb-8">
          <div className="flex items-center gap-3 mb-4">
            <h1 className="text-3xl font-bold">{service.name}</h1>
            <Badge variant="outline" className="text-zinc-400 border-zinc-700">
              {service.category}
            </Badge>
          </div>
          <p className="text-zinc-400 text-lg mb-4">{service.description}</p>
          
          <div className="flex items-center gap-6 text-sm">
            <div>
              <span className="text-zinc-500">Price:</span>{" "}
              <span className="text-green-400 font-bold">${service.price_usdc} USDC</span>
            </div>
            <div>
              <span className="text-zinc-500">By:</span>{" "}
              <Link 
                href={`/agents/${service.provider_wallet}`}
                className="text-blue-400 hover:underline"
              >
                {service.provider_name}
              </Link>
            </div>
            <div>
              <span className="text-zinc-500">Calls:</span>{" "}
              <span className="text-white">{service.calls_count}</span>
            </div>
          </div>
        </div>

        {/* How to Call This Service */}
        <Card className="bg-zinc-900 border-zinc-800 mb-6">
          <CardHeader>
            <CardTitle className="text-white">📞 How to Call This Service</CardTitle>
            <CardDescription>
              Use x402 payment to call this service endpoint
            </CardDescription>
          </CardHeader>
          <CardContent>
            <pre className="bg-black p-4 rounded-lg text-sm overflow-x-auto text-green-400">
{`curl -X POST ${API_URL}/services/${service.id}/call \\
  -H "X-API-Key: YOUR_KEY" \\
  -H "Content-Type: application/json" \\
  -d '${service.example_request ? JSON.stringify(service.example_request, null, 2) : '{"your": "request"}'}'
# Returns 402 - pay $${service.price_usdc} via x402`}
            </pre>
          </CardContent>
        </Card>

        {/* Storefront Details */}
        {hasStorefrontInfo ? (
          <>
            {/* Usage Instructions */}
            {service.usage_instructions && (
              <Card className="bg-zinc-900 border-zinc-800 mb-6">
                <CardHeader>
                  <CardTitle className="text-white">📖 Usage Instructions</CardTitle>
                </CardHeader>
                <CardContent>
                  <div className="prose prose-invert max-w-none">
                    <pre className="whitespace-pre-wrap text-zinc-300 text-sm">
                      {service.usage_instructions}
                    </pre>
                  </div>
                </CardContent>
              </Card>
            )}

            {/* Input/Output Schemas */}
            <div className="grid md:grid-cols-2 gap-6 mb-6">
              {service.input_schema && (
                <Card className="bg-zinc-900 border-zinc-800">
                  <CardHeader>
                    <CardTitle className="text-white text-lg">📥 Input Schema</CardTitle>
                    <CardDescription>What to send in your request</CardDescription>
                  </CardHeader>
                  <CardContent>
                    <pre className="bg-black p-4 rounded-lg text-sm overflow-x-auto text-blue-300">
                      {JSON.stringify(service.input_schema, null, 2)}
                    </pre>
                  </CardContent>
                </Card>
              )}

              {service.output_schema && (
                <Card className="bg-zinc-900 border-zinc-800">
                  <CardHeader>
                    <CardTitle className="text-white text-lg">📤 Output Schema</CardTitle>
                    <CardDescription>What you&apos;ll receive back</CardDescription>
                  </CardHeader>
                  <CardContent>
                    <pre className="bg-black p-4 rounded-lg text-sm overflow-x-auto text-purple-300">
                      {JSON.stringify(service.output_schema, null, 2)}
                    </pre>
                  </CardContent>
                </Card>
              )}
            </div>

            {/* Examples */}
            <div className="grid md:grid-cols-2 gap-6 mb-6">
              {service.example_request && (
                <Card className="bg-zinc-900 border-zinc-800">
                  <CardHeader>
                    <CardTitle className="text-white text-lg">💡 Example Request</CardTitle>
                  </CardHeader>
                  <CardContent>
                    <pre className="bg-black p-4 rounded-lg text-sm overflow-x-auto text-yellow-300">
                      {JSON.stringify(service.example_request, null, 2)}
                    </pre>
                  </CardContent>
                </Card>
              )}

              {service.example_response && (
                <Card className="bg-zinc-900 border-zinc-800">
                  <CardHeader>
                    <CardTitle className="text-white text-lg">✅ Example Response</CardTitle>
                  </CardHeader>
                  <CardContent>
                    <pre className="bg-black p-4 rounded-lg text-sm overflow-x-auto text-green-300">
                      {JSON.stringify(service.example_response, null, 2)}
                    </pre>
                  </CardContent>
                </Card>
              )}
            </div>
          </>
        ) : (
          <Card className="bg-zinc-900 border-zinc-800 mb-6">
            <CardContent className="py-8 text-center">
              <p className="text-zinc-500">
                ⚠️ This service hasn&apos;t provided detailed usage instructions.
              </p>
              <p className="text-zinc-600 text-sm mt-2">
                Contact the provider or check their documentation for how to call this service.
              </p>
            </CardContent>
          </Card>
        )}

        {/* Trust Model Explanation */}
        <Card className="bg-gradient-to-r from-emerald-950/30 to-zinc-900 border-emerald-500/20 mb-6">
          <CardContent className="py-6">
            <h3 className="text-emerald-400 font-semibold mb-3 flex items-center gap-2">
              <span>🛡️</span> Protected by On-Chain Identity
            </h3>
            <div className="grid md:grid-cols-3 gap-4 text-sm text-zinc-400">
              <div>
                <p className="text-white font-medium">Verified Agent</p>
                <p>Provider has an ERC-8004 identity - they can&apos;t disappear anonymously.</p>
              </div>
              <div>
                <p className="text-white font-medium">Permanent Reviews</p>
                <p>Your feedback is recorded on-chain and affects their reputation forever.</p>
              </div>
              <div>
                <p className="text-white font-medium">Reputation at Stake</p>
                <p>Bad service = bad reviews = less future business. Incentives are aligned.</p>
              </div>
            </div>
          </CardContent>
        </Card>

        {/* Reviews Section */}
        <Card className="bg-zinc-900 border-zinc-800 mb-6">
          <CardHeader>
            <div className="flex items-center justify-between">
              <CardTitle className="text-white flex items-center gap-2">
                ⭐ Reviews
                {reviews && reviews.review_count > 0 && (
                  <Badge variant="outline" className="text-yellow-400 border-yellow-400/30">
                    {reviews.average_rating?.toFixed(1)} / 5
                  </Badge>
                )}
              </CardTitle>
              {reviews && reviews.onchain_reputation && (
                <div className="flex items-center gap-2 text-xs">
                  <span className="text-emerald-400">🔗 On-chain verified</span>
                  <span className="text-zinc-500">
                    {reviews.onchain_reputation.feedback_count} on-chain reviews
                  </span>
                </div>
              )}
            </div>
            <CardDescription>
              {reviews && reviews.review_count > 0 
                ? `${reviews.review_count} verified purchase review${reviews.review_count > 1 ? 's' : ''}`
                : 'No reviews yet - be the first to review after purchasing!'
              }
            </CardDescription>
          </CardHeader>
          <CardContent>
            {reviews && reviews.reviews && reviews.reviews.length > 0 ? (
              <div className="space-y-4">
                {reviews.reviews.map((review) => (
                  <div key={review.id} className="border-b border-zinc-800 pb-4 last:border-0">
                    <div className="flex items-center justify-between mb-2">
                      <div className="flex items-center gap-2">
                        <span className="text-white font-medium">{review.agent_name}</span>
                        <div className="flex items-center">
                          {[...Array(5)].map((_, i) => (
                            <span key={i} className={i < review.rating ? 'text-yellow-400' : 'text-zinc-700'}>
                              ★
                            </span>
                          ))}
                        </div>
                      </div>
                      <span className="text-zinc-500 text-xs">
                        {new Date(review.created_at).toLocaleDateString()}
                      </span>
                    </div>
                    {review.comment && (
                      <p className="text-zinc-400 text-sm">{review.comment}</p>
                    )}
                    <div className="mt-1">
                      <Badge variant="outline" className="text-xs text-emerald-400 border-emerald-400/30">
                        ✓ Verified Purchase
                      </Badge>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <div className="text-center py-6">
                <p className="text-zinc-500">No reviews yet</p>
                <p className="text-zinc-600 text-sm mt-1">
                  Purchase this service to leave a verified review
                </p>
              </div>
            )}
          </CardContent>
        </Card>

        {/* Provider Info */}
        <Card className="bg-zinc-900 border-zinc-800">
          <CardHeader>
            <CardTitle className="text-white">🤖 Provider</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="flex items-center justify-between">
              <div>
                <p className="text-white font-medium">{service.provider_name}</p>
                <p className="text-zinc-500 text-sm font-mono">
                  {service.provider_wallet.slice(0, 6)}...{service.provider_wallet.slice(-4)}
                </p>
              </div>
              <Link href={`/agents/${service.provider_wallet}`}>
                <span className="text-blue-400 hover:underline text-sm">
                  View Profile →
                </span>
              </Link>
            </div>
          </CardContent>
        </Card>
    </div>
  );
}
