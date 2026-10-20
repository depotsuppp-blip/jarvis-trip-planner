/**
 * Turns saved Taste Profile answers (the TasteSwipe table - see
 * prisma/schema.prisma and POST /api/taste-profile) into the short block of
 * text the trip-generation prompts carry, so a plan is built around what the
 * travelers said they love instead of ignoring the quiz they took.
 *
 * Pure: no database, no Google, no Anthropic. lib/store.ts reads the rows
 * (getTasteSwipesForUsers), lib/tripGeneration.ts puts the blocks into the
 * Stage 1 (skeleton) and Stage 2 (final write) prompts, and the swipe quiz's
 * finish card (components/trip/SwipeDeck.tsx) uses the same summary for its
 * "Top vibes", so what a person is shown is what the planner is told.
 *
 * POSITIVE SIGNALS ONLY. The quiz deals the same worldwide deck to everyone:
 * CARDS_PER_CATEGORY cards for each of the DECK_CATEGORIES themes (history,
 * shopping, nature, ...), each answered LIKE or DISLIKE. A like says "this
 * photo made me want to go". A pass says almost nothing: the three sample
 * photos of a theme can be crowded, dated or just not that photo's best angle,
 * so 0 likes out of 3 does NOT mean the person dislikes the theme - and a plan
 * that quietly drops it ("they pass on food") is worse than one that keeps it.
 * So only likes are read: the themes someone liked most are what the plan
 * leans towards, and every other theme is neutral - never mentioned, never
 * avoided.
 *
 * A group is the tallies added together, which is why the threshold below is a
 * RATIO (liked over answered) rather than a count: it reads the same for one
 * traveler with three cards per theme and for six travelers with eighteen.
 */

import { DECK_CATEGORIES, isDeckCategory } from "./placeDeck";
import { DECK_CATEGORY_LABELS, type DeckPlaceCategory } from "./tripTypes";

/** One stored quiz answer - the fields of a TasteSwipe row this module reads. */
export interface TasteRow {
  userId: string;
  category: string;
  action: string;
}

export interface TasteTheme {
  category: DeckPlaceCategory;
  /** What the quiz calls it on screen ("Night markets"). */
  label: string;
  /** How many of this theme's cards were liked. */
  likes: number;
  /** How many of them were answered at all (liked or passed). */
  answers: number;
  /** likes / answers, from 0 (none liked) to 1 (all liked). */
  score: number;
}

export interface TasteSummary {
  /** How many people's answers are in this summary. */
  people: number;
  /** The themes they liked most, strongest first. Every other theme is neutral. */
  loved: TasteTheme[];
}

/**
 * How a theme is put to the model. A Record over the union, so adding a taste
 * to DeckPlaceCategory is a compile error until it has a phrase here - the same
 * rule DECK_CATEGORY_LABELS follows. Worded as the kind of PLACE (what Stage 1
 * turns into search categories) rather than the quiz's one-word label.
 */
const THEME_PHRASES: Record<DeckPlaceCategory, string> = {
  history: "historical landmarks and old towns",
  shopping: "shopping malls and boutiques",
  nature: "quiet nature spots and the outdoors",
  markets: "night markets and street-food markets",
  art: "art museums and galleries",
  food: "iconic local restaurants and signature dishes",
  cafes: "trendy cafes",
  nightlife: "lively bars and nightlife",
};

// A theme counts as liked when at least this share of its answers were likes:
// one like out of three is exactly 1/3. Nothing is read from the passes.
const LIKE_THRESHOLD = 1 / 3;

// The prompt names a few themes, not the whole deck: a profile that lists
// everything steers nothing.
const MAX_LOVED = 3;

// More themes than this liked means the person said yes to nearly everything,
// which tells the planner nothing about what stands out - and the order they
// are listed in would silently decide the plan.
const MAX_DISCRIMINATING = 5;

function strongest(themes: TasteTheme[]): TasteTheme[] {
  const liked = themes.filter((theme) => theme.score >= LIKE_THRESHOLD);
  if (liked.length > MAX_DISCRIMINATING) return [];
  // Best liked share first, then the most likes; `themes` is in deck order and
  // sort is stable, so ties settle the same way every time for the same answers.
  return liked.sort((a, b) => b.score - a.score || b.likes - a.likes).slice(0, MAX_LOVED);
}

