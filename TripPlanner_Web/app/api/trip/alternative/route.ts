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
import { PlacesApiError, searchNearbyPlaces, type LatLng, type PlaceResult } from "@/lib/places";
import { checkRateLimit } from "@/lib/rateLimit";
import { computeDayRoute, computeDepartureTimeForDay } from "@/lib/routes";
import { getDraft, saveDraft } from "@/lib/store";

// A live, in-the-moment swap - never as costly to bound as "Lock &
// Generate Plan" (which runs the whole grounded pipeline from scratch),
// but it still spends real Places + Anthropic quota per call, so it gets
// the same tight allowance as that route rather than the loose one
// ordinary voting gets.
const ALTERNATIVE_RATE_LIMIT = 5;
const ALTERNATIVE_RATE_WINDOW_MS = 5 * 60_000;

const NEARBY_SEARCH_RADIUS_METERS = 5000;
const MAX_CANDIDATES = 3;

const AlternativeChoiceSchema = z.object({
  // Index into the candidate list this route sent, 0-based - see
  // buildAlternativePrompt. Never trusted blindly (same as Stage 2's
  // placeIndex in app/api/trigger-jarvis/route.ts): an out-of-range value
  // falls back to the closest candidate (index 0) rather than throwing.
  chosenIndex: z.number().int(),
  text: z.string(),
  estimatedCostPerPerson: z.number().nullable(),
});

function buildAlternativePrompt(
  placeType: string,
  availableMinutes: number,
  originalStopText: string,
  candidates: PlaceResult[]
): string {
  const candidatesText = candidates
    .map((p, i) => {
      const ratingText = p.rating !== null ? `, rating ${p.rating}` : "";
      const priceText = p.priceLevel
        ? `, price level ${p.priceLevel.replace("PRICE_LEVEL_", "").toLowerCase()}`
        : "";
      return `${i}=${p.name} (${p.address}${ratingText}${priceText})`;
    })
    .join("; ");

  return (
    "The traveler's original plan had this stop, which is no longer workable right now (closed, " +
    `plans changed, ...): "${originalStopText}". They need a REAL, NEARBY replacement of type ` +
    `"${placeType}", and have about ${availableMinutes} minutes available for it.\n\n` +
    `Real nearby candidates found via Google Places, closest first: ${candidatesText}\n\n` +
    "Pick the single best candidate for someone with that much time available, favoring a quicker " +
    "option if time is short. Only choose from the candidates listed - never invent a name not " +
    "present in this data. Return chosenIndex (the candidate number your pick corresponds to), text " +
    "(one short sentence describing this replacement stop, incorporating the venue's real name, in " +
    "the same style as an itinerary stop), and estimatedCostPerPerson (a realistic cost per person, " +
    "using the price level shown as a guide, or null if you can't reasonably estimate one)."
  );
}

async function chooseAlternative(
  placeType: string,
  availableMinutes: number,
  originalStopText: string,
  candidates: PlaceResult[]
) {
  const apiKey = process.env.ANTHROPIC_API_KEY;
  if (!apiKey) {
    throw new Error("ANTHROPIC_API_KEY is not configured.");
  }

  const client = new Anthropic({ apiKey });
  const response = await client.messages.parse({
    model: FINALIZE_MODEL,
    max_tokens: FINALIZE_MAX_TOKENS,
    output_config: { format: zodOutputFormat(AlternativeChoiceSchema) },
    messages: [
      { role: "user", content: buildAlternativePrompt(placeType, availableMinutes, originalStopText, candidates) },
    ],
  });

  if (!response.parsed_output) {
    throw new Error("Claude did not return a valid alternative choice.");
  }
  return response.parsed_output;
}

/**
 * Recomputes travelFromPrevious for `stop`, given its new/updated
 * location and the location of the stop immediately before it in the
 * same day (or null for a day's first stop). Reuses lib/routes.ts's
 * computeDayRoute with exactly 2 points rather than a separate
 * single-leg helper - a 2-stop call already IS one leg.
 */
async function recomputeLegInto(
  fromLocation: LatLng | null,
  toLocation: LatLng | null,
  departureTime: string
): Promise<ItineraryStop["travelFromPrevious"]> {
  if (!fromLocation || !toLocation) return null;
  const legs = await computeDayRoute([fromLocation, toLocation], departureTime);
  return legs?.[0] ?? null;
}

