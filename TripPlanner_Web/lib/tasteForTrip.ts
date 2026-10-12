/**
 * Reads the Taste Profile a trip's plan should be built around: the one place
 * the generating routes (POST /api/trigger-jarvis for a group's poll, POST
 * /api/trip/create for one person) turn "whose trip is this" into the
 * TastePromptInput lib/tripGeneration.ts puts in its prompts.
 *
 * Kept apart from lib/tripGeneration.ts (pipeline: Anthropic, Places, Routes)
 * and lib/tasteSummary.ts (pure text) because this is the only piece that
 * touches the database.
 */

import { getTasteSwipesForUsers } from "./store";
import { summarizeTaste } from "./tasteSummary";
import type { TastePromptInput } from "./tripGeneration";

/**
 * The combined Taste Profile of the accounts `resolveUserIds` names, as the
 * generation prompts take it - or null when there is none to use: nobody in the
 * trip has a finished quiz, or their answers lean no clear way. `travelers` is
 * how many people the plan is for (a poll's voters, a head count), so the prompt
 * can say how many of them the profile covers.
 *
 * `resolveUserIds` is a function, not a list, so the lookup of WHO is in the trip
 * (a query, for a poll) is covered by the same never-throws guarantee as the
 * profile read itself.
 *
 * NEVER THROWS. Personalization is an enhancement: a failed lookup must not fail
 * a generation that is about to spend real Anthropic and Places quota. A failure
 * is logged and the plan is built as it always was, without a profile.
 */
export async function loadTastePromptInput(
  resolveUserIds: () => Promise<string[]>,
  travelers: number,
  label: string
): Promise<TastePromptInput | null> {
  try {
    const summary = summarizeTaste(await getTasteSwipesForUsers(await resolveUserIds()));
    return summary ? { taste: summary, travelers } : null;
  } catch (error) {
    console.error(`[taste] ${label}: could not read the taste profiles; planning without them:`, error);
    return null;
  }
}
