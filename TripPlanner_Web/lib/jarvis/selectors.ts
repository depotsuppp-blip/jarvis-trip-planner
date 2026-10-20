import { sessionTitle } from "./sessions";
import type { ChatState } from "./types";

/**
 * Selectors that need more than a field read. Each returns a primitive, so it
 * is safe to hand to `useChat` (see store.ts: a selector must return the same
 * value while what it reads has not changed).
 */

/** The title of the open session, for the top bar. */
export function selectActiveTitle(state: ChatState): string {
  if (state.activeSessionId === null) return "New session";
  return sessionTitle(state.sessions.items.find((item) => item.id === state.activeSessionId));
}