/**
 * POST /api/trip/alternative - real-time fallback routing for one stop
 * on an already-locked trip: "this cafe is closed, what else is nearby
 * right now?" Given the traveler's current lat/lng, how much time they
 * have, and a Google place type, this finds 3 real nearby candidates
 * (Places API (New) Nearby Search - see lib/places.ts's
 * searchNearbyPlaces), asks Claude to pick the best fit and estimate its
 * cost, then patches that one stop into the trip's stored itinerary and
 * recomputes the travel legs on either side of it (see
 * recomputeLegInto) - never regenerating the rest of the plan.
 *
 * AUTHORIZATION: gated the same way as POST /api/trigger-jarvis - an
 * admin_token is required in the request body and verified against
 * Poll.adminTokenHash (see lib/adminToken.ts's verifyAdminToken) before
 * anything else runs. Deliberately reuses that exact mechanism rather
 * than inventing a second one: the trip creator already has this token
 * (it's baked into their .../trip/poll/<id>?admin=<token> link - see
 * verifyAdminToken's docstring for how it's minted and distributed), and
 * only they should be able to rewrite a locked plan's stops. The
 * frontend hiding the "Find Alternative" control without this param
 * (components/trip/TimelineList.tsx, gated the same way
 * app/trip/poll/[id]/page.tsx already gates "Lock & Generate Plan") is
 * UX only; this check is the actual security boundary. Also still
 * rate-limited per trip (see ALTERNATIVE_RATE_LIMIT above), since even
 * an authorized caller spends real Places + Anthropic quota per call.
 *
 * CONCURRENCY: this reads the whole stored itinerary, patches one stop,
 * and writes the whole thing back - no per-trip lock like
 * claimPollForGeneration's (that one exists specifically because
 * trigger-jarvis's generation is a one-time, expensive claim; this is a
 * small, low-frequency edit). Two overlapping requests for the SAME trip
 * (different stops, or a genuine double-tap) can last-write-wins clobber
 * each other - the same tolerance app/api/draft/[id]/route.ts's solo
 * draft board already accepts for its own shared text blob. Worth
 * revisiting with real per-trip locking if this proves to be a problem
 * in practice.
 */
