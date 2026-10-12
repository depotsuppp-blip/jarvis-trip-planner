import Anthropic from "@anthropic-ai/sdk";
import { NextRequest, NextResponse } from "next/server";
import { verifyAdminToken } from "@/lib/adminToken";
import { formatItineraryForStorage, parseStoredItinerary } from "@/lib/itinerary";
import { PlacesApiError } from "@/lib/places";
import { checkRateLimit } from "@/lib/rateLimit";
import {
  claimPollForGeneration,
  getDraft,
  getPollDurationDays,
  getPollVoterUserIds,
  getPollVotes,
  lockPoll,
  releasePollClaim,
  saveDraft,
} from "@/lib/store";
import { loadTastePromptInput } from "@/lib/tasteForTrip";
import { generateItinerary } from "@/lib/tripGeneration";
import { summarizePollVotes } from "@/lib/tripSummary";
import { computeBestTripWindow, resolveTripDays } from "@/lib/tripDates";

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
 * runs a grounded pipeline (lib/tripGeneration.ts) - a Haiku call to plan
 * the day structure (categories + areas, no venue names; Stage 1), real
 * Google Places Text Search calls to find actual venues for every slot
 * (Stage 1.5), a second Haiku call to write the final itinerary choosing only
 * from that real data and estimating a per-stop cost (Stage 2), and finally,
 * in parallel: a real Google Routes API call per stop-to-stop leg with a
 * dynamically chosen travel mode (Stage 2.5, see lib/routes.ts - never another
 * LLM call, this is real data attached as-is) and a real-world forecast
 * lookup for the trip's window (Stage 2.6, see lib/weather.ts) - and
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
 * NOT YET DONE: pushing the result back into the LINE group chat the way
 * plugins/trip_planner.py's _finalize_trip_task does via line_notifier -
 * that needs LINE_CHANNEL_ACCESS_TOKEN and a target group id configured
 * in this Next.js app's own environment (a group id specifically, not
 * just a personal LINE_USER_ID push target - out of scope for now). The
 * frontend receiving the plan directly in this response is today's
 * substitute.
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
    // Atomically claims this trip before spending anything - see
    // claimPollForGeneration's docstring. Two near-simultaneous "Lock &
    // Generate Plan" clicks resolve to exactly one "claimed" and one
    // "in_progress" (or, if the first already finished, "locked") - never
    // two concurrent generations for the same trip.
    const claim = await claimPollForGeneration(tripId);

    if (claim === "locked") {
      const draft = await getDraft(tripId);
      const stored = draft ? parseStoredItinerary(draft.text) : null;
      if (stored) {
        return NextResponse.json({ tripId, locked: true, itinerary: stored });
      }
      console.error(`POST /api/trigger-jarvis: trip ${tripId} is locked but has no valid stored plan.`);
      return NextResponse.json(
        { error: "This poll is locked but its plan could not be loaded." },
        { status: 500 }
      );
    }

    if (claim === "in_progress") {
      return NextResponse.json(
        { error: "A plan is already being generated for this poll - please wait a moment and try again." },
        { status: 409 }
      );
    }

    // claim === "claimed" - this request now owns generation for this
    // trip and must release the claim on every exit path below except
    // success, which hands off to lockPoll instead.
    try {
      const votes = await getPollVotes(tripId);
      if (votes.length === 0) {
        await releasePollClaim(tripId);
        return NextResponse.json(
          { error: "No one has voted on this poll yet - wait for votes before locking it." },
          { status: 400 }
        );
      }

      // The organizer's actually-requested trip length, read from
      // Poll.durationDays (see that field's comment in
      // prisma/schema.prisma) - captured once at poll-creation time by
      // POST /api/poll/[id]/admin-token, NOT from this request's body.
      // resolveTripDays re-clamps it as a defense-in-depth safety net
      // (the write side already clamps too), so a malformed stored
      // value never breaks generation.
      const tripDays = resolveTripDays(await getPollDurationDays(tripId));

      // Per-stage timings, logged (not just commented) so real numbers
      // stay verifiable as this pipeline changes - see this route's
      // maxDuration comment, which cites a real measured run. Date.now()
      // deltas rather than console.time/timeEnd: that API's labels
      // collide across concurrent requests for different trips, since
      // this route has no per-request namespacing for them.
      const summary = summarizePollVotes(votes);
      // The specific tripDays-day window most voters overlap on - see
      // lib/tripDates.ts's docstring for why this replaced the old
      // earliest-start/latest-end span across every vote. Computed once
      // here and threaded through Stage 1 (the prompt) and Stage 2.5 (the
      // Routes API departure-date anchor) so both agree on the same
      // dates.
      const tripWindow = computeBestTripWindow(votes, tripDays);
      console.log(
        `[trigger-jarvis] ${tripId}: trip window ${tripWindow.startDate} to ${tripWindow.endDate} ` +
          `(${tripDays}d, ${tripWindow.voterCount}/${votes.length} voters overlap)`
      );

      // The accounts behind the votes may have finished the taste quiz; a vote
      // cast before signing in was required belongs to no account and has no
      // profile. Null (and an unchanged plan) when none of them has one.
      const taste = await loadTastePromptInput(() => getPollVoterUserIds(tripId), votes.length, tripId);
      if (taste) {
        console.log(
          `[trigger-jarvis] ${tripId}: taste profile from ${taste.taste.people}/${votes.length} voters - ` +
            `loves ${taste.taste.loved.map((t) => t.category).join(", ") || "-"}; ` +
            `passes on ${taste.taste.avoided.map((t) => t.category).join(", ") || "-"}`
        );
      }

      const itinerary = await generateItinerary(
        { kind: "poll", summary, tripWindow, tripDays, taste },
        { tag: "trigger-jarvis", id: tripId }
      );

      await saveDraft(tripId, formatItineraryForStorage(itinerary));
      await lockPoll(tripId);

      return NextResponse.json({ tripId, locked: true, itinerary }, { status: 201 });
    } catch (innerError) {
      await releasePollClaim(tripId).catch(() => {});
      throw innerError;
    }
  } catch (error) {
    if (error instanceof PlacesApiError) {
      console.error(`POST /api/trigger-jarvis: Places API error (${error.reason}):`, error.message);
      return NextResponse.json(
        { error: `Venue search is unavailable: ${error.message}`, reason: error.reason },
        { status: 502 }
      );
    }
    if (error instanceof Anthropic.RateLimitError) {
      return NextResponse.json(
        { error: "The plan generator is temporarily rate-limited - please try again shortly." },
        { status: 429 }
      );
    }
    if (error instanceof Anthropic.APIError) {
      console.error(`POST /api/trigger-jarvis failed (Anthropic ${error.status}):`, error.message);
      return NextResponse.json(
        { error: "The plan generator is temporarily unavailable. Please try again." },
        { status: 502 }
      );
    }
    console.error(`POST /api/trigger-jarvis failed for trip ${tripId}:`, error);
    return NextResponse.json(
      { error: "Something went wrong while generating the plan. Please try again." },
      { status: 500 }
    );
  }
}
