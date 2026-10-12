/**
 * The grounded trip-generation pipeline: from "what do the travelers want"
 * to a finished, stored-shape Itinerary. Shared by the two routes that
 * generate a plan - POST /api/trigger-jarvis (a group poll being locked) and
 * POST /api/trip/create (Jarvis planning one trip straight from the chat) -
 * so both build plans the same way and a change to a prompt lands in both.
 *
 * Two LLM calls, N parallel Places calls, a Routes call per leg and a weather
 * call, in these stages (all of it moved here unchanged from the trigger-jarvis
 * route, where the measured timings below were taken):
 *
 *   Stage 1    skeleton: what KIND of place each part of each day is, and
 *              roughly where - never a venue name.
 *   Stage 1.5  ground every slot in real Google Places results.
 *   Stage 2    final write: choose among the real candidates (by placeIndex)
 *              and write a name plus one short caption per stop.
 *   Stage 2.5  real Routes travel times and a weather summary, in code.
 *
 * Measured end to end against real successful Chiang Mai runs: stage 1
 * 3.5-4.1s, stage 1.5 0.5-0.7s, stage 2 10.1-14.2s, stage 2.5 well under a
 * second regardless of trip length - stage 2's Haiku call is what dominates.
 *
 * PERSONALIZATION. Both prompts can carry a Taste Profile block
 * (lib/tasteSummary.ts): Stage 1 builds the days around the themes the
 * travelers love, Stage 2 uses it to choose between candidates. With no taste
 * (null) every prompt is built exactly as it was before taste existed, so a
 * trip with no profile is generated as it always was - keep it that way: the
 * block is one optional paragraph, never a reworded prompt.
 */

import Anthropic from "@anthropic-ai/sdk";
import { zodOutputFormat } from "@anthropic-ai/sdk/helpers/zod";
import { z } from "zod";
import { FINALIZE_MODEL, FINALIZE_MAX_TOKENS } from "./llm";
import { computeTotalEstimatedCost, type Itinerary, type ItineraryStop } from "./itinerary";
import { searchPlacesForSlot, searchPlacesText, type LatLng, type PlaceResult } from "./places";
import { computeDayRoute, computeDepartureTimeForDay, type TravelLeg } from "./routes";
import { clampShortDescription, composeStopText, namesAgree } from "./stopCopy";
import { tasteFinalBlock, tasteSkeletonBlock, type TasteSummary } from "./tasteSummary";
import type { TripDateWindow } from "./tripDates";
import type { PollSummary } from "./tripSummary";
import { fetchWeatherSummary } from "./weather";

/**
 * A Taste Profile plus how many travelers the plan is for, so a prompt can say
 * "2 of its 4 travelers took the quiz" instead of presenting two people's
 * answers as the whole group's. `travelers` is omitted for a lone traveler.
 */
export interface TastePromptInput {
  taste: TasteSummary;
  travelers?: number;
}

/**
 * Thrown by a fixed-destination generation when Google Places found no real
 * venue for ANY slot - almost always a destination that is not a place
 * ("asdf"), or one Places cannot resolve. Spending Stage 2 and storing a plan
 * made only of "no venue found" stops would be worse than saying so.
 */
export class DestinationNotFoundError extends Error {
  constructor(public readonly destination: string) {
    super(`No real places were found for "${destination}".`);
    this.name = "DestinationNotFoundError";
  }
}

// ---------------------------------------------------------------------
// Stage 2's LLM-facing output shape - structurally similar to the
// persisted Itinerary (lib/itinerary.ts), but stops carry placeIndex
// (which of that slot's grounded candidates Claude used) instead of
// travelFromPrevious/location - those are attached afterward, in code,
// by enrichItineraryWithTravelTimes (Stage 2.5), never narrated by the
// model.
//
// A stop is a NAME and ONE brief caption, never a paragraph: the plan view
// shows the name in bold with the caption under it. A JSON schema can't
// hold the model to a word count (and a zod .max() would fail the whole
// generation the first time Haiku overshot it), so the limit is stated
// three times instead - in these field descriptions, in buildFinalPrompt,
// and in code (lib/stopCopy.ts's clampShortDescription, applied to
// whatever comes back). The fields are ordered so the model commits to the
// venue (placeIndex) before it writes about it.
// ---------------------------------------------------------------------

