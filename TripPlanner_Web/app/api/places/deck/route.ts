import { NextRequest, NextResponse } from "next/server";
import { getSessionUserId } from "@/lib/auth";
import { DECK_CATEGORIES, SEARCH_PAGE_SIZE, assembleDeck } from "@/lib/placeDeck";
import { PlacesApiError, searchPlacesForDeck } from "@/lib/places";
import { checkRateLimit } from "@/lib/rateLimit";

// Each deck is three billable Text Search calls, so this gets the same
// tight, per-caller allowance as the other routes that spend Places quota
// (see app/api/trip/alternative/route.ts) - far more than a person needs
// (a deck is built once per visit), far less than a runaway client loop.
const DECK_RATE_LIMIT = 6;
const DECK_RATE_WINDOW_MS = 5 * 60_000;

const MAX_DESTINATION_LENGTH = 100;

/**
 * GET /api/places/deck?destination=Siam%20Paragon - the cards for the
 * swipe step (components/trip/SwipeDeck.tsx): up to 20 places around
 * `destination`, drawn from three searches (attractions, restaurants,
 * cafes) and dealt so neighbouring cards differ - see lib/placeDeck.ts.
 *
 * Signed-in callers only, and rate-limited per user: every call spends
 * real Google Places quota, and nothing here is cached - Google's policies
 * forbid storing Places content (see lib/places.ts), so each visit builds
 * a fresh deck. `destination` is free text ("Siam Paragon", "Nimman,
 * Chiang Mai") interpolated into a search query, not an id.
 *
 * Responses: 200 { destination, places } (places may be [] if nothing was
 * found), 400 bad destination, 401 signed out, 429 rate-limited, 502 the
 * Places API itself failed (with its machine-readable `reason`, same as
 * app/api/trip/alternative/route.ts), 500 anything else.
 */
export async function GET(request: NextRequest) {
  const userId = await getSessionUserId();
  if (!userId) {
    return NextResponse.json({ error: "Sign in to see place suggestions." }, { status: 401 });
  }

  const destination = (request.nextUrl.searchParams.get("destination") ?? "")
    .replace(/\s+/g, " ")
    .trim();
  if (!destination) {
    return NextResponse.json({ error: "destination is required." }, { status: 400 });
  }
  if (destination.length > MAX_DESTINATION_LENGTH) {
    return NextResponse.json(
      { error: `destination must be at most ${MAX_DESTINATION_LENGTH} characters.` },
      { status: 400 }
    );
  }

  const rateLimit = checkRateLimit(`deck:${userId}`, DECK_RATE_LIMIT, DECK_RATE_WINDOW_MS);
  if (!rateLimit.allowed) {
    return NextResponse.json(
      { error: "Too many place requests - please wait a moment and try again." },
      { status: 429, headers: { "Retry-After": String(rateLimit.retryAfterSeconds) } }
    );
  }

  try {
    // allSettled, not all: one failed search (a transient Google error)
    // should cost the deck some cafes, not the whole thing. Only when
    // EVERY search fails - a systemic problem like a disabled API or a bad
    // key - is the first failure rethrown for the 502 below.
    const settled = await Promise.allSettled(
      DECK_CATEGORIES.map(async (spec) => ({
        spec,
        places: await searchPlacesForDeck(
          spec.buildQuery(destination),
          spec.includedType,
          SEARCH_PAGE_SIZE
        ),
      }))
    );

    const results = settled.flatMap((outcome) =>
      outcome.status === "fulfilled" ? [outcome.value] : []
    );
    const failures = settled.flatMap((outcome) =>
      outcome.status === "rejected" ? [outcome.reason] : []
    );

    if (results.length === 0) {
      throw failures[0];
    }
    for (const failure of failures) {
      console.error(`GET /api/places/deck: one search failed for "${destination}":`, failure);
    }

    return NextResponse.json(
      { destination, places: assembleDeck(results) },
      { headers: { "Cache-Control": "private, no-store" } }
    );
  } catch (error) {
    if (error instanceof PlacesApiError) {
      console.error(`GET /api/places/deck: Places API error (${error.reason}):`, error.message);
      return NextResponse.json(
        { error: "Place suggestions are unavailable right now. Please try again.", reason: error.reason },
        { status: 502 }
      );
    }
    console.error(`GET /api/places/deck failed for "${destination}":`, error);
    return NextResponse.json(
      { error: "Something went wrong while loading places. Please try again." },
      { status: 500 }
    );
  }
}
