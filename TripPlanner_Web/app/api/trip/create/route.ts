import Anthropic from "@anthropic-ai/sdk";
import crypto from "crypto";
import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import { formatItineraryForStorage } from "@/lib/itinerary";
import { PlacesApiError } from "@/lib/places";
import { prisma } from "@/lib/prisma";
import { checkRateLimit } from "@/lib/rateLimit";
import { verifySignedRequest } from "@/lib/serverAuth";
import { createLockedTrip } from "@/lib/store";
import { loadTastePromptInput } from "@/lib/tasteForTrip";
import { tasteLabels } from "@/lib/tasteSummary";
import { resolveTripDays } from "@/lib/tripDates";
import { DestinationNotFoundError, generateItinerary } from "@/lib/tripGeneration";

// The same pipeline, and the same measured budget, as POST /api/trigger-jarvis
// (two Haiku calls dominated by the second, ~15-20s end to end for a real
// Chiang Mai trip), so the same ceiling.
export const maxDuration = 60;

const CREATE_PATH = "/api/trip/create";

// A signed request is already trusted to be Jarvis, but Jarvis acts for a chat
// that anyone signed in can type into, and every plan spends real Anthropic and
// Google quota - so the allowance per person is tight. Keyed by the person the
// plan is for, so one person's burst cannot lock out another's.
const CREATE_RATE_LIMIT = 5;
const CREATE_RATE_WINDOW_MS = 10 * 60_000;

// "Plan a trip to Chiang Mai" says nothing about length. Three days is a trip
// rather than a day out and keeps the generation well inside maxDuration; the
// caller (Jarvis) says so out loud when it used this, so the person can ask for
// a different one.
const DEFAULT_CREATED_TRIP_DAYS = 3;
const MAX_HEAD_COUNT = 20;

const CreateTripBodySchema = z.object({
  // The signed-in person the plan is for: their Taste Profile personalizes it and
  // their TripParticipant row records that it is theirs. Optional because the
  // voice assistant has no web account behind it - that plan is simply general.
  userId: z.string().trim().min(1).max(128).optional(),
  // Goes into an LLM prompt and a Places query, so: one line, no control or
  // formatting characters. It is the person's own text, not another user's.
  destination: z
    .string()
    .trim()
    .min(2)
    .max(80)
    .refine((value) => !/[\p{Cc}\p{Cf}]/u.test(value), "destination must be a single line of plain text"),
  // Anything numeric is accepted and re-clamped (resolveTripDays, below): a
  // slightly-off length should become the nearest sane one, not a 400.
  days: z.number().optional(),
  headCount: z.number().optional(),
});

/**
 * POST /api/trip/create { userId?, destination, days?, headCount? } - plans ONE
 * trip to a destination right now and stores it, already locked: Jarvis's
 * "plan a trip to Chiang Mai". Answers 201 { tripId, path, destination, days,
 * stops, personalized, taste } where `path` ("/trip/poll/<id>") is the page that
 * shows the finished plan - the caller prefixes its own origin to make the link.
 *
 * The group flow is untouched: a poll is still created by voice, voted on, and
 * locked by its organizer through POST /api/trigger-jarvis. This is the other
 * door, for a trip that has no poll - one person, one destination, no votes - so
 * where trigger-jarvis infers the destination from what the group wrote, this is
 * GIVEN it (lib/tripGeneration.ts's "fixed" input). Both build the plan with the
 * same pipeline.
 *
 * PERSONALIZED when `userId` has a finished Taste Profile: their TasteSwipe
 * answers go into the Stage 1 and Stage 2 prompts (lib/tasteSummary.ts), and the
 * response says so (`personalized`, and `taste` with the theme labels) so Jarvis
 * can tell them. No profile, or a lookup that fails, makes it a general plan -
 * never an error.
 *
 * NO DATES: nobody chose any, so the stored plan carries no startDate/endDate
 * (the plan view then shows TBD) and Routes estimates use the same near-future
 * anchor an undated poll does. Dates and an admin token (stop swapping) belong to
 * a poll; this trip has neither.
 *
 * AUTHORIZATION: server-to-server only. The caller signs the request with the
 * shared secret (lib/serverAuth.ts - X-Ts/X-Sig, covering the body, so a captured
 * request cannot be replayed with a different userId). There is no browser
 * session here: Jarvis's Python side calls this on behalf of the person who is
 * signed in at the web chat, and passes their id.
 *
 * Nothing is stored unless the whole plan was generated, and then in one
 * transaction (lib/store.ts's createLockedTrip).
 *
 * Responses: 201 created, 400 bad body, 401 bad/missing signature, 404 unknown
 * userId, 422 no real places for that destination, 429 rate-limited (ours or
 * Anthropic's), 502 Places/Anthropic failed or returned an empty plan, 503 no
 * API_SECRET_KEY configured, 500 anything else.
 */
