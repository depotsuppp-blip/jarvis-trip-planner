import crypto from "crypto";
import { NextRequest, NextResponse } from "next/server";
import { hashAdminToken } from "@/lib/adminToken";
import { createPollAdminToken } from "@/lib/store";

// params is a Promise in this Next.js version, not a plain object - see
// node_modules/next/dist/docs/01-app/03-api-reference/03-file-conventions/route.md.
type RouteParams = { params: Promise<{ id: string }> };

// How far a request's X-Ts may drift from "now" before it's rejected -
// bounds the window a captured (ts, sig) pair could be replayed in.
const MAX_TIMESTAMP_SKEW_SECONDS = 300;

const HEX_SHA256_RE = /^[0-9a-f]{64}$/i;

// A raw token below this length would make the credential brute-forceable;
// plugins/trip_planner.py sends secrets.token_urlsafe(32) (~43 chars),
// comfortably above this floor.
const MIN_ADMIN_TOKEN_LENGTH = 16;

/**
 * HMAC check for POST .../admin-token - same shared-secret scheme as
 * app/api/poll/[id]/route.ts's GET (X-Ts/X-Sig, HMAC-SHA256 over
 * "{ts}.{method}.{path}", API_SECRET_KEY / TRIP_API_SECRET_KEY on the
 * Python side), but MANDATORY here rather than optional. That GET falls
 * back to trip-id-unguessability for an ordinary anonymous browser
 * read; this route mints the one credential that gates "Lock & Generate
 * Plan" (see app/api/trigger-jarvis/route.ts), so a request with no
 * signature at all must be rejected outright, never treated as
 * anonymous. The only caller is plugins/trip_planner.py's
 * _create_poll_admin_token, immediately after a poll is created by
 * voice.
 */
function verifyHmacSignature(request: NextRequest, id: string): string | null {
  const ts = request.headers.get("x-ts");
  const sig = request.headers.get("x-sig");

  const secret = process.env.API_SECRET_KEY;
  if (!secret) {
    return "API_SECRET_KEY is not configured.";
  }
  if (!ts || !sig) {
    return "Missing X-Ts/X-Sig headers.";
  }

  const tsSeconds = Number(ts);
  if (!Number.isFinite(tsSeconds)) {
    return "Invalid X-Ts header.";
  }
  if (Math.abs(Date.now() / 1000 - tsSeconds) > MAX_TIMESTAMP_SKEW_SECONDS) {
    return "Request timestamp is too old.";
  }

  if (!HEX_SHA256_RE.test(sig)) {
    return "Invalid X-Sig header.";
  }

  const expectedSig = crypto
    .createHmac("sha256", secret)
    .update(`${ts}.POST./api/poll/${id}/admin-token`)
    .digest("hex");

  const expectedBuf = Buffer.from(expectedSig, "hex");
  const providedBuf = Buffer.from(sig, "hex");
  if (
    expectedBuf.length !== providedBuf.length ||
    !crypto.timingSafeEqual(expectedBuf, providedBuf)
  ) {
    return "Signature mismatch.";
  }

  return null;
}

/**
 * POST /api/poll/[id]/admin-token - mints this trip's admin credential.
 *
 * Server-to-server only: the caller (plugins/trip_planner.py's
 * _create_poll_admin_token) generates a random token and sends the RAW
 * value here over this HMAC-authenticated channel. This route hashes it
 * (lib/adminToken.ts's hashAdminToken) and stores only the hash on the
 * Poll row - the raw token is never persisted anywhere. The caller then
 * builds .../trip/poll/<id>?admin=<rawToken> and sends it privately to
 * the trip creator only, separately from the public voting link.
 *
 * Intentionally callable more than once for the same trip id (upsert,
 * not create-or-fail): a retried voice command should mint a fresh
 * token rather than get stuck on a leftover row from a prior attempt.
 */
export async function POST(request: NextRequest, { params }: RouteParams) {
  const { id } = await params;

  try {
    const authError = verifyHmacSignature(request, id);
    if (authError) {
      return NextResponse.json({ error: authError }, { status: 401 });
    }

    const body = await request.json().catch(() => null);
    const adminToken = typeof body?.adminToken === "string" ? body.adminToken : "";
    if (!adminToken || adminToken.length < MIN_ADMIN_TOKEN_LENGTH) {
      return NextResponse.json(
        { error: `adminToken is required and must be at least ${MIN_ADMIN_TOKEN_LENGTH} characters.` },
        { status: 400 }
      );
    }

    await createPollAdminToken(id, hashAdminToken(adminToken));
    return NextResponse.json({ tripId: id }, { status: 201 });
  } catch (error) {
    console.error(`POST /api/poll/${id}/admin-token failed:`, error);
    return NextResponse.json(
      { error: "Something went wrong while creating this poll's admin token." },
      { status: 500 }
    );
  }
}
