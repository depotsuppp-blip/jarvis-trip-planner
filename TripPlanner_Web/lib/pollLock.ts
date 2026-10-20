/**
 * Locking a poll and generating its plan: the part of "Lock & Generate Plan"
 * that is the same whoever asks. Shared by the two routes that can ask:
 *
 *   POST /api/trigger-jarvis          the organizer's button, authorized by the
 *                                     admin token they were sent privately;
 *   POST /api/poll/[id]/finalize      Jarvis (voice or web chat), authorized by
 *                                     the shared secret plus who made the poll.
 *
 * Authorization, rate limits and the HTTP answer stay in each route - this is
 * only what happens once a caller has been allowed to lock this poll: claim it,
 * read its votes, build the plan (lib/tripGeneration.ts, personalized from the
 * voters' Taste Profiles) and store it. It moved here unchanged from the
 * trigger-jarvis route; the stage timings that route's comments cite are
 * unchanged too.
 */

import { formatItineraryForStorage, parseStoredItinerary, type Itinerary } from "./itinerary";
import {
  claimPollForGeneration,
  getDraft,
  getPollDurationDays,
  getPollVoterUserIds,
  getPollVotes,
  lockPoll,
  releasePollClaim,
  saveDraft,
} from "./store";
import { loadTastePromptInput } from "./tasteForTrip";
import { generateItinerary, type TastePromptInput } from "./tripGeneration";
import { computeBestTripWindow, resolveTripDays } from "./tripDates";
import { summarizePollVotes } from "./tripSummary";

/**
 * How a lock attempt ended. Everything that is a normal answer is a variant;
 * a failure of the pipeline itself (Places, Anthropic, the database) is thrown
 * AFTER the claim has been released, so a retry is never blocked by it.
 */
export type PollLockResult =
  /** A plan was generated and the poll is now locked. */
  | { kind: "generated"; itinerary: Itinerary; taste: TastePromptInput | null; voters: number }
  /** The poll was locked already; this is the plan that was stored then. */
  | { kind: "already_locked"; itinerary: Itinerary }
  /** Locked, but TripDraft holds no valid plan - a data problem, not a caller's. */
  | { kind: "locked_without_plan" }
  /** Another request holds the claim right now. */
  | { kind: "in_progress" }
  /** Nobody has voted, so there is nothing to plan from. The claim was released. */
  | { kind: "no_votes" };

/**
 * Locks `tripId` and generates its plan, or says why not (see PollLockResult).
 * `logTag` names the calling route in every stage log line (lib/tripGeneration.ts
 * writes the timings), so a log can be tied back to the request that caused it.
 *
 * The caller must already have decided this poll may be locked: nothing here
 * checks an admin token or who is asking.
 */
export async function lockAndGeneratePoll(tripId: string, logTag: string): Promise<PollLockResult> {
  // Atomically claims this trip before spending anything - see
  // claimPollForGeneration's docstring. Two near-simultaneous "Lock & Generate
  // Plan" requests resolve to exactly one "claimed" and one "in_progress" (or,
  // if the first already finished, "locked") - never two concurrent
  // generations for the same trip.
  const claim = await claimPollForGeneration(tripId);

  if (claim === "locked") {
    const draft = await getDraft(tripId);
    const stored = draft ? parseStoredItinerary(draft.text) : null;
    return stored ? { kind: "already_locked", itinerary: stored } : { kind: "locked_without_plan" };
  }

  if (claim === "in_progress") {
    return { kind: "in_progress" };
  }

  // claim === "claimed" - this request now owns generation for this trip and
  // must release the claim on every exit path below except success, which hands
  // off to lockPoll instead.
  try {
    const votes = await getPollVotes(tripId);
    if (votes.length === 0) {
      await releasePollClaim(tripId);
      return { kind: "no_votes" };
    }

    // The organizer's actually-requested trip length, read from
    // Poll.durationDays (see that field's comment in prisma/schema.prisma) -
    // captured once at poll-creation time by POST /api/poll/[id]/admin-token,
    // NOT from any request body. resolveTripDays re-clamps it as a
    // defense-in-depth safety net (the write side already clamps too), so a
    // malformed stored value never breaks generation.
    const tripDays = resolveTripDays(await getPollDurationDays(tripId));

    // Per-stage timings are logged (not just commented) so real numbers stay
    // verifiable as this pipeline changes. Date.now() deltas rather than
    // console.time/timeEnd: that API's labels collide across concurrent
    // requests for different trips, since it has no per-request namespacing.
    const summary = summarizePollVotes(votes);
    // The specific tripDays-day window most voters overlap on - see
    // lib/tripDates.ts's docstring for why this replaced the old
    // earliest-start/latest-end span across every vote. Computed once here and
    // threaded through Stage 1 (the prompt) and Stage 2.5 (the Routes API
    // departure-date anchor) so both agree on the same dates.
    const tripWindow = computeBestTripWindow(votes, tripDays);
    console.log(
      `[${logTag}] ${tripId}: trip window ${tripWindow.startDate} to ${tripWindow.endDate} ` +
        `(${tripDays}d, ${tripWindow.voterCount}/${votes.length} voters overlap)`
    );

    // The accounts behind the votes may have finished the taste quiz; a vote
    // cast before signing in was required belongs to no account and has no
    // profile. Null (and an unchanged plan) when none of them has one.
    const taste = await loadTastePromptInput(() => getPollVoterUserIds(tripId), votes.length, tripId);
    if (taste) {
      console.log(
        `[${logTag}] ${tripId}: taste profile from ${taste.taste.people}/${votes.length} voters - ` +
          `likes ${taste.taste.loved.map((t) => t.category).join(", ") || "-"}`
      );
    }

    const itinerary = await generateItinerary(
      { kind: "poll", summary, tripWindow, tripDays, taste },
      { tag: logTag, id: tripId }
    );

    await saveDraft(tripId, formatItineraryForStorage(itinerary));
    await lockPoll(tripId);

    return { kind: "generated", itinerary, taste, voters: votes.length };
  } catch (error) {
    await releasePollClaim(tripId).catch(() => {});
    throw error;
  }
}