export async function POST(request: NextRequest) {
  // The text, before any parsing: the signature covers these exact bytes.
  const rawBody = await request.text();

  const authFailure = verifySignedRequest(request.headers, "POST", CREATE_PATH, rawBody);
  if (authFailure) {
    return NextResponse.json({ error: authFailure.error }, { status: authFailure.status });
  }

  let json: unknown;
  try {
    json = JSON.parse(rawBody);
  } catch {
    return NextResponse.json({ error: "The body must be JSON." }, { status: 400 });
  }
  const parsed = CreateTripBodySchema.safeParse(json);
  if (!parsed.success) {
    return NextResponse.json(
      { error: "destination is required (2-80 characters, one line); userId, days and headCount are optional." },
      { status: 400 }
    );
  }

  const { userId } = parsed.data;
  const destination = parsed.data.destination.replace(/\s+/g, " ");
  const tripDays = parsed.data.days === undefined ? DEFAULT_CREATED_TRIP_DAYS : resolveTripDays(parsed.data.days);
  const headCount = Math.min(MAX_HEAD_COUNT, Math.max(1, Math.round(parsed.data.headCount ?? 1) || 1));

  const rateLimit = checkRateLimit(`create:${userId ?? "voice"}`, CREATE_RATE_LIMIT, CREATE_RATE_WINDOW_MS);
  if (!rateLimit.allowed) {
    return NextResponse.json(
      { error: "Too many trips planned in a short time - please wait a few minutes and try again." },
      { status: 429, headers: { "Retry-After": String(rateLimit.retryAfterSeconds) } }
    );
  }

  const tripId = crypto.randomUUID();

  try {
    if (userId) {
      const user = await prisma.user.findUnique({ where: { id: userId }, select: { id: true } });
      if (!user) {
        return NextResponse.json({ error: "Unknown user." }, { status: 404 });
      }
    }

    const taste = userId ? await loadTastePromptInput(async () => [userId], headCount, tripId) : null;
    if (taste) {
      console.log(
        `[trip-create] ${tripId}: taste profile - loves ${taste.taste.loved.map((t) => t.category).join(", ") || "-"}; ` +
          `passes on ${taste.taste.avoided.map((t) => t.category).join(", ") || "-"}`
      );
    }

    const itinerary = await generateItinerary(
      { kind: "fixed", destination, headCount, tripWindow: null, tripDays, taste },
      { tag: "trip-create", id: tripId }
    );

    const stops = itinerary.days.reduce((total, day) => total + day.stops.length, 0);
    if (stops === 0) {
      console.error(`POST ${CREATE_PATH}: the generator returned a plan with no stops for "${destination}"; nothing stored.`);
      return NextResponse.json(
        { error: "The plan generator returned an empty plan. Please try again." },
        { status: 502 }
      );
    }

    await createLockedTrip({
      tripId,
      itineraryJson: formatItineraryForStorage(itinerary),
      durationDays: tripDays,
      userId,
    });

    return NextResponse.json(
      {
        tripId,
        path: `/trip/poll/${tripId}`,
        destination: itinerary.destination,
        days: itinerary.days.length,
        stops,
        personalized: taste !== null,
        taste: taste ? tasteLabels(taste.taste) : null,
      },
      { status: 201 }
    );
  } catch (error) {
    if (error instanceof DestinationNotFoundError) {
      return NextResponse.json(
        {
          error: `No real places were found for "${destination}" - check the spelling or try a bigger nearby city.`,
          reason: "DESTINATION_NOT_FOUND",
        },
        { status: 422 }
      );
    }
    if (error instanceof PlacesApiError) {
      console.error(`POST ${CREATE_PATH}: Places API error (${error.reason}):`, error.message);
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
      console.error(`POST ${CREATE_PATH} failed (Anthropic ${error.status}):`, error.message);
      return NextResponse.json(
        { error: "The plan generator is temporarily unavailable. Please try again." },
        { status: 502 }
      );
    }
    console.error(`POST ${CREATE_PATH} failed for trip ${tripId}:`, error);
    return NextResponse.json(
      { error: "Something went wrong while planning the trip. Please try again." },
      { status: 500 }
    );
  }
}
