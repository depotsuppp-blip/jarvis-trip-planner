import Anthropic from "@anthropic-ai/sdk";
import { zodOutputFormat } from "@anthropic-ai/sdk/helpers/zod";
import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import { verifyAdminToken } from "@/lib/adminToken";
import { FINALIZE_MODEL, FINALIZE_MAX_TOKENS } from "@/lib/llm";
import {
  computeTotalEstimatedCost,
  formatItineraryForStorage,
  parseStoredItinerary,
  type Itinerary,
  type ItineraryStop,
} from "@/lib/itinerary";
import { PlacesApiError, searchPlacesForSlot, searchPlacesText, type LatLng, type PlaceResult } from "@/lib/places";
import { checkRateLimit } from "@/lib/rateLimit";
import { computeDayRoute, computeDepartureTimeForDay, type TravelLeg } from "@/lib/routes";
import {
  claimPollForGeneration,
  getDraft,
  getPollVotes,
  lockPoll,
  releasePollClaim,
  saveDraft,
} from "@/lib/store";
import { summarizePollVotes, type PollSummary } from "@/lib/tripSummary";
import { computeBestTripWindow, resolveTripDays, type TripDateWindow } from "@/lib/tripDates";
import { fetchWeatherSummary } from "@/lib/weather";

// Two Haiku calls, N parallel Places calls, plus one Routes API call per
// LEG (Stage 2.5 - see lib/routes.ts's per-leg dynamic travel mode),
// plus one Weather API call, all in parallel within their stage. Measured
// end to end against real successful Chiang Mai runs (see this route's
// own [trigger-jarvis] stage logs): stage 1 (skeleton) 3.5-4.1s, stage
// 1.5 (Places, all in parallel) 0.5-0.7s, stage 2 (final write)
// 10.1-14.2s, stage 2.5 (Routes + weather, all in parallel) well under a
// second regardless of trip length - stage 2's Haiku call, not Stage 2.5,
// is what actually dominates. https://vercel.com/docs/functions/configuring-functions/duration.
export const maxDuration = 60;

// Locking a poll spends real LLM quota and (once LINE push-back exists -
// see this route's own docstring below) sends a LINE message - a much
// costlier action per call than casting a vote, so the allowance here is
// tight.
const TRIGGER_RATE_LIMIT = 3;
const TRIGGER_RATE_WINDOW_MS = 5 * 60_000;

// ---------------------------------------------------------------------
// Stage 2's LLM-facing output shape - structurally similar to the
// persisted Itinerary (lib/itinerary.ts), but stops carry placeIndex
// (which of that slot's grounded candidates Claude used) instead of
// travelFromPrevious/location - those are attached afterward, in code,
// by enrichItineraryWithTravelTimes (Stage 2.5), never narrated by the
// model.
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
  // A realistic per-person cost in `currency` below, grounded in the
  // candidate's real Places priceLevel (see formatSlotForPrompt) - null
  // for a stop with no real venue to estimate from.
  estimatedCostPerPerson: z.number().nullable(),
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
  // The ISO 4217-ish currency code the model determined for the
  // destination (e.g. "THB") - every stop's estimatedCostPerPerson is
  // denominated in this same currency.
  currency: z.string(),
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
  // restaurant", "viewpoint" - ignored by groundSkeleton when
  // specificPlaceName is set (see below); otherwise never a specific
  // venue name.
  category: z.string(),
  // Neighborhood/area to search near, e.g. "Nimman", "Old City". Ignored
  // when specificPlaceName is set - a named search doesn't need an area.
  area: z.string(),
  // The exact venue name, when - and ONLY when - this slot exists to
  // satisfy a specific place the group named in their wishlist (e.g.
  // "คัตสึยะ" -> "Katsuya", romanized/translated into however it would
  // appear in a map search) - see buildSkeletonPrompt's STRICT RULE.
  // "" for every ordinary slot, where category+area are what's used
  // instead. groundSkeleton searches by this name directly rather than
  // by category+area when it's non-empty, and Stage 2's prompt (see
  // formatSlotForPrompt) flags the slot so the model doesn't quietly
  // substitute a different venue of the same general type.
  specificPlaceName: z.string(),
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
 * tripDays-day window computeBestTripWindow picked (the specific days
 * most voters overlap on), not the group's full combined date range, and
 * instructs the model to return exactly that many days. tripDays is the
 * organizer's actually-requested trip length (see lib/tripDates.ts's
 * resolveTripDays) - a 1-day request must produce a 1-day itinerary, not
 * a hardcoded 5.
 */
