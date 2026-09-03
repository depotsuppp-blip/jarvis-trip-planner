import Anthropic from "@anthropic-ai/sdk";
import { zodOutputFormat } from "@anthropic-ai/sdk/helpers/zod";
import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import { verifyAdminToken } from "@/lib/adminToken";
import { PlacesApiError, searchPlacesForSlot, type LatLng, type PlaceResult } from "@/lib/places";
import { checkRateLimit } from "@/lib/rateLimit";
import { computeDayRoute, type TravelLeg } from "@/lib/routes";
import {
  claimPollForGeneration,
  getDraft,
  getPollVotes,
  lockPoll,
  releasePollClaim,
  saveDraft,
} from "@/lib/store";
import { summarizePollVotes, type PollSummary } from "@/lib/tripSummary";
import { computeBestTripWindow, TRIP_WINDOW_DAYS, type TripDateWindow } from "@/lib/tripDates";

// Two Haiku calls, N parallel Places calls, plus up to one Routes API
// call per day (Stage 2.5), all days in parallel. Measured end to end
// against two real successful 6-day Chiang Mai runs (see this route's
// own [trigger-jarvis] stage logs): stage 1 (skeleton) 3.5-4.1s, stage
// 1.5 (Places, all in parallel) 0.5-0.7s, stage 2 (final write)
// 10.1-14.2s, stage 2.5 (Routes, all 6 days in parallel) 0.17-0.31s -
// total 14.8-19.9s, comfortably inside this ceiling. Stage 2.5 stays
// well under a second regardless of trip length, since every day's
// Compute Routes call runs concurrently rather than one after another;
// stage 2's Haiku call, not Stage 2.5, is what actually dominates.
// https://vercel.com/docs/functions/configuring-functions/duration.
export const maxDuration = 60;

// Locking a poll spends real LLM quota and (once LINE push-back exists -
// see this route's own docstring below) sends a LINE message - a much
// costlier action per call than casting a vote, so the allowance here is
// tight.
const TRIGGER_RATE_LIMIT = 3;
const TRIGGER_RATE_WINDOW_MS = 5 * 60_000;

// Cost-optimization default: claude-haiku-4-5, not opus-5/sonnet-5 -
// already decided against escalating once this two-stage grounded
// architecture replaced the single-call approach (see the prior
// hallucination-vs-cost comparison). No thinking/effort config below:
// unlike Opus 5/Sonnet 5/Fable 5, Haiku 4.5 is pre-4.6-tier -
// output_config.effort errors outright on this model, and omitting
// `thinking` entirely (rather than an explicit {type:"disabled"}) is
// its correct "no extended thinking" state.
const FINALIZE_MODEL = "claude-haiku-4-5-20251001";
const FINALIZE_MAX_TOKENS = 4096;

// ---------------------------------------------------------------------
// Final itinerary shape - each day is now one ORDERED sequence of stops
// (rather than separate activities[]/meals[] bags), each optionally
// carrying travelFromPrevious - the Stage 2.5 Routes API leg from the
// PRECEDING stop in this same array, null for a day's first stop or
// wherever travel data wasn't available (see enrichItineraryWithTravelTimes).
// This has diverged from plugins/trip_planner.py's Itinerary Pydantic
// model (which still uses activities[]/meals[]) - that Python pipeline
// doesn't run Stage 2.5 and isn't touched by it.
// ---------------------------------------------------------------------

const TravelLegSchema = z.object({
  durationMinutes: z.number(),
  distanceMeters: z.number(),
});

const ItineraryStopSchema = z.object({
  slotType: z.enum(["activity", "meal"]),
  text: z.string(),
  travelFromPrevious: TravelLegSchema.nullable(),
});

const ItineraryDaySchema = z.object({
  day: z.number(),
  summary: z.string(),
  stops: z.array(ItineraryStopSchema),
});

const ItinerarySchema = z.object({
  destination: z.string(),
  days: z.array(ItineraryDaySchema),
  notes: z.string(),
  // The specific TRIP_WINDOW_DAYS-day window (see lib/tripDates.ts) this
  // itinerary was generated for - optional so a plan stored before this
  // field existed still parses via parseStoredItinerary below rather
  // than being rejected outright.
  startDate: z.string().optional(),
  endDate: z.string().optional(),
});

