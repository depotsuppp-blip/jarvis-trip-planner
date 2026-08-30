import crypto from "crypto";
import { NextRequest, NextResponse } from "next/server";
import { addPollVote, getPollVotes, type PollVote } from "@/lib/store";
import { summarizePollVotes } from "@/lib/tripSummary";
import { verifyBearerLineToken } from "@/lib/lineAuth";
import { checkRateLimit } from "@/lib/rateLimit";

// A generous allowance for legitimate double-taps/retries while still
// stopping a runaway client loop - this is a low-stakes vote, not the
// costly LLM-triggering action /api/trigger-jarvis guards.
const VOTE_RATE_LIMIT = 10;
const VOTE_RATE_WINDOW_MS = 60_000;

// params is a Promise in this Next.js version, not a plain object - see
// node_modules/next/dist/docs/01-app/03-api-reference/03-file-conventions/route.md.
type RouteParams = { params: Promise<{ id: string }> };

// How far a request's X-Ts may drift from "now" before it's rejected -
// bounds the window a captured (ts, sig) pair could be replayed in.
const MAX_TIMESTAMP_SKEW_SECONDS = 300;

const HEX_SHA256_RE = /^[0-9a-f]{64}$/i;

/**
 * HMAC check for GET, verified ONLY when a caller actually presents
 * X-Ts/X-Sig headers - today that's exclusively
 * plugins/trip_planner.py's _fetch_poll_data (see that file for the
 * matching signature generation), which needs API_SECRET_KEY and
 * TRIP_API_SECRET_KEY to hold the identical shared-secret value despite
 * the different variable names on each side.
 *
 * A request with NEITHER header is treated as an ordinary anonymous
 * read, not rejected - the poll and dashboard pages themselves
 * (app/trip/poll/[id]/page.tsx, app/trip/dashboard/[id]/page.tsx) call
 * this same GET from the browser with no signature at all, and already
 * display these same votes to anyone holding the trip link with no
 * login required. Requiring a signature here without ever updating
 * those two callers to send one made every browser load 401 - trip id
 * unguessability is this app's actual access-control boundary for a
 * read, same as the equally unauthenticated GET /api/draft/[id].
 * A signature that IS present is still fully verified below, so
 * plugins/trip_planner.py's calls are unaffected and a forged one is
 * still rejected.
 */
function verifyHmacSignature(request: NextRequest, id: string): string | null {
  const ts = request.headers.get("x-ts");
  const sig = request.headers.get("x-sig");
  if (!ts && !sig) {
    return null;
  }

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

  try {
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
  } catch (error) {
    // An uncaught throw here (e.g. a corrupted .data/polls.json, a disk
    // error) previously escaped as a bare connection drop instead of a
    // JSON response - see the same rationale on POST below.
    console.error(`GET /api/poll/${id} failed:`, error);
    return NextResponse.json(
      { error: "Something went wrong while loading this poll." },
      { status: 500 }
    );
  }
}

/**
 * Best-effort caller IP for rate-limiting an anonymous vote, which has
 * no lineUserId to key by. Vercel sets X-Forwarded-For; falls back to a
 * single shared bucket if it's absent (e.g. local dev), which is
 * strictly a lower bound on protection, never a hole relative to today
 * - unauthenticated voting is new, so there was no per-caller limit at
 * all on this path before.
 */
function clientIp(request: NextRequest): string {
  const forwarded = request.headers.get("x-forwarded-for");
  return forwarded ? forwarded.split(",")[0].trim() : "unknown";
}

export async function POST(request: NextRequest, { params }: RouteParams) {
  const { id } = await params;

  try {
    return await handlePollVote(request, id);
  } catch (error) {
    // Last-resort net: anything unexpected here (a corrupted
    // .data/polls.json, a disk/fs error inside addPollVote, etc.) used to
    // escape as an unhandled exception - which Vercel/Next.js surfaces to
    // the browser as a connection drop (ERR_ABORTED) with no response
    // body at all, not a normal 500. Logging the full error server-side
    // and always returning JSON keeps the frontend able to show the user
    // something instead of a silent failure.
    console.error(`POST /api/poll/${id} failed:`, error);
    return NextResponse.json(
      { error: "Something went wrong while submitting your vote. Please try again." },
      { status: 500 }
    );
  }
}

async function handlePollVote(request: NextRequest, id: string): Promise<NextResponse> {
  // A LINE session is now optional, not required - see
  // app/trip/poll/[id]/page.tsx, which no longer forces a liff.login()
  // redirect before voting is possible (that redirect proved unreliable
  // in real testing). A header that IS present must still check out,
  // though: a present-but-bad token fails loudly (401) rather than
  // silently downgrading to an anonymous vote the user wouldn't know
  // about. Only a request with NO Authorization header at all is
  // treated as an intentional anonymous submission.
  const authHeader = request.headers.get("authorization");
  let lineUserId = "";
  if (authHeader) {
    lineUserId = (await verifyBearerLineToken(authHeader)) || "";
    if (!lineUserId) {
      return NextResponse.json(
        { error: "Invalid or expired LINE ID token." },
        { status: 401 }
      );
    }
  }

  const rateLimitKey = lineUserId ? `vote:${lineUserId}` : `vote:anon:${clientIp(request)}`;
  const rateLimit = checkRateLimit(rateLimitKey, VOTE_RATE_LIMIT, VOTE_RATE_WINDOW_MS);
  if (!rateLimit.allowed) {
    return NextResponse.json(
      { error: "Too many votes submitted - please wait a moment and try again." },
      { status: 429, headers: { "Retry-After": String(rateLimit.retryAfterSeconds) } }
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