/**
 * The summary for these quiz answers, or null when they say nothing usable: no
 * valid rows, nothing liked, or so much liked that no theme stands out. Null is
 * the "personalize nothing" answer - callers then leave the prompts exactly as
 * they were before taste existed.
 *
 * Rows with a category the deck does not deal, or an action that is not LIKE or
 * DISLIKE, are ignored rather than trusted (the API validates both on the way
 * in, but this reads what is in the table).
 */
export function summarizeTaste(rows: TasteRow[]): TasteSummary | null {
  const tallies = new Map<DeckPlaceCategory, { likes: number; answers: number }>();
  const people = new Set<string>();

  for (const row of rows) {
    if (!isDeckCategory(row.category)) continue;
    if (row.action !== "LIKE" && row.action !== "DISLIKE") continue;
    const tally = tallies.get(row.category) ?? { likes: 0, answers: 0 };
    tally.answers += 1;
    if (row.action === "LIKE") tally.likes += 1;
    tallies.set(row.category, tally);
    people.add(row.userId);
  }

  // Deck order, so every later tie-break is stable.
  const themes: TasteTheme[] = [];
  for (const spec of DECK_CATEGORIES) {
    const tally = tallies.get(spec.category);
    if (!tally) continue;
    themes.push({
      category: spec.category,
      label: DECK_CATEGORY_LABELS[spec.category],
      likes: tally.likes,
      answers: tally.answers,
      score: tally.likes / tally.answers,
    });
  }

  const loved = strongest(themes);
  if (loved.length === 0) return null;
  return { people: people.size, loved };
}

function describe(theme: TasteTheme): string {
  return `${THEME_PHRASES[theme.category]} (${theme.likes} of ${theme.answers} answers liked)`;
}

/** "a, b and c" - the list shape the prompt sentences use. */
function joinList(items: string[]): string {
  if (items.length <= 1) return items.join("");
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

/**
 * Who the profile belongs to, as the prompt says it: "the traveler" for one
 * person's answers, and for a group how many of its travelers have one - the
 * rest took no quiz, and the model should not read the profile as the whole
 * group's. `travelers` is the group's size (a poll's voters); omit it for solo.
 */
function whose(taste: TasteSummary, travelers: number | undefined): string {
  if (travelers === undefined || travelers <= 1) return "the traveler";
  return taste.people >= travelers
    ? `the group (all ${travelers} travelers)`
    : `the group (${taste.people} of its ${travelers} travelers took the quiz)`;
}

/**
 * The paragraph for the Stage 1 (skeleton) prompt: what to build the days
 * around. Stage 1 decides the KIND of place for each slot, so this is where a
 * profile does most of its work - a theme nobody asked for never reaches the
 * candidate list Stage 2 chooses from. It says what they liked and that the
 * rest is open, never what to leave out.
 */
export function tasteSkeletonBlock(taste: TasteSummary, travelers?: number): string {
  return [
    `Taste profile of ${whose(taste, travelers)}, from the places they liked in a swipe quiz about kinds of places:`,
    `- Likes: ${joinList(taste.loved.map(describe))}.`,
    "Personalize the structure with this. Make what they like the backbone of the days: give each " +
      "liked theme at least one slot where the destination offers it, the strongest theme most often, " +
      "and still mix in other kinds of places so no day repeats itself. Kinds of place that are not " +
      "listed are neutral, not disliked - the quiz only showed a few sample photos of each - so keep " +
      "using them wherever the destination does them well.",
  ].join("\n");
}

/**
 * The paragraph for the Stage 2 (final write) prompt: how to choose between
 * the real candidates Google returned for a slot. Stage 2 can only pick from
 * what Stage 1's categories found, so this is the tie-breaker, and it says
 * outright that the profile is not to be quoted - a caption saying "because you
 * like cafes" is noise on a 15-word line.
 */
export function tasteFinalBlock(taste: TasteSummary, travelers?: number): string {
  return (
    `Taste profile of ${whose(taste, travelers)} (never mention it in any caption): likes ` +
    `${joinList(taste.loved.map((t) => THEME_PHRASES[t.category]))}. ` +
    "When a Stop lists more than one candidate, choose the one that best fits what they like; " +
    "candidates of any other kind are fine too. A Stop marked USER-REQUESTED is always kept as asked."
  );
}

/** The labels a caller may show a person ("Nature", "Cafes"), strongest first. */
export function tasteLabels(taste: TasteSummary): { loved: string[] } {
  return { loved: taste.loved.map((theme) => theme.label) };
}