function buildSkeletonPrompt(summary: PollSummary, tripWindow: TripDateWindow, tripDays: number): string {
  const vibesText =
    summary.topVibes.length > 0
      ? summary.topVibes.map((v) => `${v.vibe} (${v.count} votes)`).join(", ")
      : "No vibes selected yet.";
  const wishlistText =
    summary.wishlist.length > 0
      ? summary.wishlist.join("; ")
      : "No specific places suggested.";
  const votersText = summary.voters.length > 0 ? summary.voters.join(", ") : "the group";
  const nightsText = tripDays > 1 ? `${tripDays - 1} nights` : "no overnight stay";

  return (
    "Act as an Expert Travel Planner. Plan the STRUCTURE of a trip itinerary based on this consensus " +
    "data from a group trip poll - a full, realistic, well-paced day, not a bare-minimum outline. You " +
    "will fill in real venue names in a later step - for now, decide only what KIND of place each part " +
    "of the day should be (or, for an exact wishlist request, WHICH exact place - see the STRICT RULE " +
    "below), and roughly where. Infer a specific real destination city from the group's requested " +
    "places and vibes. Resolve conflicting wishes by prioritizing the most popular vibes. Do not " +
    "reference how the group gets to the destination or where they are coming from - start the " +
    "itinerary from arrival.\n\n" +
    `Group size: ${summary.totalVotes} people (${votersText}).\n` +
    `Top vibes, most to least popular: ${vibesText}.\n` +
    `Specific places requested by the group: ${wishlistText}.\n\n` +
    `This trip is FIXED at exactly ${tripDays} day${tripDays === 1 ? "" : "s"} / ${nightsText}, ` +
    `from ${tripWindow.startDate} to ${tripWindow.endDate} inclusive - the specific window that the ` +
    `most voters (${tripWindow.voterCount} of ${summary.totalVotes}) can make, not the group's full ` +
    "combined date range. You MUST return EXACTLY " +
    `${tripDays} day object${tripDays === 1 ? "" : "s"} in the "days" array, numbered 1 through ` +
    `${tripDays} in calendar order matching that window - never more, never fewer. The itinerary you ` +
    `produce, end to end, MUST cover exactly ${tripDays} day${tripDays === 1 ? "" : "s"} - not fewer, ` +
    "not more - regardless of how the group's individual votes were worded.\n\n" +
    "For each day, structure it explicitly across Morning, Afternoon, and Evening, with AT LEAST 4 to " +
    "6 stops total across the day (activities and meals combined) - a sparse day with only 1-3 stops " +
    "is NOT acceptable. Fill logical gaps yourself: if there's a lull between two stops (for example " +
    "after an active outing and before dinner), proactively insert a nearby cafe, gallery, park, or " +
    "rest stop rather than leaving a long unplanned gap - a well-paced day has no dead time. For each " +
    "slot, give: slotType (\"activity\" or \"meal\"), a short search category such as \"cafe\", " +
    "\"temple\", \"night market\", \"Thai restaurant\", \"viewpoint\", or \"museum\" (NOT a specific " +
    "venue name), and the neighborhood or area to look near.\n\n" +
    "STRICT RULE - exact requests: if the group's wishlist above names a SPECIFIC place rather than a " +
    "general category (a proper noun someone would search by name, e.g. \"คัตสึยะ\" or \"Central " +
    "World\"), you MUST create a slot for that exact place and set specificPlaceName to the venue's " +
    "real name, romanized/translated into however it would appear in a map search (e.g. \"คัตสึยะ\" -> " +
    "\"Katsuya\") - do NOT generalize it into a category like \"Japanese restaurant\" instead. For " +
    "every other, ordinary slot, leave specificPlaceName as an empty string and rely on category+area " +
    "as usual - do not invent or guess a specific venue name for those."
  );
}

