import type { SessionSummary } from "./types";

/**
 * Pure helpers for the sidebar's session list: how sessions group by day and how
 * each is titled and timed. `now` is always passed in (never read here) so the
 * result is a function of its inputs and the caller decides how often it ticks.
 */

export interface SessionGroup {
  label: string;
  items: SessionSummary[];
}

const DAY_MS = 24 * 60 * 60 * 1000;

function startOfDay(time: number): number {
  const date = new Date(time);
  date.setHours(0, 0, 0, 0);
  return date.getTime();
}

/** Today, Yesterday, the days before that up to a week back, then Earlier; newest first, empty groups left out. */
export function groupSessions(items: SessionSummary[], now: number): SessionGroup[] {
  const today = startOfDay(now);
  const buckets: SessionGroup[] = [
    { label: "Today", items: [] },
    { label: "Yesterday", items: [] },
    { label: "Previous 7 days", items: [] },
    { label: "Earlier", items: [] },
  ];

  for (const item of [...items].sort((a, b) => b.updatedAt - a.updatedAt)) {
    const day = startOfDay(item.updatedAt);
    if (day >= today) buckets[0].items.push(item);
    else if (day >= today - DAY_MS) buckets[1].items.push(item);
    else if (day >= today - 7 * DAY_MS) buckets[2].items.push(item);
    else buckets[3].items.push(item);
  }

  return buckets.filter((bucket) => bucket.items.length > 0);
}

export function sessionTitle(session: Pick<SessionSummary, "title"> | null | undefined): string {
  return session?.title?.trim() || "Untitled session";
}

const timeOfDay = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" });
const dayOfMonth = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short" });
const weekday = new Intl.DateTimeFormat(undefined, { weekday: "short" });

/** "09:41" today, the weekday within the last week, else "12 Oct": the quiet right-hand label of a session row. */
export function formatSessionTime(updatedAt: number, now: number): string {
  const today = startOfDay(now);
  const day = startOfDay(updatedAt);
  if (day >= today) return timeOfDay.format(updatedAt);
  if (day >= today - 6 * DAY_MS) return weekday.format(updatedAt);
  return dayOfMonth.format(updatedAt);
}

/** The clock time under a message. */
export function formatMessageTime(time: number): string {
  return timeOfDay.format(time);
}

/** "Good morning" before noon, "Good afternoon" until 6pm, else "Good evening". */
export function greetingForHour(hour: number): string {
  if (hour < 5) return "Good evening";
  if (hour < 12) return "Good morning";
  if (hour < 18) return "Good afternoon";
  return "Good evening";
}
