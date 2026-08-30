/**
 * Best-effort in-memory rate limiter, keyed by whatever the caller
 * passes (a verified lineUserId, ideally - never a client-supplied
 * value alone, since that would let a caller just claim a fresh key).
 *
 * Only holds within one running server process: Vercel can spin up
 * multiple concurrent function instances with no memory shared between
 * them, so this is not a hard cap under real multi-instance load. It
 * still stops the common case - a retry loop, a double-tap, one caller
 * hammering a single warm instance - which is what these mutating
 * routes need protection from today. A durable, cross-instance limit
 * would need Vercel KV or Upstash Redis; deliberately not introduced
 * here, since adding a new hosted dependency is an infra decision, not
 * a code fix.
 */

interface Bucket {
  count: number;
  windowStart: number;
}

const buckets = new Map<string, Bucket>();

export interface RateLimitResult {
  allowed: boolean;
  retryAfterSeconds: number;
}

export function checkRateLimit(key: string, limit: number, windowMs: number): RateLimitResult {
  const now = Date.now();
  const bucket = buckets.get(key);

  if (!bucket || now - bucket.windowStart >= windowMs) {
    buckets.set(key, { count: 1, windowStart: now });
    return { allowed: true, retryAfterSeconds: 0 };
  }

  if (bucket.count < limit) {
    bucket.count += 1;
    return { allowed: true, retryAfterSeconds: 0 };
  }

  const retryAfterSeconds = Math.ceil((bucket.windowStart + windowMs - now) / 1000);
  return { allowed: false, retryAfterSeconds };
}