export async function POST(request: NextRequest) {
  const body = await request.json().catch(() => null);

  const tripId = typeof body?.trip_id === "string" ? body.trip_id.trim() : "";
  const adminToken = typeof body?.admin_token === "string" ? body.admin_token.trim() : "";
  const dayNumber = typeof body?.dayNumber === "number" ? body.dayNumber : NaN;
  const stopIndex = typeof body?.stopIndex === "number" ? body.stopIndex : NaN;
  const lat = typeof body?.lat === "number" ? body.lat : NaN;
  const lng = typeof body?.lng === "number" ? body.lng : NaN;
  const availableMinutes = typeof body?.availableMinutes === "number" ? body.availableMinutes : NaN;
  const placeType = typeof body?.placeType === "string" ? body.placeType.trim().slice(0, 50) : "";

  if (!tripId) {
    return NextResponse.json({ error: "trip_id is required." }, { status: 400 });
  }

  if (!(await verifyAdminToken(tripId, adminToken))) {
    return NextResponse.json(
      { error: "A valid admin token is required to find an alternative for this trip." },
      { status: 403 }
    );
  }

  if (!Number.isInteger(dayNumber) || dayNumber < 1) {
    return NextResponse.json({ error: "dayNumber must be a positive integer." }, { status: 400 });
  }
  if (!Number.isInteger(stopIndex) || stopIndex < 0) {
    return NextResponse.json({ error: "stopIndex must be a non-negative integer." }, { status: 400 });
  }
  if (!Number.isFinite(lat) || lat < -90 || lat > 90 || !Number.isFinite(lng) || lng < -180 || lng > 180) {
    return NextResponse.json({ error: "lat/lng must be valid coordinates." }, { status: 400 });
  }
  if (!Number.isFinite(availableMinutes) || availableMinutes <= 0) {
    return NextResponse.json({ error: "availableMinutes must be a positive number." }, { status: 400 });
  }
  if (!placeType) {
    return NextResponse.json({ error: "placeType is required." }, { status: 400 });
  }

  const rateLimit = checkRateLimit(`alternative:${tripId}`, ALTERNATIVE_RATE_LIMIT, ALTERNATIVE_RATE_WINDOW_MS);
  if (!rateLimit.allowed) {
    return NextResponse.json(
      { error: "Too many alternative requests for this trip - please wait a few minutes and try again." },
      { status: 429, headers: { "Retry-After": String(rateLimit.retryAfterSeconds) } }
    );
  }

  try {
    const draft = await getDraft(tripId);
    const itinerary: Itinerary | null = draft ? parseStoredItinerary(draft.text) : null;
    if (!itinerary) {
      return NextResponse.json(
        { error: "This trip has no locked itinerary yet - lock a plan before requesting an alternative." },
        { status: 404 }
      );
    }

    const dayIndex = itinerary.days.findIndex((d) => d.day === dayNumber);
    if (dayIndex === -1 || stopIndex >= itinerary.days[dayIndex].stops.length) {
      return NextResponse.json({ error: "No such day/stop in this trip's itinerary." }, { status: 404 });
    }

    const day = itinerary.days[dayIndex];
    const originalStop = day.stops[stopIndex];

    const candidates = await searchNearbyPlaces(
      { lat, lng },
      NEARBY_SEARCH_RADIUS_METERS,
      placeType,
      MAX_CANDIDATES
    );
    if (candidates.length === 0) {
      return NextResponse.json(
        { error: `No ${placeType} alternatives found within ${NEARBY_SEARCH_RADIUS_METERS / 1000}km.` },
        { status: 404 }
      );
    }

    const choice = await chooseAlternative(placeType, availableMinutes, originalStop.text, candidates);
    const safeIndex = choice.chosenIndex >= 0 && choice.chosenIndex < candidates.length ? choice.chosenIndex : 0;
    const chosen = candidates[safeIndex];

    const departureTime = computeDepartureTimeForDay(itinerary.startDate ?? null, dayNumber);
    const previousStop = stopIndex > 0 ? day.stops[stopIndex - 1] : null;
    const nextStop = stopIndex + 1 < day.stops.length ? day.stops[stopIndex + 1] : null;

    const newStop: ItineraryStop = {
      slotType: originalStop.slotType,
      text: choice.text,
      location: chosen.location,
      estimatedCostPerPerson: choice.estimatedCostPerPerson,
      travelFromPrevious: await recomputeLegInto(
        previousStop?.location ?? null,
        chosen.location,
        departureTime
      ),
    };

    day.stops[stopIndex] = newStop;
    if (nextStop) {
      day.stops[stopIndex + 1] = {
        ...nextStop,
        travelFromPrevious: await recomputeLegInto(chosen.location, nextStop.location ?? null, departureTime),
      };
    }

    itinerary.totalTripEstimatedCost = computeTotalEstimatedCost(itinerary);

    await saveDraft(tripId, formatItineraryForStorage(itinerary));

    return NextResponse.json({ tripId, dayNumber, stopIndex, stop: newStop, itinerary }, { status: 200 });
  } catch (error) {
    if (error instanceof PlacesApiError) {
      console.error(`POST /api/trip/alternative: Places API error (${error.reason}):`, error.message);
      return NextResponse.json(
        { error: `Nearby search is unavailable: ${error.message}`, reason: error.reason },
        { status: 502 }
      );
    }
    if (error instanceof Anthropic.RateLimitError) {
      return NextResponse.json(
        { error: "The alternative picker is temporarily rate-limited - please try again shortly." },
        { status: 429 }
      );
    }
    if (error instanceof Anthropic.APIError) {
      console.error(`POST /api/trip/alternative failed (Anthropic ${error.status}):`, error.message);
      return NextResponse.json(
        { error: "The alternative picker is temporarily unavailable. Please try again." },
        { status: 502 }
      );
    }
    console.error(`POST /api/trip/alternative failed for trip ${tripId}:`, error);
    return NextResponse.json(
      { error: "Something went wrong while finding an alternative. Please try again." },
      { status: 500 }
    );
  }
}
