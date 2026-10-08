import type { DeckSearchPlace } from "@/lib/places";
import type { DeckPlace, DeckPlaceCategory } from "@/lib/tripTypes";

/**
 * Pure deck-building logic for GET /api/places/deck - what to search for,
 * and how to turn three raw result lists into one varied deck of cards.
 * No Google calls happen here (the route does those via
 * lib/places.ts's searchPlacesForDeck), so this stays trivially testable.
 */

/**
 * Cards per deck: small enough to finish in one sitting, big enough to
 * say something real about a person's taste.
 */
export const DECK_SIZE = 20;

/**
 * How many results each of the three searches asks Google for. Text
 * Search bills per request, not per result (see searchPlacesForDeck), so
 * this is deliberately a near-full page rather than DECK_SIZE / 3 - the
 * surplus is what's left to deal from after closed and photo-less places
 * are filtered out.
 */
export const SEARCH_PAGE_SIZE = 15;

export interface DeckCategorySpec {
  category: DeckPlaceCategory;
  /** A Google Place Types (New) value - a bias, not a filter (see searchPlacesForDeck). */
  includedType: string;
  /** Card label when Google doesn't supply a more specific primary type. */
  fallbackLabel: string;
  buildQuery: (destination: string) => string;
}

/**
 * The deck is dealt round-robin in this order, so no two neighbouring
 * cards come from the same search. It opens with an attraction on
 * purpose: landmarks photograph best, and the first card someone sees
 * sets their expectations for the rest.
 */
export const DECK_CATEGORIES: DeckCategorySpec[] = [
  {
    category: "attraction",
    includedType: "tourist_attraction",
    fallbackLabel: "Attraction",
    buildQuery: (destination) => `top tourist attractions near ${destination}`,
  },
  {
    category: "restaurant",
    includedType: "restaurant",
    fallbackLabel: "Restaurant",
    buildQuery: (destination) => `best restaurants near ${destination}`,
  },
  {
    category: "cafe",
    includedType: "cafe",
    fallbackLabel: "Cafe",
    buildQuery: (destination) => `popular cafes near ${destination}`,
  },
];

// A card's photo area is at most ~420 CSS px wide; 800 keeps it sharp on a
// 2x phone screen without paying for (or downloading) anything larger.
const PHOTO_WIDTH_PX = 800;

/**
 * Same-origin URL the browser loads a card's photo from - see
 * app/api/places/photo/route.ts, which resolves it to a Google image
 * server-side so the Places API key never appears in a URL the client
 * sees.
 */
export function photoProxyUrl(photoName: string): string {
  return `/api/places/photo?${new URLSearchParams({ name: photoName, w: String(PHOTO_WIDTH_PX) })}`;
}

function isOpen(place: DeckSearchPlace): boolean {
  return place.businessStatus !== "CLOSED_PERMANENTLY" && place.businessStatus !== "CLOSED_TEMPORARILY";
}

function toDeckPlace(place: DeckSearchPlace, spec: DeckCategorySpec): DeckPlace {
  return {
    id: place.id,
    name: place.name,
    category: spec.category,
    categoryLabel: place.typeLabel ?? spec.fallbackLabel,
    rating: place.rating,
    ratingCount: place.ratingCount,
    address: place.address,
    photoUrl: place.photoName ? photoProxyUrl(place.photoName) : null,
    photoAttribution: place.photoName ? place.photoAttribution : null,
  };
}

/**
 * Builds the final deck from each search's raw results (given in
 * DECK_CATEGORIES order):
 *
 * - Places Google reports as closed are dropped - nobody should be asked
 *   whether they'd like to visit somewhere that isn't there.
 * - Within a search, places with a photo come first (otherwise keeping
 *   Google's own relevance order), so photo-less ones only reach the deck
 *   if a category has run out of photographed places.
 * - The three lists are dealt round-robin and de-duplicated by place id
 *   (the same cafe can also surface as an attraction), up to `size`.
 *   Deliberately NOT shuffled: the same destination should produce the
 *   same deck order for everyone in a group, so their swipes line up.
 *
 * A search that returned little (or nothing) just contributes fewer
 * cards; the others fill the remainder, so the deck can come up short of
 * `size` only when the area genuinely has too few places.
 */
export function assembleDeck(
  results: { spec: DeckCategorySpec; places: DeckSearchPlace[] }[],
  size: number = DECK_SIZE
): DeckPlace[] {
  const queues = results.map(({ spec, places }) => {
    const open = places.filter(isOpen);
    return {
      spec,
      remaining: [...open.filter((p) => p.photoName), ...open.filter((p) => !p.photoName)],
    };
  });

  const seen = new Set<string>();
  const deck: DeckPlace[] = [];

  while (deck.length < size) {
    let dealtThisRound = false;

    for (const queue of queues) {
      if (deck.length >= size) break;

      let next = queue.remaining.shift();
      while (next && seen.has(next.id)) {
        next = queue.remaining.shift();
      }
      if (!next) continue;

      seen.add(next.id);
      deck.push(toDeckPlace(next, queue.spec));
      dealtThisRound = true;
    }

    if (!dealtThisRound) break;
  }

  return deck;
}