type Itinerary = z.infer<typeof ItinerarySchema>;
type ItineraryStop = z.infer<typeof ItineraryStopSchema>;

// ---------------------------------------------------------------------
// Stage 2's LLM-facing output shape - structurally similar, but stops
// carry placeIndex (which of that slot's grounded candidates Claude
// used) instead of travelFromPrevious. Travel data is never asked of
// the model - see this route's docstring point 4: Stage 2.5 attaches it
// afterward, in code, from real Routes API results, never narrated.
// ---------------------------------------------------------------------

const ItineraryStopLLMSchema = z.object({
  slotType: z.enum(["activity", "meal"]),
  text: z.string(),
  // 0-based index into the slot's candidate list (see formatSlotForPrompt)
  // indicating exactly which real venue this stop's text is about. -1
  // means "no real venue was available for this slot" (formatSlotForPrompt's
  // NO REAL VENUES FOUND case) - any value outside the slot's actual
  // candidate range is treated the same way by enrichItineraryWithTravelTimes,
  // never trusted blindly.
  placeIndex: z.number().int(),
});

const ItineraryDayLLMSchema = z.object({
  day: z.number(),
  summary: z.string(),
  stops: z.array(ItineraryStopLLMSchema),
});

const ItineraryLLMSchema = z.object({
  destination: z.string(),
  days: z.array(ItineraryDayLLMSchema),
  notes: z.string(),
});

type ItineraryLLM = z.infer<typeof ItineraryLLMSchema>;

// ---------------------------------------------------------------------
// Stage 1: activity skeleton - what KIND of place each part of each day
// should be, and roughly where. No venue names at this stage - those
// come from real Places data in stage 2, never from the model's own
// recall.
// ---------------------------------------------------------------------

const ActivitySlotSchema = z.object({
  slotType: z.enum(["activity", "meal"]),
  // A search category, e.g. "cafe", "temple", "night market", "Thai
  // restaurant", "viewpoint" - never a specific venue name.
  category: z.string(),
  // Neighborhood/area to search near, e.g. "Nimman", "Old City".
  area: z.string(),
});

const DaySkeletonSchema = z.object({
  day: z.number(),
  theme: z.string(),
  slots: z.array(ActivitySlotSchema),
});

const ItinerarySkeletonSchema = z.object({
  destination: z.string(),
  days: z.array(DaySkeletonSchema),
});

type ItinerarySkeleton = z.infer<typeof ItinerarySkeletonSchema>;

/**
 * Mirrors plugins/trip_planner.py's _build_finalization_prompt in intent
 * - reads the same pre-aggregated PollSummary shape
 * (lib/tripSummary.ts's summarizePollVotes) rather than re-tallying raw
 * votes. Unlike a "respond with ONLY a JSON object" prompt, this doesn't
 * need to ask for JSON at all - output_config.format below constrains
 * the response shape at the API level.
 *
 * Dates diverge from that Python prompt, though: this passes the actual
 * TRIP_WINDOW_DAYS-day window computeBestTripWindow picked (the specific
 * days most voters overlap on), not the group's full combined date
 * range, and instructs the model to return exactly that many days - see
 * this route's docstring point about the trip being a fixed "5 Days 4
 * Nights" length, not however wide the poll's raw votes happened to
 * span.
 */