const ItineraryStopLLMSchema = z.object({
  slotType: z.enum(["activity", "meal"]),
  // 0-based index into the slot's candidate list (see formatSlotForPrompt)
  // indicating exactly which real venue this stop is. -1 means "no real
  // venue was available for this slot" (formatSlotForPrompt's NO REAL
  // VENUES FOUND case) - any value outside the slot's actual candidate
  // range is treated the same way by enrichItineraryWithTravelTimes, never
  // trusted blindly.
  placeIndex: z.number().int(),
  // The chosen candidate's name as listed. Used as a cross-check only: the
  // name that is stored is the Places candidate's own (see
  // enrichItineraryWithTravelTimes), so a misspelled or invented one here
  // can't reach the plan.
  placeName: z
    .string()
    .describe("The chosen candidate's name copied exactly as listed, nothing else. Empty string when placeIndex is -1."),
  shortDescription: z
    .string()
    .describe(
      "ONE brief sentence of at most 15 words (aim for 8 to 12): what to do or why it is worth the stop. " +
        "No second sentence, no list, no semicolon, no trailing period, and never the place name itself."
    ),
  // A realistic per-person cost in `currency` below, grounded in the
  // candidate's real Places priceLevel (see formatSlotForPrompt) - null
  // for a stop with no real venue to estimate from.
  estimatedCostPerPerson: z.number().nullable(),
});

const ItineraryDayLLMSchema = z.object({
  day: z.number(),
  summary: z
    .string()
    .describe("A label of at most 6 words naming the day's theme, such as 'Old City and night markets'. Not a sentence."),
  stops: z.array(ItineraryStopLLMSchema),
});

