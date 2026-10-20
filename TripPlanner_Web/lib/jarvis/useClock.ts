import { useSyncExternalStore } from "react";
import { greetingForHour } from "./sessions";

/**
 * Time, read the way React wants it: not `Date.now()` in a render, but a store
 * that says "it is now this minute" and ticks. The server renders with the
 * server snapshot (0 / "Hello"), the browser corrects it right after hydration,
 * so there is never a mismatch between the two.
 */

const MINUTE_MS = 60_000;

const listeners = new Set<() => void>();
let ticker: number | undefined;

function subscribeToMinutes(listener: () => void) {
  listeners.add(listener);
  if (listeners.size === 1) {
    ticker = window.setInterval(() => listeners.forEach((notify) => notify()), MINUTE_MS);
  }
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) window.clearInterval(ticker);
  };
}

/** The current time rounded down to the minute; changes once a minute. 0 on the server. */
export function useMinuteClock(): number {
  return useSyncExternalStore(
    subscribeToMinutes,
    () => Math.floor(Date.now() / MINUTE_MS) * MINUTE_MS,
    () => 0
  );
}

const subscribeNever = () => () => {};

/** "Good morning" / "Good afternoon" / "Good evening" by the visitor's own clock; "Hello" until the browser has said. */
export function useGreeting(): string {
  return useSyncExternalStore(
    subscribeNever,
    () => greetingForHour(new Date().getHours()),
    () => "Hello"
  );
}