function buildSkeletonPrompt(summary: PollSummary, tripWindow: TripDateWindow): string {
  const vibesText =
    summary.topVibes.length > 0
      ? summary.topVibes.map((v) => `${v.vibe} (${v.count} votes)`).join(", ")
      : "No vibes selected yet.";
  const wishlistText =
    summary.wishlist.length > 0
      ? summary.wishlist.join("; ")
      : "No specific places suggested.";
  const votersText = summary.voters.length > 0 ? summary.voters.join(", ") : "the group";

  return (
    "Plan the STRUCTURE of a trip itinerary based on this consensus data from a group trip poll. " +
    "You will fill in real venue names in a later step - for now, decide only what KIND of place " +
    "each part of the day should be, and roughly where. Infer a specific real destination city from " +
    "the group's requested places and vibes. Resolve conflicting wishes by prioritizing the most " +
    "popular vibes. Do not reference how the group gets to the destination or where they are coming " +
    "from - start the itinerary from arrival.\n\n" +
    `Group size: ${summary.totalVotes} people (${votersText}).\n` +
    `Top vibes, most to least popular: ${vibesText}.\n` +
    `Specific places requested by the group: ${wishlistText}.\n\n` +
    `This trip is FIXED at exactly ${TRIP_WINDOW_DAYS} days / ${TRIP_WINDOW_DAYS - 1} nights, ` +
    `from ${tripWindow.startDate} to ${tripWindow.endDate} inclusive - the specific window that the ` +
    `most voters (${tripWindow.voterCount} of ${summary.totalVotes}) can make, not the group's full ` +
    "combined date range. You MUST return EXACTLY " +
    `${TRIP_WINDOW_DAYS} day objects in the "days" array, numbered 1 through ${TRIP_WINDOW_DAYS} in ` +
    "calendar order matching that window - never more, never fewer.\n\n" +
    "For each day, break it into a small number of slots - roughly 3-4 activities and 2 meals per " +
    "day is a reasonable density, not more. For each slot, give: slotType (\"activity\" or \"meal\"), " +
    "a short search category such as \"cafe\", \"temple\", \"night market\", \"Thai restaurant\", " +
    "\"viewpoint\", or \"museum\" - NOT a specific venue name - and the neighborhood or area to look " +
    "near. Do not invent or guess any specific venue name at this stage."
  );
}

async function generateSkeleton(
  summary: PollSummary,
  tripWindow: TripDateWindow
): Promise<ItinerarySkeleton> {
  const apiKey = process.env.ANTHROPIC_API_KEY;
  if (!apiKey) {
    throw new Error("ANTHROPIC_API_KEY is not configured.");
  }

  const client = new Anthropic({ apiKey });
  const response = await client.messages.parse({
    model: FINALIZE_MODEL,
    max_tokens: FINALIZE_MAX_TOKENS,
    output_config: { format: zodOutputFormat(ItinerarySkeletonSchema) },
    messages: [{ role: "user", content: buildSkeletonPrompt(summary, tripWindow) }],
  });

  if (!response.parsed_output) {
    throw new Error("Claude did not return a valid itinerary skeleton.");
  }

  // Prompt-level enforcement (above) is the primary mechanism, but Haiku
  // can still miscount - a code-level safety net is cheap insurance. An
  // over-long skeleton is truncated to the requested window rather than
  // failing the whole generation; an under-long one is left as-is and
  // just logged, since fabricating extra days' content isn't safe to do
  // without another grounded LLM pass.
  const skeleton = response.parsed_output;
  if (skeleton.days.length !== TRIP_WINDOW_DAYS) {
    console.error(
      `[trigger-jarvis] skeleton returned ${skeleton.days.length} days, expected ${TRIP_WINDOW_DAYS} ` +
        `(window ${tripWindow.startDate} to ${tripWindow.endDate}).`
    );
    if (skeleton.days.length > TRIP_WINDOW_DAYS) {
      return { ...skeleton, days: skeleton.days.slice(0, TRIP_WINDOW_DAYS) };
    }
  }
  return skeleton;
}

// ---------------------------------------------------------------------
// Stage 1.5: ground every slot in real Places data, in parallel.
// ---------------------------------------------------------------------

interface GroundedSlot {
  day: number;
  slotType: "activity" | "meal";
  category: string;
  area: string;
  places: PlaceResult[];
}

async function groundSkeleton(skeleton: ItinerarySkeleton): Promise<GroundedSlot[]> {
  const flatSlots = skeleton.days.flatMap((day) =>
    day.slots.map((slot) => ({ day: day.day, ...slot }))
  );

  // Promise.all, not sequential - N independent HTTP calls, and a slot's
  // result doesn't depend on any other slot's. A PlacesApiError (the API
  // itself is broken - not configured, not enabled, billing off, ...)
  // rejects the whole batch immediately, which is correct: that failure
  // mode affects every slot identically, so there is no partial result
  // worth salvaging, and the caller needs one clear, specific message
  // instead of N generic ones.
  return Promise.all(
    flatSlots.map(async (slot) => ({
      ...slot,
      places: await searchPlacesForSlot(slot.category, slot.area, skeleton.destination),
    }))
  );
}