const ItineraryLLMSchema = z.object({
  destination: z.string(),
  days: z.array(ItineraryDayLLMSchema),
  notes: z
    .string()
    .describe("Empty, or at most two short sentences (30 words in all) of practical advice such as dress code or booking."),
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

// The day-structure rules every Stage 1 prompt carries, whoever the plan is
// for: kept in one place so the poll prompt and the fixed-destination prompt
// can't drift apart on how full a day must be.
const SKELETON_STRUCTURE_RULES =
  "For each day, structure it explicitly across Morning, Afternoon, and Evening, with AT LEAST 4 to " +
  "6 stops total across the day (activities and meals combined) - a sparse day with only 1-3 stops " +
  "is NOT acceptable. Fill logical gaps yourself: if there's a lull between two stops (for example " +
  "after an active outing and before dinner), proactively insert a nearby cafe, gallery, park, or " +
  "rest stop rather than leaving a long unplanned gap - a well-paced day has no dead time. For each " +
  "slot, give: slotType (\"activity\" or \"meal\"), a short search category such as \"cafe\", " +
  "\"temple\", \"night market\", \"Thai restaurant\", \"viewpoint\", or \"museum\" (NOT a specific " +
  "venue name), and the neighborhood or area to look near.";

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
 *
 * `taste` (the voters' combined Taste Profile) adds one paragraph between the
 * group's own requests and the date rules; null leaves the prompt exactly as it
 * was before taste existed.
 */
export function buildSkeletonPrompt(
  summary: PollSummary,
  tripWindow: TripDateWindow,
  tripDays: number,
  taste: TastePromptInput | null = null
): string {
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
  const tasteText = taste ? `${tasteSkeletonBlock(taste.taste, taste.travelers)}\n\n` : "";

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
    tasteText +
    `This trip is FIXED at exactly ${tripDays} day${tripDays === 1 ? "" : "s"} / ${nightsText}, ` +
    `from ${tripWindow.startDate} to ${tripWindow.endDate} inclusive - the specific window that the ` +
    `most voters (${tripWindow.voterCount} of ${summary.totalVotes}) can make, not the group's full ` +
    "combined date range. You MUST return EXACTLY " +
    `${tripDays} day object${tripDays === 1 ? "" : "s"} in the "days" array, numbered 1 through ` +
    `${tripDays} in calendar order matching that window - never more, never fewer. The itinerary you ` +
    `produce, end to end, MUST cover exactly ${tripDays} day${tripDays === 1 ? "" : "s"} - not fewer, ` +
    "not more - regardless of how the group's individual votes were worded.\n\n" +
    `${SKELETON_STRUCTURE_RULES}\n\n` +
    "STRICT RULE - exact requests: if the group's wishlist above names a SPECIFIC place rather than a " +
    "general category (a proper noun someone would search by name, e.g. \"คัตสึยะ\" or \"Central " +
    "World\"), you MUST create a slot for that exact place and set specificPlaceName to the venue's " +
    "real name, romanized/translated into however it would appear in a map search (e.g. \"คัตสึยะ\" -> " +
    "\"Katsuya\") - do NOT generalize it into a category like \"Japanese restaurant\" instead. For " +
    "every other, ordinary slot, leave specificPlaceName as an empty string and rely on category+area " +
    "as usual - do not invent or guess a specific venue name for those."
  );
}

/**
 * The Stage 1 prompt for a trip whose destination is already known - Jarvis's
 * "plan a trip to Chiang Mai", where nobody voted and there is no wishlist to
 * infer a city from. Same day-structure rules as buildSkeletonPrompt; what
 * differs is that the destination is given (not inferred), the group is just a
 * head count, and there are no named places, so every slot is an ordinary
 * category + area one.
 *
 * `tripWindow` is null when nobody chose dates: the prompt then says so rather
 * than inventing a calendar for the model to plan around.
 */
export function buildFixedSkeletonPrompt(input: {
  destination: string;
  tripDays: number;
  headCount: number;
  tripWindow: TripDateWindow | null;
  taste: TastePromptInput | null;
}): string {
  const { destination, tripDays, headCount, tripWindow, taste } = input;
  const nightsText = tripDays > 1 ? `${tripDays - 1} nights` : "no overnight stay";
  const datesText = tripWindow
    ? `from ${tripWindow.startDate} to ${tripWindow.endDate} inclusive`
    : "with no calendar dates chosen yet";
  const tasteText = taste ? `${tasteSkeletonBlock(taste.taste, taste.travelers)}\n\n` : "";

  return (
    `Act as an Expert Travel Planner. Plan the STRUCTURE of a trip itinerary for ${destination} - a ` +
    "full, realistic, well-paced day, not a bare-minimum outline. You will fill in real venue names " +
    "in a later step - for now, decide only what KIND of place each part of the day should be, and " +
    "roughly where. Do not reference how the traveler gets to the destination or where they are " +
    "coming from - start the itinerary from arrival.\n\n" +
    `Destination: ${destination}. Return it in the "destination" field as the place's proper name ` +
    "(for example \"Chiang Mai, Thailand\").\n" +
    `Group size: ${headCount} ${headCount === 1 ? "person" : "people"}.\n\n` +
    tasteText +
    `This trip is FIXED at exactly ${tripDays} day${tripDays === 1 ? "" : "s"} / ${nightsText}, ` +
    `${datesText}. You MUST return EXACTLY ${tripDays} day object${tripDays === 1 ? "" : "s"} in the ` +
    `"days" array, numbered 1 through ${tripDays} in order - never more, never fewer.\n\n` +
    `${SKELETON_STRUCTURE_RULES}\n\n` +
    "Nobody has named a specific place, so leave specificPlaceName as an empty string for every " +
    "slot and rely on category+area - do not invent or guess a specific venue name."
  );
}

async function generateSkeleton(
  prompt: string,
  tripDays: number,
  windowLabel: string,
  logTag: string
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
    messages: [{ role: "user", content: prompt }],
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
      `[${logTag}] skeleton returned ${skeleton.days.length} days, expected ${tripDays} (${windowLabel}).`
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
      "NO REAL VENUES FOUND. Say so plainly in this stop's shortDescription rather than inventing " +
      "one, set placeIndex to -1, placeName to an empty string, and estimatedCostPerPerson to null."
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
 *
 * Brevity is stated as a hard rule for EVERY field the model writes, not
 * just the caption: Stage 2 used to return a free sentence-or-more per stop
 * plus a paragraph per day, and the plan view had to render all of it. Now
 * a stop is a name and one caption of at most 15 words, a day's summary is
 * a six-word label, and notes are two short sentences at most - see
 * ItineraryStopLLMSchema for why the code backs this up as well.
 *
 * `taste` adds one paragraph just before the stops - how to choose between
 * candidates - and null leaves the prompt as it was before taste existed.
 */
export function buildFinalPrompt(
  skeleton: ItinerarySkeleton,
  groundedSlots: GroundedSlot[],
  taste: TastePromptInput | null = null
): string {
  const daysText = skeleton.days
    .map((day) => {
      const daySlots = groundedSlots.filter((s) => s.day === day.day);
      const slotsText = daySlots.map((slot, i) => formatSlotForPrompt(slot, i)).join("\n");
      return `Day ${day.day} (${day.theme}):\n${slotsText}`;
    })
    .join("\n\n");
  const tasteText = taste ? `${tasteFinalBlock(taste.taste, taste.travelers)}\n\n` : "";

  return (
    `Write the final trip itinerary for ${skeleton.destination}, following this planned structure, ` +
    "using the REAL Google Places search results listed for each stop below. Only use venues from " +
    "the provided candidates - never invent a name not present in this data. For each day, return " +
    "exactly one stop object per Stop listed, IN THE SAME ORDER, with these fields:\n" +
    "- slotType: copy from the Stop.\n" +
    "- placeIndex: the candidate number (0, 1, or 2) you chose.\n" +
    "- placeName: that candidate's name copied EXACTLY as listed - the name only, no address, rating " +
    "or extra words.\n" +
    "- shortDescription: ONE brief sentence of at most 15 words (aim for 8 to 12) saying what to do " +
    "there or why it is worth the stop. It sits directly under the place name, so never repeat the " +
    "name in it. A second sentence, a list, a semicolon, a paragraph or a trailing period is a " +
    "mistake. Good: \"Walk the quiet forest tunnels and ponds\", \"Try the rich, creamy coconut khao " +
    "soi\", \"Browse local crafts and street snacks\".\n" +
    "- estimatedCostPerPerson: a realistic estimated cost per person for that activity or meal, as a " +
    "plain number, using the venue's price level above as a guide where one is shown.\n" +
    "If a Stop says NO REAL VENUES FOUND, set placeIndex to -1, placeName to an empty string, " +
    "shortDescription to a plain statement of at most 10 words that no real venue was found for it " +
    "(never invent a fallback), and estimatedCostPerPerson to null. For a Stop marked USER-REQUESTED, " +
    "use that candidate even if its category label doesn't perfectly match, since the group explicitly " +
    "asked for it by name.\n\n" +
    "Brevity is a hard rule for EVERYTHING you write: no field may contain a paragraph. Each day's " +
    "summary is a label of at most 6 words naming that day's theme (for example \"Old City and night " +
    "markets\"), not a sentence. notes is empty, or at most two short sentences (30 words in all) of " +
    "practical advice such as dress code or booking.\n\n" +
    `Also return currency: the ISO 4217 currency code actually used day-to-day in ${skeleton.destination} ` +
    "(e.g. \"THB\", \"USD\", \"JPY\") - every estimatedCostPerPerson value above must be a realistic " +
    "amount in this same currency, not USD by default.\n\n" +
    tasteText +
    `${daysText}`
  );
}

async function generateFinalItinerary(
  prompt: string
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
    messages: [{ role: "user", content: prompt }],
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
 * Resolves one LLM-returned stop's placeIndex back to the real Places
 * candidate it chose, or null if there isn't one - an out-of-range or -1
 * placeIndex, or a slot Stage 1.5 found zero candidates for. Never trusts
 * placeIndex blindly: Claude's structured output is schema-validated, not
 * content-validated, so an out-of-bounds integer is treated the same as
 * "no venue" rather than throwing. The stop's coordinates (which may still
 * be null - a candidate Google returned with no location) AND its stored
 * name come from this candidate, never from the model's own wording.
 */
function resolveStopPlace(stop: ItineraryStopLLM, slot: GroundedSlot | undefined): PlaceResult | null {
  if (!slot) return null;
  if (stop.placeIndex < 0 || stop.placeIndex >= slot.places.length) return null;
  return slot.places[stop.placeIndex];
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
 *
 * `tripWindow` is null for a trip nobody gave dates: the routing estimates are
 * then anchored to the same near-future fallback date an undated poll uses
 * (computeDepartureTimeForDay), and the stored plan carries NO startDate or
 * endDate - an invented calendar would read as a real one.
 */
async function enrichItineraryWithTravelTimes(
  itinerary: ItineraryLLM,
  groundedSlots: GroundedSlot[],
  tripWindow: TripDateWindow | null,
  logTag: string
): Promise<Itinerary> {
  const days = await Promise.all(
    itinerary.days.map(async (day) => {
      const daySlots = groundedSlots.filter((s) => s.day === day.day);
      const places = day.stops.map((stop, i) => resolveStopPlace(stop, daySlots[i]));
      const locations = places.map((place) => place?.location ?? null);

      const geocodedIndices = locations
        .map((loc, i) => (loc ? i : -1))
        .filter((i) => i >= 0);
      const geocodedCoords = geocodedIndices.map((i) => locations[i] as LatLng);

      const legs =
        geocodedCoords.length >= 2
          ? await computeDayRoute(
              geocodedCoords,
              computeDepartureTimeForDay(tripWindow?.startDate ?? null, day.day)
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

      const stops: ItineraryStop[] = day.stops.map((stop, i) => {
        // The stored name is the chosen candidate's own; the model's
        // placeName only cross-checks that its placeIndex and its words
        // agree (a mismatch is logged, never shown). With no candidate
        // behind the stop there is no name at all.
        const place = places[i];
        const placeName = place?.name ?? "";
        const shortDescription = clampShortDescription(stop.shortDescription, placeName);
        if (place && stop.placeName && !namesAgree(stop.placeName, place.name)) {
          console.warn(
            `[${logTag}] day ${day.day} stop ${i + 1}: the model wrote "${stop.placeName}" but ` +
              `placeIndex ${stop.placeIndex} is "${place.name}" - keeping the candidate's name.`
          );
        }

        return {
          slotType: stop.slotType,
          text: composeStopText(placeName, shortDescription),
          placeName: placeName || undefined,
          shortDescription: shortDescription || undefined,
          travelFromPrevious: travelByStopIndex.get(i) ?? null,
          location: locations[i],
          estimatedCostPerPerson: stop.estimatedCostPerPerson,
        };
      });

      return { day: day.day, summary: day.summary, stops };
    })
  );

  const built: Itinerary = {
    destination: itinerary.destination,
    days,
    notes: itinerary.notes,
    ...(tripWindow ? { startDate: tripWindow.startDate, endDate: tripWindow.endDate } : {}),
    currency: itinerary.currency,
  };
  built.totalTripEstimatedCost = computeTotalEstimatedCost(built);
  return built;
}

// ---------------------------------------------------------------------
// The whole pipeline
// ---------------------------------------------------------------------

export type GenerationInput =
  | {
      /** A group poll being locked: the destination is inferred from the votes. */
      kind: "poll";
      summary: PollSummary;
      /** Always dated - computeBestTripWindow falls back to a near-future one. */
      tripWindow: TripDateWindow;
      tripDays: number;
      taste: TastePromptInput | null;
    }
  | {
      /** One trip to a destination the caller already knows (Jarvis, from chat). */
      kind: "fixed";
      destination: string;
      headCount: number;
      /** null: nobody chose dates, and the plan will carry none. */
      tripWindow: TripDateWindow | null;
      tripDays: number;
      taste: TastePromptInput | null;
    };

/**
 * Runs Stage 1 -> 2.5 and returns the finished itinerary, ready for
 * formatItineraryForStorage. Persisting it is the caller's job (a poll is
 * locked, a fixed trip is created - see lib/store.ts), as is everything before
 * generation: authorization, rate limits, claims.
 *
 * `log` prefixes every line this writes: `tag` is the route's name and `id` the
 * trip, so a stage timing can be tied back to one request.
 *
 * Throws PlacesApiError when Google Places itself is broken, Anthropic's own
 * errors as they are, and DestinationNotFoundError (a "fixed" input only) when
 * no slot found any venue at all.
 */
export async function generateItinerary(
  input: GenerationInput,
  log: { tag: string; id: string }
): Promise<Itinerary> {
  const prefix = `[${log.tag}] ${log.id}:`;
  const windowLabel = input.tripWindow
    ? `window ${input.tripWindow.startDate} to ${input.tripWindow.endDate}`
    : "no dates chosen";

  const skeletonPrompt =
    input.kind === "poll"
      ? buildSkeletonPrompt(input.summary, input.tripWindow, input.tripDays, input.taste)
      : buildFixedSkeletonPrompt(input);

  let stageStart = Date.now();
  const skeleton = await generateSkeleton(skeletonPrompt, input.tripDays, windowLabel, log.tag);
  console.log(`${prefix} Stage 1 (skeleton) ${Date.now() - stageStart}ms`);

  stageStart = Date.now();
  const groundedSlots = await groundSkeleton(skeleton);
  console.log(`${prefix} Stage 1.5 (Places) ${Date.now() - stageStart}ms`);

  if (input.kind === "fixed" && groundedSlots.every((slot) => slot.places.length === 0)) {
    throw new DestinationNotFoundError(input.destination);
  }

  stageStart = Date.now();
  const draftItinerary = await generateFinalItinerary(buildFinalPrompt(skeleton, groundedSlots, input.taste));
  console.log(`${prefix} Stage 2 (final write) ${Date.now() - stageStart}ms`);

  stageStart = Date.now();
  const representativeLocation = findRepresentativeLocation(groundedSlots);
  const [itinerary, weather] = await Promise.all([
    enrichItineraryWithTravelTimes(draftItinerary, groundedSlots, input.tripWindow, log.tag),
    representativeLocation && input.tripWindow
      ? fetchWeatherSummary(representativeLocation, input.tripWindow.startDate, input.tripWindow.endDate)
      : Promise.resolve(null),
  ]);
  if (weather) {
    itinerary.weather = weather;
  }
  console.log(`${prefix} Stage 2.5 (Routes + weather) ${Date.now() - stageStart}ms`);

  return itinerary;
}