async function generateSkeleton(
  summary: PollSummary,
  tripWindow: TripDateWindow,
  tripDays: number
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
    messages: [{ role: "user", content: buildSkeletonPrompt(summary, tripWindow, tripDays) }],
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
  if (skeleton.days.length !== tripDays) {
    console.error(
      `[trigger-jarvis] skeleton returned ${skeleton.days.length} days, expected ${tripDays} ` +
        `(window ${tripWindow.startDate} to ${tripWindow.endDate}).`
    );
    if (skeleton.days.length > tripDays) {
      return { ...skeleton, days: skeleton.days.slice(0, tripDays) };
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
  // Carried through from ActivitySlotSchema so formatSlotForPrompt (Stage
  // 2) can flag this slot as a specific wishlist request - see that
  // schema field's docstring.
  specificPlaceName: string;
  places: PlaceResult[];
}

/**
 * A slot with specificPlaceName set is searched BY THAT NAME directly
 * (Text Search for "<name>, <destination>"), never by category+area -
 * this is what actually makes the STRICT RULE in buildSkeletonPrompt
 * bite: a wishlist venue gets grounded in real Places data under its own
 * name instead of being genericized into "Japanese restaurant" the way
 * an ordinary category slot is.
 */
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
      places: slot.specificPlaceName
        ? await searchPlacesText(`${slot.specificPlaceName}, ${skeleton.destination}`)
        : await searchPlacesForSlot(slot.category, slot.area, skeleton.destination),
    }))
  );
}

/**
 * The first real, geocoded venue found anywhere in the grounded slots -
 * used as a stand-in "destination location" for the weather lookup
 * (Stage 2.5), since this app never geocodes the destination string
 * itself. Good enough for a same-city trip's average forecast; not meant
 * to be the mathematically precise city center.
 */
function findRepresentativeLocation(groundedSlots: GroundedSlot[]): LatLng | null {
  for (const slot of groundedSlots) {
    const location = slot.places[0]?.location;
    if (location) return location;
  }
  return null;
}

// ---------------------------------------------------------------------
// Stage 2: write the final itinerary, choosing only from real results.
// ---------------------------------------------------------------------

function formatSlotForPrompt(slot: GroundedSlot, slotPosition: number): string {
  const requestedNote = slot.specificPlaceName
    ? ` [USER-REQUESTED: "${slot.specificPlaceName}" - the group asked for this exact place by name]`
    : "";
  if (slot.places.length === 0) {
    return (
      `  - Stop ${slotPosition} [${slot.slotType}]${requestedNote} ${slot.category} near ${slot.area}: ` +
      "NO REAL VENUES FOUND. Say so plainly in this stop's text rather than inventing one, set " +
      "placeIndex to -1, and set estimatedCostPerPerson to null."
    );
  }
  const candidates = slot.places
    .slice(0, 3)
    .map((p, i) => {
      const ratingText = p.rating !== null ? `, rating ${p.rating}` : "";
      const priceText = p.priceLevel
        ? `, price level ${p.priceLevel.replace("PRICE_LEVEL_", "").toLowerCase()}`
        : "";
      return `${i}=${p.name} (${p.address}${ratingText}${priceText})`;
    })
    .join("; ");
  return `  - Stop ${slotPosition} [${slot.slotType}]${requestedNote} ${slot.category} near ${slot.area}, candidates: ${candidates}`;
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
    "text (a short one-sentence description, incorporating the chosen venue's real name), placeIndex " +
    "(the candidate number - 0, 1, or 2 - that your text is about), and estimatedCostPerPerson (a " +
    "realistic estimated cost per person for that activity or meal, as a plain number, using the " +
    "venue's price level above as a guide where one is shown). If a Stop says NO REAL VENUES FOUND, " +
    "say so plainly in that stop's text rather than inventing a fallback, set placeIndex to -1, and " +
    "set estimatedCostPerPerson to null. For a Stop marked USER-REQUESTED, use that candidate even if " +
    "its category label doesn't perfectly match, since the group explicitly asked for it by name.\n\n" +
    `Also return currency: the ISO 4217 currency code actually used day-to-day in ${skeleton.destination} ` +
    "(e.g. \"THB\", \"USD\", \"JPY\") - every estimatedCostPerPerson value above must be a realistic " +
    "amount in this same currency, not USD by default.\n\n" +
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
// Routes API travel-time/distance estimate (dynamic per-leg mode - see
// lib/routes.ts), and attach a Stage 2.6 weather summary for the trip's
// window. Both run entirely in code, after Stage 2 - never routed
// through another LLM call.
// ---------------------------------------------------------------------

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
 * Attaches Stage 2.5 travel data and per-stop cost to every day, calling
 * Routes API for every day in parallel (Promise.all, not sequential) -
 * each day is an independent request, and there's no reason one day's
 * routing should wait on another's round trip.
 *
 * Within a day, only stops with a resolvable location (see
 * resolveStopLocation) are sent to Routes API, preserving their
 * original stop-array positions - a stop with no coordinates (no real
 * venue found for that slot, or Google had no location for the chosen
 * one) simply can't anchor a leg on either side of it, so it always gets
 * travelFromPrevious: null, and the NEXT geocoded stop's travel time (if
 * any) is computed from the last geocoded stop before it, skipping the
 * gap - the closest honest estimate available rather than omitting that
 * leg too. A single leg's own routing failure (see lib/routes.ts's
 * computeDayRoute) degrades that one leg to travelFromPrevious: null,
 * never the whole day - it must never fail the itinerary this route
 * already spent real Anthropic and Places quota generating.
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
          const leg = legs[k];
          if (leg) {
            travelByStopIndex.set(geocodedIndices[k + 1], leg);
          }
        }
      }

      const stops: ItineraryStop[] = day.stops.map((stop, i) => ({
        slotType: stop.slotType,
        text: stop.text,
        travelFromPrevious: travelByStopIndex.get(i) ?? null,
        location: locations[i],
        estimatedCostPerPerson: stop.estimatedCostPerPerson,
      }));

      return { day: day.day, summary: day.summary, stops };
    })
  );

  const built: Itinerary = {
    destination: itinerary.destination,
    days,
    notes: itinerary.notes,
    startDate: tripWindow.startDate,
    endDate: tripWindow.endDate,
    currency: itinerary.currency,
  };
  built.totalTripEstimatedCost = computeTotalEstimatedCost(built);
  return built;
}