// ---------------------------------------------------------------------
// Stage 2: write the final itinerary, choosing only from real results.
// ---------------------------------------------------------------------

function formatSlotForPrompt(slot: GroundedSlot, slotPosition: number): string {
  if (slot.places.length === 0) {
    return (
      `  - Stop ${slotPosition} [${slot.slotType}] ${slot.category} near ${slot.area}: ` +
      "NO REAL VENUES FOUND. Say so plainly in this stop's text rather than inventing one, and set placeIndex to -1."
    );
  }
  const candidates = slot.places
    .slice(0, 3)
    .map((p, i) => `${i}=${p.name} (${p.address}${p.rating !== null ? `, rating ${p.rating}` : ""})`)
    .join("; ");
  return `  - Stop ${slotPosition} [${slot.slotType}] ${slot.category} near ${slot.area}, candidates: ${candidates}`;
}

/**
 * Mirrors buildSkeletonPrompt's data-shape reasoning: asks for exactly
 * one stop per slot, IN ORDER, since Stage 2.5 (enrichItineraryWithTravelTimes)
 * matches Claude's returned stops array back to groundedSlots by that
 * same array position, not by any name matching.
 */
function buildFinalPrompt(skeleton: ItinerarySkeleton, groundedSlots: GroundedSlot[]): string {
  const daysText = skeleton.days
    .map((day) => {
      const daySlots = groundedSlots.filter((s) => s.day === day.day);
      const slotsText = daySlots.map((slot, i) => formatSlotForPrompt(slot, i)).join("\n");
      return `Day ${day.day} (${day.theme}):\n${slotsText}`;
    })
    .join("\n\n");

  return (
    `Write the final trip itinerary for ${skeleton.destination}, following this planned structure, ` +
    "using the REAL Google Places search results listed for each stop below. Only use venues from " +
    "the provided candidates - never invent a name not present in this data. For each day, return " +
    "exactly one stop object per Stop listed, IN THE SAME ORDER, with: slotType (copy from the Stop), " +
    "text (a short one-sentence description, incorporating the chosen venue's real name), and " +
    "placeIndex (the candidate number - 0, 1, or 2 - that your text is about). If a Stop says NO REAL " +
    "VENUES FOUND, say so plainly in that stop's text rather than inventing a fallback, and set " +
    "placeIndex to -1.\n\n" +
    `${daysText}`
  );
}

async function generateFinalItinerary(
  skeleton: ItinerarySkeleton,
  groundedSlots: GroundedSlot[]
): Promise<ItineraryLLM> {
  const apiKey = process.env.ANTHROPIC_API_KEY;
  if (!apiKey) {
    throw new Error("ANTHROPIC_API_KEY is not configured.");
  }

  const client = new Anthropic({ apiKey });
  const response = await client.messages.parse({
    model: FINALIZE_MODEL,
    max_tokens: FINALIZE_MAX_TOKENS,
    output_config: { format: zodOutputFormat(ItineraryLLMSchema) },
    messages: [{ role: "user", content: buildFinalPrompt(skeleton, groundedSlots) }],
  });

  if (!response.parsed_output) {
    throw new Error("Claude did not return a valid itinerary.");
  }
  return response.parsed_output;
}

// ---------------------------------------------------------------------
// Stage 2.5: ground every stop-to-stop transition in a real Google
// Routes API drive-time/distance estimate. Runs entirely in code, after
// Stage 2 - never routed through another LLM call (see this route's
// docstring point 4).
// ---------------------------------------------------------------------

// Mid-morning is a reasonable default departure for a day of sightseeing
// - not load-bearing precision, since TRAFFIC_AWARE's estimate for a
// date weeks out is a historical-pattern prediction regardless of the
// exact hour. Left in UTC rather than resolved to the destination's
// actual timezone (which this app doesn't otherwise track anywhere) -
// still lands the estimate on the right DAY, which is what matters for
// a weekday-vs-weekend traffic pattern.
const DEFAULT_DEPARTURE_HOUR_UTC = 10;

// Used only when no vote supplied a start date at all - still anchors
// the estimate to a plausible FUTURE date (required by Routes API's
// departureTime) rather than "now", which would request current traffic
// instead of a general future-pattern estimate.
const FALLBACK_DAYS_FROM_NOW = 14;

