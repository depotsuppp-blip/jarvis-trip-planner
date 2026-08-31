/**
 * Small formatting helpers shared by the Poll page's boarding pass and
 * the Dashboard's overview card - both derive a human date range from
 * the same raw vote list, and duplicating the logic risked the two
 * pages disagreeing on what "TBD" or a formatted range looks like.
 */

export function formatShortDate(dateStr: string): string {
  const parsed = new Date(`${dateStr}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime())) return dateStr;
  return parsed.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
}

export function computeDateRangeLabel(
  votes: { startDate: string; endDate: string }[]
): string {
  const starts = votes.map((v) => v.startDate).filter(Boolean).sort();
  const ends = votes.map((v) => v.endDate).filter(Boolean).sort();
  if (starts.length === 0 && ends.length === 0) return "TBD";
  return `${formatShortDate(starts[0] ?? ends[0])} – ${formatShortDate(
    ends[ends.length - 1] ?? starts[starts.length - 1]
  )}`;
}

/**
 * The earliest voted "From" date, as a raw YYYY-MM-DD string (not the
 * formatted label computeDateRangeLabel produces) - null if no vote
 * supplied one. Used by app/api/trigger-jarvis/route.ts's Stage 2.5 to
 * anchor each itinerary day's Routes API departureTime to the trip's
 * actual likely date (day 1 = this date, day N = this date + N-1 days)
 * rather than "now", which would ask for current traffic instead of a
 * plausible future estimate.
 */
export function earliestStartDate(votes: { startDate: string }[]): string | null {
  const starts = votes.map((v) => v.startDate).filter(Boolean).sort();
  return starts[0] ?? null;
}

export interface VibeTally {
  vibe: string;
  count: number;
}

export function tallyVibes(votes: { vibes: string[] }[]): VibeTally[] {
  const counts = new Map<string, number>();
  for (const vote of votes) {
    for (const vibe of vote.vibes ?? []) {
      counts.set(vibe, (counts.get(vibe) ?? 0) + 1);
    }
  }
  return [...counts.entries()]
    .sort((a, b) => b[1] - a[1])
    .map(([vibe, count]) => ({ vibe, count }));
}

export interface PollSummary {
  totalVotes: number;
  dateRangeLabel: string;
  topVibes: VibeTally[];
  wishlist: string[];
  voters: string[];
}

/**
 * The aggregated shape the poll API's GET route hands to any consumer
 * that wants totals rather than raw votes - originally added so the
 * Python backend's finalize_trip_plan_async (plugins/trip_planner.py)
 * has one clean object to read instead of re-implementing this same
 * tallying logic in a second language.
 */
export function summarizePollVotes(
  votes: {
    name: string;
    startDate: string;
    endDate: string;
    wishlist: string;
    vibes: string[];
  }[]
): PollSummary {
  return {
    totalVotes: votes.length,
    dateRangeLabel: computeDateRangeLabel(votes),
    topVibes: tallyVibes(votes),
    wishlist: votes.map((v) => v.wishlist.trim()).filter(Boolean),
    voters: votes.map((v) => v.name),
  };
}
