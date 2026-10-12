/**
 * Turns saved Taste Profile answers (the TasteSwipe table - see
 * prisma/schema.prisma and POST /api/taste-profile) into the short block of
 * text the trip-generation prompts carry, so a plan is built around what the
 * travelers said they love instead of ignoring the quiz they took.
 *
 * Pure: no database, no Google, no Anthropic. lib/store.ts reads the rows
 * (getTasteSwipesForUsers) and lib/tripGeneration.ts puts the blocks into the
 * Stage 1 (skeleton) and Stage 2 (final write) prompts.
 *
 * What a profile can say. The quiz deals the same worldwide deck to everyone:
 * CARDS_PER_CATEGORY cards for each of the DECK_CATEGORIES themes (history,
 * shopping, nature, ...), each answered LIKE or DISLIKE. So per theme there is a
 * small tally, and the useful signal is the theme where someone leaned clearly
 * one way - not any single card. A group is the tallies added together, which is
 * why the thresholds below are RATIOS (liked minus passed, over answered)
 * rather than counts: they read the same for one traveler with three cards per
 * theme and for six travelers with eighteen.
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
  likes: number;
  dislikes: number;
  /** (likes - dislikes) / answers, from -1 (always passed) to 1 (always liked). */
  score: number;
}

export interface TasteSummary {
  /** How many people's answers are in this summary. */
  people: number;
  /** The themes they leaned towards, strongest first. */
  loved: TasteTheme[];
  /** The themes they leaned away from, strongest first. */
  avoided: TasteTheme[];
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

// A theme counts as loved (or avoided) when the answers lean at least this far
// that way: two likes and a pass out of three is exactly 1/3.
const LEAN_THRESHOLD = 1 / 3;

// The prompt names a few themes, not the whole deck: a profile that lists
// everything steers nothing.
const MAX_LOVED = 3;
const MAX_AVOIDED = 2;

// More themes than this leaning the same way means the person said yes (or no)
// to nearly everything, which tells the planner nothing about what stands out -
// and the order they are listed in would silently decide the plan.
const MAX_DISCRIMINATING = 5;

function lean(themes: TasteTheme[], direction: 1 | -1, limit: number): TasteTheme[] {
  const leaning = themes.filter((theme) => theme.score * direction >= LEAN_THRESHOLD);
  if (leaning.length > MAX_DISCRIMINATING) return [];
  // Strongest lean first; the deck's own order settles ties, so the result is
  // the same every time for the same answers.
  return leaning
    .sort((a, b) => (b.score - a.score) * direction || (direction === 1 ? b.likes - a.likes : b.dislikes - a.dislikes))
    .slice(0, limit);
}

/**
 * The summary for these quiz answers, or null when they say nothing usable: no
 * valid rows, or no theme they lean clearly towards or away from. Null is the
 * "personalize nothing" answer - callers then leave the prompts exactly as they
 * were before taste existed.
 *
 * Rows with a category the deck does not deal, or an action that is not LIKE or
 * DISLIKE, are ignored rather than trusted (the API validates both on the way
 * in, but this reads what is in the table).
 */
export function summarizeTaste(rows: TasteRow[]): TasteSummary | null {
  const tallies = new Map<DeckPlaceCategory, { likes: number; dislikes: number }>();
  const people = new Set<string>();

  for (const row of rows) {
    if (!isDeckCategory(row.category)) continue;
    if (row.action !== "LIKE" && row.action !== "DISLIKE") continue;
    const tally = tallies.get(row.category) ?? { likes: 0, dislikes: 0 };
    if (row.action === "LIKE") tally.likes += 1;
    else tally.dislikes += 1;
    tallies.set(row.category, tally);
    people.add(row.userId);
  }

  // Deck order, so every later tie-break is stable.
  const themes: TasteTheme[] = [];
  for (const spec of DECK_CATEGORIES) {
    const tally = tallies.get(spec.category);
    if (!tally) continue;
    const answers = tally.likes + tally.dislikes;
    themes.push({
      category: spec.category,
      label: DECK_CATEGORY_LABELS[spec.category],
      likes: tally.likes,
      dislikes: tally.dislikes,
      score: (tally.likes - tally.dislikes) / answers,
    });
  }

  const loved = lean(themes, 1, MAX_LOVED);
  const avoided = lean(themes, -1, MAX_AVOIDED);
  if (loved.length === 0 && avoided.length === 0) return null;
  return { people: people.size, loved, avoided };
}

function describe(theme: TasteTheme, direction: "loved" | "avoided"): string {
  const answers = theme.likes + theme.dislikes;
  const count = direction === "loved" ? theme.likes : theme.dislikes;
  return `${THEME_PHRASES[theme.category]} (${count} of ${answers} answers ${direction === "loved" ? "liked" : "passed"})`;
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
 * candidate list Stage 2 chooses from.
 */
export function tasteSkeletonBlock(taste: TasteSummary, travelers?: number): string {
  const lines: string[] = [
    `Taste profile of ${whose(taste, travelers)}, from their answers to a swipe quiz about kinds of places:`,
  ];
  if (taste.loved.length > 0) {
    lines.push(`- Loves: ${joinList(taste.loved.map((t) => describe(t, "loved")))}.`);
  }
  if (taste.avoided.length > 0) {
    lines.push(`- Passes on: ${joinList(taste.avoided.map((t) => describe(t, "avoided")))}.`);
  }
  lines.push(
    "Personalize the structure with this. Make what they love the backbone of the days: give each " +
      "loved theme at least one slot where the destination offers it, the strongest theme most often, " +
      "and still mix in other kinds of places so no day repeats itself. Do not schedule the kinds of " +
      "place they pass on, unless a place they asked for by name is one of them."
  );
  return lines.join("\n");
}

/**
 * The paragraph for the Stage 2 (final write) prompt: how to choose between
 * the real candidates Google returned for a slot. Stage 2 can only pick from
 * what Stage 1's categories found, so this is the tie-breaker, and it says
 * outright that the profile is not to be quoted - a caption saying "because you
 * like cafes" is noise on a 15-word line.
 */
export function tasteFinalBlock(taste: TasteSummary, travelers?: number): string {
  const parts: string[] = [];
  if (taste.loved.length > 0) {
    parts.push(`loves ${joinList(taste.loved.map((t) => THEME_PHRASES[t.category]))}`);
  }
  if (taste.avoided.length > 0) {
    parts.push(`passes on ${joinList(taste.avoided.map((t) => THEME_PHRASES[t.category]))}`);
  }
  return (
    `Taste profile of ${whose(taste, travelers)} (never mention it in any caption): ${parts.join("; ")}. ` +
    "When a Stop lists more than one candidate, choose the one that best fits what they love, and " +
    "avoid a candidate that only suits what they pass on. A Stop marked USER-REQUESTED is always kept as asked."
  );
}

/** The labels a caller may show a person ("Nature", "Cafes"), strongest first. */
export function tasteLabels(taste: TasteSummary): { loved: string[]; avoided: string[] } {
  return {
    loved: taste.loved.map((theme) => theme.label),
    avoided: taste.avoided.map((theme) => theme.label),
  };
}