/**
 * RFC3339 UTC timestamp for itinerary day N's departure - tripStartDate
 * (day 1's date) plus (dayNumber - 1) days, at a fixed mid-morning hour.
 * Clamped to at least tomorrow if that lands in the past (a poll whose
 * voted dates have already elapsed) or if tripStartDate is missing/
 * unparseable entirely - Routes API rejects a past departureTime.
 */
function computeDepartureTimeForDay(tripStartDate: string | null, dayNumber: number): string {
  const fallback = new Date();
  fallback.setUTCDate(fallback.getUTCDate() + FALLBACK_DAYS_FROM_NOW);
  fallback.setUTCHours(DEFAULT_DEPARTURE_HOUR_UTC, 0, 0, 0);

  let departure = fallback;
  if (tripStartDate) {
    const parsed = new Date(`${tripStartDate}T00:00:00Z`);
    if (!Number.isNaN(parsed.getTime())) {
      parsed.setUTCDate(parsed.getUTCDate() + (dayNumber - 1));
      parsed.setUTCHours(DEFAULT_DEPARTURE_HOUR_UTC, 0, 0, 0);
      departure = parsed;
    }
  }

  const tomorrow = new Date();
  tomorrow.setUTCDate(tomorrow.getUTCDate() + 1);
  if (departure.getTime() < tomorrow.getTime()) {
    departure = tomorrow;
    departure.setUTCHours(DEFAULT_DEPARTURE_HOUR_UTC, 0, 0, 0);
  }

  return departure.toISOString();
}

type ItineraryStopLLM = z.infer<typeof ItineraryStopLLMSchema>;

/**
 * Resolves one LLM-returned stop's placeIndex back to real coordinates,
 * or null if there aren't any - an out-of-range or -1 placeIndex, a
 * slot Stage 1.5 found zero candidates for, or a candidate Google
 * returned with no location. Never trusts placeIndex blindly: Claude's
 * structured output is schema-validated, not content-validated, so an
 * out-of-bounds integer is treated the same as "no venue" rather than
 * throwing.
 */
function resolveStopLocation(stop: ItineraryStopLLM, slot: GroundedSlot | undefined): LatLng | null {
  if (!slot) return null;
  if (stop.placeIndex < 0 || stop.placeIndex >= slot.places.length) return null;
  return slot.places[stop.placeIndex].location;
}

/**
 * Attaches Stage 2.5 travel data to every day, calling Routes API for
 * every day in parallel (Promise.all, not sequential) - each day is an
 * independent request, and there's no reason one day's routing should
 * wait on another's round trip.
 *
 * Within a day, only stops with a resolvable location (see
 * resolveStopLocation) are sent to Routes API, preserving their
 * original stop-array positions - a stop with no coordinates (no real
 * venue found for that slot, or Google had no location for the chosen
 * one) simply can't anchor a leg on either side of it, so it always gets
 * travelFromPrevious: null, and the NEXT geocoded stop's travel time (if
 * any) is computed from the last geocoded stop before it, skipping the
 * gap - the closest honest estimate available rather than omitting that
 * leg too. If the whole day's Routes API call fails outright (see
 * computeDayRoute), every stop in that day gets travelFromPrevious: null -
 * a day-level failure can't be attributed to one specific leg, but it
 * must never fail the itinerary this route already spent real Anthropic
 * and Places quota generating.
 */
async function enrichItineraryWithTravelTimes(
  itinerary: ItineraryLLM,
  groundedSlots: GroundedSlot[],
  tripWindow: TripDateWindow
): Promise<Itinerary> {
  const days = await Promise.all(
    itinerary.days.map(async (day) => {
      const daySlots = groundedSlots.filter((s) => s.day === day.day);
      const locations = day.stops.map((stop, i) => resolveStopLocation(stop, daySlots[i]));

      const geocodedIndices = locations
        .map((loc, i) => (loc ? i : -1))
        .filter((i) => i >= 0);
      const geocodedCoords = geocodedIndices.map((i) => locations[i] as LatLng);

      const legs =
        geocodedCoords.length >= 2
          ? await computeDayRoute(
              geocodedCoords,
              computeDepartureTimeForDay(tripWindow.startDate, day.day)
            )
          : null;

      // legs[k] is the transition INTO geocodedIndices[k + 1] - map it
      // back onto that stop's original position; every other stop
      // (ungeocoded, or the day's first geocoded stop) stays null.
      const travelByStopIndex = new Map<number, TravelLeg>();
      if (legs) {
        for (let k = 0; k < legs.length; k++) {
          travelByStopIndex.set(geocodedIndices[k + 1], legs[k]);
        }
      }

      const stops: ItineraryStop[] = day.stops.map((stop, i) => ({
        slotType: stop.slotType,
        text: stop.text,
        travelFromPrevious: travelByStopIndex.get(i) ?? null,
      }));

      return { day: day.day, summary: day.summary, stops };
    })
  );

  return {
    destination: itinerary.destination,
    days,
    notes: itinerary.notes,
    startDate: tripWindow.startDate,
    endDate: tripWindow.endDate,
  };
}

