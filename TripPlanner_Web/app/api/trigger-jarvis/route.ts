import Anthropic from "@anthropic-ai/sdk";
import { zodOutputFormat } from "@anthropic-ai/sdk/helpers/zod";
import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import { verifyBearerLineToken } from "@/lib/lineAuth";
import { PlacesApiError, searchPlacesForSlot, type PlaceResult } from "@/lib/places";
import { checkRateLimit } from "@/lib/rateLimit";
import {
  claimPollForGeneration,
  getDraft,
  getPollVotes,
  lockPoll,
  releasePollClaim,
  saveDraft,
} from "@/lib/store";
import { summarizePollVotes, type PollSummary } from "@/lib/tripSummary";

// Two Haiku calls plus N parallel Places calls. Measured end to end
// against a real successful run (19 slots, 0 empty): stage 1 (skeleton)
// 3.9s, stage 1.5 (Places, all in parallel) 0.7s, stage 2 (final write)
// 10.3s - total ~15s, comfortably inside this ceiling.
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
// Final itinerary shape - unchanged from the single-call version, and
// the same shape as plugins/trip_planner.py's Itinerary Pydantic model.
// ---------------------------------------------------------------------

const ItineraryDaySchema = z.object({
  day: z.number(),
  summary: z.string(),
  activities: z.array(z.string()),
  meals: z.array(z.string()),
});

const ItinerarySchema = z.object({
  destination: z.string(),
  days: z.array(ItineraryDaySchema),
  notes: z.string(),
});

type Itinerary = z.infer<typeof ItinerarySchema>;

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
 */
function buildSkeletonPrompt(summary: PollSummary): string {
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
    `Preferred dates: ${summary.dateRangeLabel}.\n` +
    `Top vibes, most to least popular: ${vibesText}.\n` +
    `Specific places requested by the group: ${wishlistText}.\n\n` +
    "For each day, break it into a small number of slots - roughly 3-4 activities and 2 meals per " +
    "day is a reasonable density, not more. For each slot, give: slotType (\"activity\" or \"meal\"), " +
    "a short search category such as \"cafe\", \"temple\", \"night market\", \"Thai restaurant\", " +
    "\"viewpoint\", or \"museum\" - NOT a specific venue name - and the neighborhood or area to look " +
    "near. Do not invent or guess any specific venue name at this stage."
  );
}

