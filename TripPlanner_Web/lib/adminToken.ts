/**
 * Admin-token hashing and verification - the real access-control
 * boundary for "Lock & Generate Plan" (POST /api/trigger-jarvis), which
 * no longer depends on LINE identity at all (that proved unreliable
 * throughout this project - see lib/lineAuth.ts's use elsewhere for
 * identity, never authorization).
 *
 * The raw token is minted once, server-to-server, by POST
 * /api/poll/[id]/admin-token (called by plugins/trip_planner.py's
 * _run_consensus_poll right after it creates a poll by voice) and is
 * never persisted - only its SHA-256 hash, in Poll.adminTokenHash (see
 * prisma/schema.prisma). Jarvis sends the raw token to the trip creator
 * privately as .../trip/poll/<id>?admin=<token>; the ordinary voting
 * link sent to the group carries no such param. The frontend
 * (app/trip/poll/[id]/page.tsx) only renders the "Lock & Generate Plan"
 * button when that param is present - a UX convenience, not the
 * security boundary. The boundary is verifyAdminToken below, which
 * app/api/trigger-jarvis/route.ts calls before doing anything else.
 */

import crypto from "crypto";
import { getPollAdminTokenHash } from "./store";

/**
 * SHA-256 hex digest of an admin token. Plain hashing, not HMAC - this
 * isn't verifying a message against a shared secret, it's storing a
 * high-entropy random token (secrets.token_urlsafe(32) on the Python
 * side, ~256 bits) the way an API key or session token is normally
 * stored, so a database leak alone can't be replayed as the raw token.
 */
export function hashAdminToken(token: string): string {
  return crypto.createHash("sha256").update(token, "utf8").digest("hex");
}

/**
 * True only if `rawToken` hashes to the value stored for `tripId`.
 * False for an empty token, a trip with no admin token ever issued
 * (adminTokenHash === ""), or a mismatch - every "not authorized" case
 * collapses to the same false, so a caller can't distinguish "wrong
 * token" from "no token was ever set" by timing or response shape.
 * Comparison is constant-time (crypto.timingSafeEqual), matching the
 * HMAC verification pattern already used in
 * app/api/poll/[id]/route.ts's verifyHmacSignature.
 */
export async function verifyAdminToken(tripId: string, rawToken: string): Promise<boolean> {
  if (!rawToken) return false;

  const storedHash = await getPollAdminTokenHash(tripId);
  if (!storedHash) return false;

  const providedHash = hashAdminToken(rawToken);
  const storedBuf = Buffer.from(storedHash, "hex");
  const providedBuf = Buffer.from(providedHash, "hex");
  return storedBuf.length === providedBuf.length && crypto.timingSafeEqual(storedBuf, providedBuf);
}