/**
 * POST /api/trigger-jarvis - "Lock & Generate Plan" on the poll page.
 *
 * Self-contained: reads this trip's votes from Neon via Prisma, then
 * runs a grounded pipeline - a Haiku call to plan the day structure
 * (categories + areas, no venue names; Stage 1), real Google Places
 * Text Search calls to find actual venues for every slot (Stage 1.5), a
 * second Haiku call to write the final itinerary choosing only from
 * that real data and estimating a per-stop cost (Stage 2), and finally,
 * in parallel: a real Google Routes API call per stop-to-stop leg with a
 * dynamically chosen travel mode (Stage 2.5, see
 * enrichItineraryWithTravelTimes and lib/routes.ts - never another LLM
 * call, this is real data attached as-is) and a real-world forecast
 * lookup for the trip's window (Stage 2.6, see lib/weather.ts) - and
 * returns the finished itinerary in the response body. No dependency on
 * Jarvis's local Python backend, which never accepts inbound
 * connections.
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
  // The organizer's actually-requested trip length, forwarded from the
  // ?days=<n> param on their admin link (see build_liff_link's admin URL
  // in plugins/trip_planner.py's _run_consensus_poll, and this same
  // field read in app/trip/poll/[id]/page.tsx) - resolveTripDays clamps
  // and defaults it, so a missing/malformed value never breaks
  // generation, it just falls back to DEFAULT_TRIP_DAYS.
  const tripDays = resolveTripDays(body?.days);

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

      let stageStart = Date.now();
      const skeleton = await generateSkeleton(summary, tripWindow, tripDays);
      console.log(`[trigger-jarvis] ${tripId}: Stage 1 (skeleton) ${Date.now() - stageStart}ms`);

      stageStart = Date.now();
      const groundedSlots = await groundSkeleton(skeleton);
      console.log(`[trigger-jarvis] ${tripId}: Stage 1.5 (Places) ${Date.now() - stageStart}ms`);

      stageStart = Date.now();
      const draftItinerary = await generateFinalItinerary(skeleton, groundedSlots);
      console.log(`[trigger-jarvis] ${tripId}: Stage 2 (final write) ${Date.now() - stageStart}ms`);

      stageStart = Date.now();
      const representativeLocation = findRepresentativeLocation(groundedSlots);
      const [itinerary, weather] = await Promise.all([
        enrichItineraryWithTravelTimes(draftItinerary, groundedSlots, tripWindow),
        representativeLocation
          ? fetchWeatherSummary(representativeLocation, tripWindow.startDate, tripWindow.endDate)
          : Promise.resolve(null),
      ]);
      if (weather) {
        itinerary.weather = weather;
      }
      console.log(`[trigger-jarvis] ${tripId}: Stage 2.5 (Routes + weather) ${Date.now() - stageStart}ms`);

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