async function generateSkeleton(summary: PollSummary): Promise<ItinerarySkeleton> {
  const apiKey = process.env.ANTHROPIC_API_KEY;
  if (!apiKey) {
    throw new Error("ANTHROPIC_API_KEY is not configured.");
  }

  const client = new Anthropic({ apiKey });
  const response = await client.messages.parse({
    model: FINALIZE_MODEL,
    max_tokens: FINALIZE_MAX_TOKENS,
    output_config: { format: zodOutputFormat(ItinerarySkeletonSchema) },
    messages: [{ role: "user", content: buildSkeletonPrompt(summary) }],
  });

  if (!response.parsed_output) {
    throw new Error("Claude did not return a valid itinerary skeleton.");
  }
  return response.parsed_output;
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

function formatSlotForPrompt(slot: GroundedSlot): string {
  if (slot.places.length === 0) {
    return `  - [${slot.slotType}] ${slot.category} near ${slot.area}: NO REAL VENUES FOUND. Say so plainly in this slot's text rather than inventing one.`;
  }
  const candidates = slot.places
    .slice(0, 3)
    .map((p) => `${p.name} (${p.address}${p.rating !== null ? `, rating ${p.rating}` : ""})`)
    .join("; ");
  return `  - [${slot.slotType}] ${slot.category} near ${slot.area}: ${candidates}`;
}

function buildFinalPrompt(skeleton: ItinerarySkeleton, groundedSlots: GroundedSlot[]): string {
  const daysText = skeleton.days
    .map((day) => {
      const daySlots = groundedSlots.filter((s) => s.day === day.day);
      return `Day ${day.day} (${day.theme}):\n${daySlots.map(formatSlotForPrompt).join("\n")}`;
    })
    .join("\n\n");

  return (
    `Write the final trip itinerary for ${skeleton.destination}, following this planned structure, ` +
    "using the REAL Google Places search results listed for each slot below. Only use venues from " +
    "the provided search results - never invent a name not present in this data. If a slot says NO " +
    "REAL VENUES FOUND, say so plainly in that slot's activity or meal text rather than inventing a " +
    "fallback. For each slot, pick one of its real results and write a short one-sentence " +
    "description incorporating it.\n\n" +
    `${daysText}`
  );
}

async function generateFinalItinerary(
  skeleton: ItinerarySkeleton,
  groundedSlots: GroundedSlot[]
): Promise<Itinerary> {
  const apiKey = process.env.ANTHROPIC_API_KEY;
  if (!apiKey) {
    throw new Error("ANTHROPIC_API_KEY is not configured.");
  }

  const client = new Anthropic({ apiKey });
  const response = await client.messages.parse({
    model: FINALIZE_MODEL,
    max_tokens: FINALIZE_MAX_TOKENS,
    output_config: { format: zodOutputFormat(ItinerarySchema) },
    messages: [{ role: "user", content: buildFinalPrompt(skeleton, groundedSlots) }],
  });

  if (!response.parsed_output) {
    throw new Error("Claude did not return a valid itinerary.");
  }
  return response.parsed_output;
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
 * Best-effort caller IP for rate-limiting an anonymous trigger, which has
 * no lineUserId to key by - same helper as app/api/poll/[id]/route.ts's
 * clientIp, duplicated rather than shared since each route already keeps
 * its own small local helpers (see e.g. that route's verifyHmacSignature).
 */
function clientIp(request: NextRequest): string {
  const forwarded = request.headers.get("x-forwarded-for");
  return forwarded ? forwarded.split(",")[0].trim() : "unknown";
}

/**
 * POST /api/trigger-jarvis - "Lock & Generate Plan" on the poll page.
 *
 * Self-contained: reads this trip's votes from Neon via Prisma, then
 * runs a grounded two-stage generation - a Haiku call to plan the day
 * structure (categories + areas, no venue names), real Google Places
 * Text Search calls to find actual venues for every slot, then a second
 * Haiku call to write the final itinerary choosing only from that real
 * data - and returns the finished itinerary in the response body. No
 * dependency on Jarvis's local Python backend, which never accepts
 * inbound connections.
 *
 * NOT YET DONE: pushing the result back into the LINE group chat the way
 * plugins/trip_planner.py's _finalize_trip_task does via line_notifier -
 * that needs LINE_CHANNEL_ACCESS_TOKEN and a target group id configured
 * in this Next.js app's own environment (a group id specifically, not
 * just a personal LINE_USER_ID push target - out of scope for now). The
 * frontend receiving the plan directly in this response is today's
 * substitute.
 *
 * A LINE session is optional here, not required - matching POST
 * /api/poll/[id]'s voting path. A header that IS present must still
 * check out (a present-but-bad token fails loudly, same reasoning as
 * voting), but a request with no Authorization header at all proceeds
 * anonymously. This used to be a hard requirement specifically to stop
 * repeated-quota-burning abuse of a trip's poll link - but
 * claimPollForGeneration below is what actually prevents that now: only
 * one generation can ever succeed per trip regardless of who calls this,
 * and a made-up trip_id with no real votes is rejected before any paid
 * work runs. A verified identity on top of that added little real
 * protection while reintroducing the exact LIFF fragility that was
 * deliberately removed from voting earlier in this project - see
 * lockPoll's lockedByLineUserId for the (metadata-only, not
 * access-control) record of who triggered a given generation.
 */
export async function POST(request: NextRequest) {
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

  const rateLimitKey = lineUserId ? `trigger:${lineUserId}` : `trigger:anon:${clientIp(request)}`;
  const rateLimit = checkRateLimit(rateLimitKey, TRIGGER_RATE_LIMIT, TRIGGER_RATE_WINDOW_MS);
  if (!rateLimit.allowed) {
    return NextResponse.json(
      { error: "Too many plan-generation requests - please wait a few minutes and try again." },
      { status: 429, headers: { "Retry-After": String(rateLimit.retryAfterSeconds) } }
    );
  }

  const body = await request.json().catch(() => null);
  const tripId = typeof body?.trip_id === "string" ? body.trip_id.trim() : "";

  if (!tripId) {
    return NextResponse.json({ error: "trip_id is required." }, { status: 400 });
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

      const summary = summarizePollVotes(votes);
      const skeleton = await generateSkeleton(summary);
      const groundedSlots = await groundSkeleton(skeleton);
      const itinerary = await generateFinalItinerary(skeleton, groundedSlots);

      await saveDraft(tripId, formatItineraryForStorage(itinerary));
      await lockPoll(tripId, lineUserId);

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
    // TEMPORARY DIAGNOSTIC: production returns this generic 500 for a
    // reproducible failure that never happens locally against the same
    // database - surfacing the real error/stack to find out why instead
    // of guessing. Revert to the generic message once found.
    return NextResponse.json(
      {
        error: "Something went wrong while generating the plan. Please try again.",
        debug: {
          name: error instanceof Error ? error.name : typeof error,
          message: error instanceof Error ? error.message : String(error),
          stack: error instanceof Error ? error.stack : undefined,
        },
      },
      { status: 500 }
    );
  }
}
