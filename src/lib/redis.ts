import { Redis } from "@upstash/redis";

let redis: Redis | null = null;

export function getRedis(): Redis | null {
  if (!process.env.UPSTASH_REDIS_REST_URL || !process.env.UPSTASH_REDIS_REST_TOKEN) {
    return null;
  }
  if (!redis) {
    redis = new Redis({
      url: process.env.UPSTASH_REDIS_REST_URL,
      token: process.env.UPSTASH_REDIS_REST_TOKEN,
    });
  }
  return redis;
}

/**
 * Publish a payment event to the "payments" channel.
 * Voice server listens on this channel to confirm payment mid-call.
 */
export async function publishPaymentEvent(data: {
  call_id: string;
  event: string;
  amount: number;
  prospect_id?: string;
}): Promise<boolean> {
  const r = getRedis();
  if (!r) return false;
  try {
    await r.publish("payments", JSON.stringify(data));
    return true;
  } catch {
    return false;
  }
}
