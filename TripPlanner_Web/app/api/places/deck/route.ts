import { NextResponse } from "next/server";
import { getSessionUserId } from "@/lib/auth";
import { DECK_CATEGORIES, SEARCH_PAGE_SIZE, assembleDeck, buildSeedQuery } from "@/lib/placeDeck";
import { PlacesApiError, searchPlacesForDeck } from "@/lib/places";
import { checkRateLimit } from "@/lib/rateLimit";

// Each deck is one billable Text Search call per seed in DECK_CATEGORIES, so
// this gets a tight, per-caller allowance like the other routes that spend
// Places quota (see app/api/trip/alternative/route.ts). Counted in decks, not
// calls: three decks of eight searches keeps the worst case per window close
// to what the old three-search deck allowed (six of them) - and is still far
// more than a person needs (a deck is built once per visit), far less than a
// runaway client loop.
const DECK_RATE_LIMIT = 3;
const DECK_RATE_WINDOW_MS = 5 * 60_000;

/**
 * GET /api/places/deck - the cards for the swipe step
 * (components/trip/SwipeDeck.tsx). The swipe deck is a one-time onboarding
 * quiz that builds a person's general taste profile ("do they like nature, or
 * shopping?"), so it is NOT scoped to a trip, a destination or one kind of
 * place: this runs the curated seed searches in DECK_CATEGORIES (a broad
 * theme in a major city each - historical landmarks in Rome, night markets in
 * Taipei, ...) and deals the results into one deck with an even number of
 * cards per theme - see lib/placeDeck.ts. It takes no parameters; a
 * `destination` query parameter from an older client is ignored.
 *
 * Signed-in callers only, and rate-limited per user: every call spends real
 * Google Places quota, and nothing here is cached - Google's policies forbid
 * storing Places content (see lib/places.ts), so each visit builds a fresh
 * deck. The seeds are fixed, so those decks come out the same each time (give
 * or take Google re-ranking), which is what lets a returning user pick up
 * where they left off.
 *
 * Responses: 200 { places } (places may be [] if nothing was found), 401
 * signed out, 429 rate-limited, 502 the Places API itself failed (with its
 * machine-readable `reason`, same as app/api/trip/alternative/route.ts), 500
 * anything else.
 */
export async function GET() {
  const userId = await getSessionUserId();
  if (!userId) {
    return NextResponse.json({ error: "Sign in to see place suggestions." }, { status: 401 });
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
    // should cost the deck one taste, not the whole thing. Only when EVERY
    // search fails - a systemic problem like a disabled API or a bad key - is
    // the first failure rethrown for the 502 below.
    const settled = await Promise.allSettled(
      DECK_CATEGORIES.map(async (spec) => ({
        spec,
        places: await searchPlacesForDeck(buildSeedQuery(spec), SEARCH_PAGE_SIZE),
      }))
    );

    const results = settled.flatMap((outcome) =>
      outcome.status === "fulfilled" ? [outcome.value] : []
    );
    // allSettled keeps its input's order, so a result's index is its seed's.
    const failures = settled.flatMap((outcome, index) =>
      outcome.status === "rejected"
        ? [{ query: buildSeedQuery(DECK_CATEGORIES[index]), reason: outcome.reason }]
        : []
    );

    if (results.length === 0) {
      throw failures[0].reason;
    }
    for (const failure of failures) {
      console.error(`GET /api/places/deck: the "${failure.query}" search failed:`, failure.reason);
    }

    return NextResponse.json(
      { places: assembleDeck(results) },
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
    console.error("GET /api/places/deck failed:", error);
    return NextResponse.json(
      { error: "Something went wrong while loading places. Please try again." },
      { status: 500 }
    );
  }
}
