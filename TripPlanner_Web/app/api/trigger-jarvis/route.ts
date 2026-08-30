import Anthropic from "@anthropic-ai/sdk";
import { zodOutputFormat } from "@anthropic-ai/sdk/helpers/zod";
import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import { verifyBearerLineToken } from "@/lib/lineAuth";
import { checkRateLimit } from "@/lib/rateLimit";
import { getDraft, getPollVotes, isPollLocked, lockPoll, saveDraft } from "@/lib/store";
import { summarizePollVotes, type PollSummary } from "@/lib/tripSummary";

// Generating an itinerary is one Anthropic call at output_config.effort
// "high" with adaptive thinking - comfortably longer than the platform's
// default function timeout. Raises the ceiling on this route only; see
// https://vercel.com/docs/functions/configuring-functions/duration.
export const maxDuration = 60;

// Locking a poll spends real LLM quota and (once LINE push-back exists -
// see this route's own docstring below) sends a LINE message - a much
// costlier action per call than casting a vote, so the allowance here is
// tight.
const TRIGGER_RATE_LIMIT = 3;
const TRIGGER_RATE_WINDOW_MS = 5 * 60_000;

// Mirrors plugins/trip_planner.py's FINALIZE_ANTHROPIC_MODEL and
// _FINALIZE_MAX_TOKENS - the same one-shot, per-poll, high-effort call,
// just run directly from this Vercel function instead of proxying to a
// Jarvis-side webhook receiver that was never built (see the module
// docstring there: "once that webhook receiver exists - it does not yet").
// Vercel's serverless functions cannot reach Jarvis's local Python
// backend inbound - it never accepts inbound connections by design, it
// only ever calls OUT (to LINE, Maps, Gemini/Anthropic) - so this route
// is now the actual finalization implementation, not a proxy to one.
const FINALIZE_MODEL = "claude-opus-5";
const FINALIZE_MAX_TOKENS = 4096;

const ItineraryDaySchema = z.object({
  day: z.number(),
  summary: z.string(),
  activities: z.array(z.string()),
  meals: z.array(z.string()),
});

// The same shape as plugins/trip_planner.py's Itinerary Pydantic model -
// one definition of "what an itinerary looks like" per language, mirrored
// rather than shared since this is a separate runtime. Passed as
// output_config.format below, so Claude's response is constrained to this
// shape at the API level (this project's TypeScript side has no
// tool_choice-forced-tool_use path the way the Python side does - the SDK's
// messages.parse + Zod is the equivalent structured-output guarantee).
const ItinerarySchema = z.object({
  destination: z.string(),
  days: z.array(ItineraryDaySchema),
  notes: z.string(),
});

type Itinerary = z.infer<typeof ItinerarySchema>;

/**
 * Mirrors plugins/trip_planner.py's _build_finalization_prompt byte-for-
 * byte in intent - reads the same pre-aggregated PollSummary shape
 * (lib/tripSummary.ts's summarizePollVotes, the same function GET
 * /api/poll/[id] already calls) rather than re-tallying raw votes, so the
 * tallying logic exists in exactly one place in this language too.
 *
 * Unlike the Python version, this prompt does NOT ask for "ONLY a JSON
 * object" - output_config.format below constrains the response shape at
 * the API level, so there is nothing for the model to get wrong or wrap
 * in markdown fences.
 */
function buildFinalizationPrompt(summary: PollSummary): string {
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
    "Generate a complete trip itinerary based on this consensus data from a " +
    "group trip poll. Resolve any conflicting wishes by prioritizing the " +
    "most popular vibes. Do not reference how the group gets to the " +
    "destination or where they are coming from - start the itinerary from " +
    "arrival.\n\n" +
    `Group size: ${summary.totalVotes} people (${votersText}).\n` +
    `Preferred dates: ${summary.dateRangeLabel}.\n` +
    `Top vibes, most to least popular: ${vibesText}.\n` +
    `Specific places requested by the group: ${wishlistText}.`
  );
}

async function generateItinerary(summary: PollSummary): Promise<Itinerary> {
  const apiKey = process.env.ANTHROPIC_API_KEY;
  if (!apiKey) {
    throw new Error("ANTHROPIC_API_KEY is not configured.");
  }

  const client = new Anthropic({ apiKey });
  const response = await client.messages.parse({
    model: FINALIZE_MODEL,
    max_tokens: FINALIZE_MAX_TOKENS,
    thinking: { type: "adaptive" },
    output_config: { effort: "high", format: zodOutputFormat(ItinerarySchema) },
    messages: [{ role: "user", content: buildFinalizationPrompt(summary) }],
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
 * POST /api/trigger-jarvis - "Lock & Generate Plan" on the poll page.
 *
 * Self-contained: reads this trip's votes from Neon via Prisma, calls
 * Anthropic directly, and returns the finished itinerary in the response
 * body - no dependency on Jarvis's local Python backend, which never
 * accepts inbound connections (see FINALIZE_MODEL's comment above).
 *
 * NOT YET DONE: pushing the result back into the LINE group chat the way
 * plugins/trip_planner.py's _finalize_trip_task does via line_notifier -
 * that needs LINE_CHANNEL_ACCESS_TOKEN and a target group/user id
 * configured in this Next.js app's own environment, which isn't there
 * yet (see this app's .env.example). The frontend receiving the plan
 * directly in this response is today's substitute.
 */
export async function POST(request: NextRequest) {
  // Without this, anyone who obtains a trip's poll link (forwarded into
  // a group chat, so not exactly secret) could call this route directly
  // - no UI needed - and repeatedly burn LLM quota. This only proves the
  // caller is SOME real LINE user, not that they're the trip's organizer
  // - see the isAdmin TODO in app/trip/poll/[id]/page.tsx for the still-
  // open next step.
  const lineUserId = await verifyBearerLineToken(request.headers.get("authorization"));
  if (!lineUserId) {
    return NextResponse.json(
      { error: "Missing, invalid, or expired LINE ID token." },
      { status: 401 }
    );
  }

  const rateLimit = checkRateLimit(`trigger:${lineUserId}`, TRIGGER_RATE_LIMIT, TRIGGER_RATE_WINDOW_MS);
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
    // Already locked - return the stored plan instead of spending LLM
    // quota again. A stale/corrupt stored value (should not happen; only
    // this route ever writes it) falls through to regenerating below
    // rather than erroring the whole request.
    if (await isPollLocked(tripId)) {
      const draft = await getDraft(tripId);
      const stored = draft ? parseStoredItinerary(draft.text) : null;
      if (stored) {
        return NextResponse.json({ tripId, locked: true, itinerary: stored });
      }
    }

    const votes = await getPollVotes(tripId);
    if (votes.length === 0) {
      return NextResponse.json(
        { error: "No one has voted on this poll yet - wait for votes before locking it." },
        { status: 400 }
      );
    }

    const summary = summarizePollVotes(votes);
    const itinerary = await generateItinerary(summary);

    await saveDraft(tripId, formatItineraryForStorage(itinerary));
    await lockPoll(tripId);

    return NextResponse.json({ tripId, locked: true, itinerary }, { status: 201 });
  } catch (error) {
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