/**
 * The generated plan is stored as JSON in the trip's TripDraft row
 * (shared with the solo draft board feature - see that model's comment
 * in prisma/schema.prisma) so a repeat "Lock & Generate Plan" click on an
 * already-locked poll can return the exact same plan without spending
 * LLM quota again, rather than re-deriving it from a lossy text format.
 */
function formatItineraryForStorage(itinerary: Itinerary): string {
  return JSON.stringify(itinerary, null, 2);
}

function parseStoredItinerary(text: string): Itinerary | null {
  try {
    return ItinerarySchema.parse(JSON.parse(text));
  } catch {
    return null;
  }
}

/**
 * POST /api/trigger-jarvis - "Lock & Generate Plan" on the poll page.
 *
 * Self-contained: reads this trip's votes from Neon via Prisma, then
 * runs a grounded pipeline - a Haiku call to plan the day structure
 * (categories + areas, no venue names; Stage 1), real Google Places
 * Text Search calls to find actual venues for every slot (Stage 1.5), a
 * second Haiku call to write the final itinerary choosing only from
 * that real data (Stage 2), and finally a real Google Routes API
 * Compute Routes call per day to attach drive-time/distance between
 * each day's consecutive stops (Stage 2.5, see
 * enrichItineraryWithTravelTimes - never another LLM call, this is
 * real data attached as-is) - and returns the finished itinerary in the
 * response body. No dependency on Jarvis's local Python backend, which
 * never accepts inbound connections.
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

      // Per-stage timings, logged (not just commented) so real numbers
      // stay verifiable as this pipeline changes - see this route's
      // maxDuration comment, which cites a real measured run. Date.now()
      // deltas rather than console.time/timeEnd: that API's labels
      // collide across concurrent requests for different trips, since
      // this route has no per-request namespacing for them.
      const summary = summarizePollVotes(votes);
      // The specific TRIP_WINDOW_DAYS-day window most voters overlap on -
      // see lib/tripDates.ts's docstring for why this replaced the old
      // earliest-start/latest-end span across every vote. Computed once
      // here and threaded through Stage 1 (the prompt) and Stage 2.5 (the
      // Routes API departure-date anchor) so both agree on the same
      // dates.
      const tripWindow = computeBestTripWindow(votes);
      console.log(
        `[trigger-jarvis] ${tripId}: trip window ${tripWindow.startDate} to ${tripWindow.endDate} ` +
          `(${tripWindow.voterCount}/${votes.length} voters overlap)`
      );

      let stageStart = Date.now();
      const skeleton = await generateSkeleton(summary, tripWindow);
      console.log(`[trigger-jarvis] ${tripId}: Stage 1 (skeleton) ${Date.now() - stageStart}ms`);

      stageStart = Date.now();
      const groundedSlots = await groundSkeleton(skeleton);
      console.log(`[trigger-jarvis] ${tripId}: Stage 1.5 (Places) ${Date.now() - stageStart}ms`);

      stageStart = Date.now();
      const draftItinerary = await generateFinalItinerary(skeleton, groundedSlots);
      console.log(`[trigger-jarvis] ${tripId}: Stage 2 (final write) ${Date.now() - stageStart}ms`);

      stageStart = Date.now();
      const itinerary = await enrichItineraryWithTravelTimes(draftItinerary, groundedSlots, tripWindow);
      console.log(`[trigger-jarvis] ${tripId}: Stage 2.5 (Routes) ${Date.now() - stageStart}ms`);

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
