import crypto from "crypto";
import { NextRequest, NextResponse } from "next/server";
import { addPollVote, getPollVotes, type PollVote } from "@/lib/store";
import { summarizePollVotes } from "@/lib/tripSummary";

// params is a Promise in this Next.js version, not a plain object - see
// node_modules/next/dist/docs/01-app/03-api-reference/03-file-conventions/route.md.
type RouteParams = { params: Promise<{ id: string }> };

// How far a request's X-Ts may drift from "now" before it's rejected -
// bounds the window a captured (ts, sig) pair could be replayed in.
const MAX_TIMESTAMP_SKEW_SECONDS = 300;

const HEX_SHA256_RE = /^[0-9a-f]{64}$/i;

/**
 * HMAC gate for GET - the only caller today is
 * plugins/trip_planner.py's _fetch_poll_data (see that file for the
 * matching signature generation), which needs API_SECRET_KEY and
 * TRIP_API_SECRET_KEY to hold the identical shared-secret value despite
 * the different variable names on each side. Returns an error message
 * on failure, or null when the request is authenticated.
 */
function verifyHmacSignature(request: NextRequest, id: string): string | null {
  const secret = process.env.API_SECRET_KEY;
  if (!secret) {
    return "API_SECRET_KEY is not configured.";
  }

  const ts = request.headers.get("x-ts");
  const sig = request.headers.get("x-sig");
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
    .update(`${ts}.GET./api/poll/${id}`)
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

export async function GET(request: NextRequest, { params }: RouteParams) {
  const { id } = await params;

  const authError = verifyHmacSignature(request, id);
  if (authError) {
    return NextResponse.json({ error: authError }, { status: 401 });
  }

  const votes = await getPollVotes(id);
  // `summary` is additive - existing consumers (the Poll and Dashboard
  // pages) keep reading `votes` unchanged. It exists so a consumer that
  // just wants totals - notably plugins/trip_planner.py's
  // finalize_trip_plan_async on the Python side - gets one clean object
  // instead of re-implementing this same tallying logic in a second
  // language.
  return NextResponse.json({ tripId: id, votes, summary: summarizePollVotes(votes) });
}

/**
 * Verifies a LINE LIFF ID token against LINE's own verification endpoint
 * and returns the trusted LINE user id (claims.sub) on success. Returns
 * null on any failure - missing config, network error, non-2xx, or a
 * claim that doesn't check out - so the caller always gets a clean
 * "unauthenticated" signal rather than having to distinguish failure
 * modes itself.
 */
async function verifyLineIdToken(idToken: string): Promise<string | null> {
  const clientId = process.env.LINE_CHANNEL_ID;
  if (!clientId) {
    console.error("LINE_CHANNEL_ID is not configured; cannot verify ID tokens.");
    return null;
  }

  let response: Response;
  try {
    response = await fetch("https://api.line.me/oauth2/v2.1/verify", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ id_token: idToken, client_id: clientId }),
    });
  } catch {
    return null;
  }

  if (!response.ok) {
    return null;
  }

  const claims = await response.json().catch(() => null);
  if (!claims || typeof claims.sub !== "string" || !claims.sub) {
    return null;
  }
  if (claims.aud !== clientId) {
    return null;
  }
  if (claims.iss !== "https://access.line.me") {
    return null;
  }
  if (typeof claims.exp !== "number" || claims.exp * 1000 <= Date.now()) {
    return null;
  }

  return claims.sub;
}

export async function POST(request: NextRequest, { params }: RouteParams) {
  const { id } = await params;

  const [scheme, idToken] = (request.headers.get("authorization") || "").split(" ");
  if (scheme !== "Bearer" || !idToken) {
    return NextResponse.json(
      { error: "Missing or invalid Authorization header." },
      { status: 401 }
    );
  }

  const lineUserId = await verifyLineIdToken(idToken);
  if (!lineUserId) {
    return NextResponse.json(
      { error: "Invalid or expired LINE ID token." },
      { status: 401 }
    );
  }

  const body = await request.json().catch(() => null);

  if (!body || typeof body.name !== "string" || !body.name.trim()) {
    return NextResponse.json(
      { error: "A name is required to submit a vote." },
      { status: 400 }
    );
  }

  const vote: PollVote = {
    name: body.name.trim(),
    lineUserId,
    startDate: typeof body.startDate === "string" ? body.startDate : "",
    endDate: typeof body.endDate === "string" ? body.endDate : "",
    wishlist: typeof body.wishlist === "string" ? body.wishlist.trim() : "",
    vibes: Array.isArray(body.vibes)
      ? body.vibes.filter((v: unknown): v is string => typeof v === "string")
      : [],
    submittedAt: new Date().toISOString(),
  };

  const votes = await addPollVote(id, vote);
  return NextResponse.json({ tripId: id, votes }, { status: 201 });
}
