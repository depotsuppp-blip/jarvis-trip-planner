import type { DeckSearchPlace } from "@/lib/places";
import type { DeckPlace, DeckPlaceCategory } from "@/lib/tripTypes";

/**
 * Pure deck-building logic for GET /api/places/deck - what to search for,
 * and how to turn each search's raw results into one varied deck of cards.
 * No Google calls happen here (the route does those via
 * lib/places.ts's searchPlacesForDeck), so this stays trivially testable.
 *
 * The deck is a one-time taste questionnaire, not a set of suggestions for
 * any trip: what we want to learn is whether someone likes nature, or
 * shopping, or night markets IN GENERAL. So every deck is drawn from the
 * same curated seeds - one broad theme in one major city each, spread over
 * the world - and never from a trip's destination. A seed's cards are all
 * the same kind of place, which is what lets a like or a pass count as
 * evidence for (or against) the whole theme.
 */

/**
 * Cards each seed contributes. Three per theme is enough that one odd photo
 * (or one place someone happens to know) doesn't settle whether they like
 * the theme, while every theme together still takes a minute or two. The
 * deck is DECK_CATEGORIES.length x this - fewer only when a search comes up
 * short.
 */
export const CARDS_PER_CATEGORY = 3;

/**
 * How many results each seed search asks Google for. Text Search bills per
 * request, not per result (see searchPlacesForDeck), so this only trades
 * response size for surplus: CARDS_PER_CATEGORY are wanted, and the rest is
 * what's left to deal from once closed places and duplicates are dropped.
 */
export const SEARCH_PAGE_SIZE = 6;

export interface DeckCategorySpec {
  category: DeckPlaceCategory;
  /**
   * The broad theme searched for, worded the way a traveler would say it -
   * the adjective ("quiet", "bustling", "high-end") steers Google toward the
   * character of the place, not just its type.
   */
  theme: string;
  /**
   * The major city the theme is searched in. Different for every category,
   * so the deck spans the world rather than sampling one place.
   */
  city: string;
  /** Card label when Google doesn't supply a more specific primary type. */
  fallbackLabel: string;
}

/**
 * The seed list, curated by hand: each entry is one billable Text Search,
 * and its results are what the cards for that category are dealt from. The
 * first five themes are the ones the product asked for; food, cafes and
 * nightlife round out the picture and line up with the poll's own vibes
 * (Cafe Hopping, Nightlife, BBQ/Grill).
 *
 * To change the taste questions, edit this list: the deck, its size and the
 * number of searches per deck all follow from it. Try a new theme/city pair
 * against live Text Search first, the way these were - the top results
 * should be on-topic, open, and have photos.
 *
 * The deck is dealt round-robin in this order, so no two neighbouring cards
 * measure the same taste. It opens with landmarks on purpose: they
 * photograph best, and the first card someone sees sets their expectations
 * for the rest.
 */
export const DECK_CATEGORIES: DeckCategorySpec[] = [
  { category: "history", theme: "historical landmarks", city: "Rome", fallbackLabel: "Landmark" },
  { category: "shopping", theme: "high-end shopping malls", city: "Dubai", fallbackLabel: "Shopping mall" },
  { category: "nature", theme: "quiet nature spots", city: "Vancouver", fallbackLabel: "Nature spot" },
  { category: "markets", theme: "bustling night markets", city: "Taipei", fallbackLabel: "Market" },
  { category: "art", theme: "modern art museums", city: "New York", fallbackLabel: "Art museum" },
  { category: "food", theme: "iconic local restaurants", city: "Tokyo", fallbackLabel: "Restaurant" },
  { category: "cafes", theme: "trendy cafes", city: "Melbourne", fallbackLabel: "Cafe" },
  { category: "nightlife", theme: "lively bars and nightlife", city: "Barcelona", fallbackLabel: "Bar" },
];

/** The Text Search query for one seed, e.g. "quiet nature spots in Vancouver". */
export function buildSeedQuery(spec: DeckCategorySpec): string {
  return `${spec.theme} in ${spec.city}`;
}

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
    city: spec.city,
    categoryLabel: place.typeLabel ?? spec.fallbackLabel,
    rating: place.rating,
    ratingCount: place.ratingCount,
    address: place.address,
    photoUrl: place.photoName ? photoProxyUrl(place.photoName) : null,
    photoAttribution: place.photoName ? place.photoAttribution : null,
  };
}

/**
 * Builds the final deck from each seed's raw results (given in
 * DECK_CATEGORIES order):
 *
 * - Places Google reports as closed are dropped - nobody should be asked
 *   whether they'd like to visit somewhere that isn't there.
 * - Within a seed, places with a photo come first (otherwise keeping
 *   Google's own relevance order), so photo-less ones only reach the deck
 *   if a category has run out of photographed places.
 * - Each category gets at most `cardsPerCategory` cards, dealt round-robin
 *   (one per category per round) and de-duplicated by place id - the same
 *   place can come up under two themes (a mall in a night-market search),
 *   and it is dealt once, to whichever category reaches it first.
 *   Deliberately NOT shuffled: the seeds are fixed, so every visit deals the
 *   same deck, which is what lets a refresh resume where someone left off
 *   (the client skips ids already answered) and keeps everyone's answers
 *   comparable.
 *
 * A search that returned little (or nothing) just contributes fewer cards,
 * and the other categories are NOT topped up to make up the number - a
 * short deck with an even spread measures taste better than a full one that
 * over-asks about some themes. The deck can only come up short when a search
 * fails or a city genuinely has too few places.
 */
export function assembleDeck(
  results: { spec: DeckCategorySpec; places: DeckSearchPlace[] }[],
  cardsPerCategory: number = CARDS_PER_CATEGORY
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

  for (let round = 0; round < cardsPerCategory; round++) {
    for (const queue of queues) {
      let next = queue.remaining.shift();
      while (next && seen.has(next.id)) {
        next = queue.remaining.shift();
      }
      if (!next) continue;

      seen.add(next.id);
      deck.push(toDeckPlace(next, queue.spec));
    }
  }

  return deck;
}
