import { NextRequest, NextResponse } from "next/server";
import { verifyAdminToken } from "@/lib/adminToken";
import { generationFailureResponse } from "@/lib/generationErrors";
import { lockAndGeneratePoll } from "@/lib/pollLock";
import { checkRateLimit } from "@/lib/rateLimit";

// Two Haiku calls, N parallel Places calls, plus one Routes API call per
// LEG (Stage 2.5 - see lib/routes.ts's per-leg dynamic travel mode),
// plus one Weather API call, all in parallel within their stage. Measured
// end to end against real successful Chiang Mai runs (see the
// [trigger-jarvis] stage logs lib/tripGeneration.ts writes): stage 1
// (skeleton) 3.5-4.1s, stage 1.5 (Places, all in parallel) 0.5-0.7s, stage 2
// (final write) 10.1-14.2s, stage 2.5 (Routes + weather, all in parallel)
// well under a second regardless of trip length - stage 2's Haiku call, not
// Stage 2.5, is what actually dominates. Reading the voters' Taste Profiles
// is one indexed query before stage 1.
// https://vercel.com/docs/functions/configuring-functions/duration.
export const maxDuration = 60;

// Locking a poll spends real LLM quota and (once LINE push-back exists -
// see this route's own docstring below) sends a LINE message - a much
// costlier action per call than casting a vote, so the allowance here is
// tight.
const TRIGGER_RATE_LIMIT = 3;
const TRIGGER_RATE_WINDOW_MS = 5 * 60_000;

/**
 * POST /api/trigger-jarvis - "Lock & Generate Plan" on the poll page.
 *
 * Self-contained: reads this trip's votes from Neon via Prisma, then
 * runs a grounded pipeline (lib/pollLock.ts -> lib/tripGeneration.ts) - a
 * Haiku call to plan the day structure (categories + areas, no venue names;
 * Stage 1), real Google Places Text Search calls to find actual venues for
 * every slot (Stage 1.5), a second Haiku call to write the final itinerary
 * choosing only from that real data and estimating a per-stop cost (Stage 2),
 * and finally, in parallel: a real Google Routes API call per stop-to-stop leg
 * with a dynamically chosen travel mode (Stage 2.5, see lib/routes.ts - never
 * another LLM call, this is real data attached as-is) and a real-world
 * forecast lookup for the trip's window (Stage 2.6, see lib/weather.ts) - and
 * returns the finished itinerary in the response body. No dependency on
 * Jarvis's local Python backend, which never accepts inbound
 * connections.
 *
 * PERSONALIZATION: the accounts that voted may have finished the taste quiz
 * (TasteSwipe). Their answers are added together (lib/tasteSummary.ts) and
 * carried into the Stage 1 and Stage 2 prompts, so the group's plan leans
 * towards what they said they love and away from what they pass on. Voters
 * without a finished quiz simply add nothing, and a poll where nobody has one
 * is generated exactly as before.
 *
 * NOT YET DONE: pushing the result back into the LINE GROUP chat.
 * plugins/trip_planner.py's voice finalize sends the plan's link to the
 * organizer's own LINE through line_notifier (a personal LINE_USER_ID push
 * target); reaching the group needs LINE_CHANNEL_ACCESS_TOKEN and a target
 * group id configured in this Next.js app's own environment - out of scope
 * for now. The frontend receiving the plan directly in this response is
 * today's substitute.
 *
 * AUTHORIZATION: an admin_token is required in the request body and
 * verified against Poll.adminTokenHash (see lib/adminToken.ts's
 * verifyAdminToken) before anything else runs - no LINE identity is
 * accepted or resolved here at all any more. LINE auth proved unreliable
 * throughout this project (see voting's own history of the same
 * problem) and, worse, was never actually an authorization check to
 * begin with - any verified-or-anonymous caller who knew a trip_id could
 * already trigger generation regardless of identity. The admin token is
 * minted once per trip, out of band, when the poll is created by voice
 * (plugins/trip_planner.py's _run_consensus_poll -> POST
 * /api/poll/[id]/admin-token) and sent privately to the trip creator
 * only - a plain voter holding just the public poll link never receives
 * it and cannot construct it. The frontend hiding the "Lock & Generate
 * Plan" button without this token (app/trip/poll/[id]/page.tsx) is UX
 * only; this check is the actual security boundary.
 *
 * The other way to lock a poll is POST /api/poll/[id]/finalize, which Jarvis
 * calls (signed with the shared secret, for the person who created the poll);
 * both end in the same lockAndGeneratePoll.
 */
export async function POST(request: NextRequest) {
  const body = await request.json().catch(() => null);
  const tripId = typeof body?.trip_id === "string" ? body.trip_id.trim() : "";
  const adminToken = typeof body?.admin_token === "string" ? body.admin_token.trim() : "";

  if (!tripId) {
    return NextResponse.json({ error: "trip_id is required." }, { status: 400 });
  }

  if (!(await verifyAdminToken(tripId, adminToken))) {
    return NextResponse.json(
      { error: "A valid admin token is required to lock and generate this trip's plan." },
      { status: 403 }
    );
  }

  const rateLimitKey = `trigger:${tripId}`;
  const rateLimit = checkRateLimit(rateLimitKey, TRIGGER_RATE_LIMIT, TRIGGER_RATE_WINDOW_MS);
  if (!rateLimit.allowed) {
    return NextResponse.json(
      { error: "Too many plan-generation requests - please wait a few minutes and try again." },
      { status: 429, headers: { "Retry-After": String(rateLimit.retryAfterSeconds) } }
    );
  }

  try {
    const result = await lockAndGeneratePoll(tripId, "trigger-jarvis");

    switch (result.kind) {
      case "already_locked":
        return NextResponse.json({ tripId, locked: true, itinerary: result.itinerary });

      case "locked_without_plan":
        console.error(`POST /api/trigger-jarvis: trip ${tripId} is locked but has no valid stored plan.`);
        return NextResponse.json(
          { error: "This poll is locked but its plan could not be loaded." },
          { status: 500 }
        );

      case "in_progress":
        return NextResponse.json(
          { error: "A plan is already being generated for this poll - please wait a moment and try again." },
          { status: 409 }
        );

      case "no_votes":
        return NextResponse.json(
          { error: "No one has voted on this poll yet - wait for votes before locking it." },
          { status: 400 }
        );

      case "generated":
        return NextResponse.json({ tripId, locked: true, itinerary: result.itinerary }, { status: 201 });
    }
  } catch (error) {
    const known = generationFailureResponse(error, "POST /api/trigger-jarvis");
    if (known) return known;
    console.error(`POST /api/trigger-jarvis failed for trip ${tripId}:`, error);
    return NextResponse.json(
      { error: "Something went wrong while generating the plan. Please try again." },
      { status: 500 }
    );
  }
}
