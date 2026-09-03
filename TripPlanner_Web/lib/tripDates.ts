/**
 * Picks the actual trip dates from a poll's votes - this app only ever
 * plans a fixed-length trip ("5 Days 4 Nights"), so the question isn't
 * "what's the group's combined date range" (the old min/max-of-all-votes
 * logic in computeDateRangeLabel, still used for the poll's informational
 * displays) but "which specific TRIP_WINDOW_DAYS-day window works for the
 * most people." See app/api/trigger-jarvis/route.ts, the only caller that
 * feeds this into the LLM prompt.
 */

export const TRIP_WINDOW_DAYS = 5;

// Used only when no vote supplied a usable date range at all - mirrors
// FALLBACK_DAYS_FROM_NOW in app/api/trigger-jarvis/route.ts (Stage 2.5's
// own past-date fallback), so an undated trip still anchors to the same
// plausible near-future date everywhere in this pipeline.
const FALLBACK_START_DAYS_FROM_NOW = 14;

export interface TripDateWindow {
  startDate: string; // YYYY-MM-DD, inclusive
  endDate: string; // YYYY-MM-DD, inclusive - always startDate + (TRIP_WINDOW_DAYS - 1) days
  // How many voters' [startDate, endDate] range overlaps this window at
  // all (shares at least one day with it) - not how many are free for
  // the ENTIRE window. "Most people can join at least part of this trip"
  // is the majority-vote question being answered, not "everyone's fully
  // free."
  voterCount: number;
}

function addDaysISO(dateStr: string, days: number): string {
  const d = new Date(`${dateStr}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

function daysBetweenISO(a: string, b: string): number {
  const da = new Date(`${a}T00:00:00Z`).getTime();
  const db = new Date(`${b}T00:00:00Z`).getTime();
  return Math.round((db - da) / 86_400_000);
}

/** Inclusive day count where [a.start, a.end] and [b.start, b.end] overlap - 0 if they don't. */
function overlapDays(a: { start: string; end: string }, b: { start: string; end: string }): number {
  const start = a.start > b.start ? a.start : b.start;
  const end = a.end < b.end ? a.end : b.end;
  return start <= end ? daysBetweenISO(start, end) + 1 : 0;
}

/**
 * Finds the consecutive TRIP_WINDOW_DAYS-day window overlapping the most
 * voters' stated availability, replacing the old approach of just taking
 * the earliest startDate and latest endDate across every vote (which
 * produces a span as wide as the group's combined range, not a real trip
 * length, and ignores that the group might not actually agree on any
 * single stretch that long).
 *
 * Brute-forces every candidate start date across the voted range rather
 * than a smarter sweep-line algorithm - the search space is at most a
 * few months of days for a trip poll, so there's no real performance to
 * trade simplicity for.
 */
export function computeBestTripWindow(
  votes: { startDate: string; endDate: string }[],
  today: Date = new Date()
): TripDateWindow {
  const intervals = votes
    .map((v) => ({ start: v.startDate, end: v.endDate }))
    .filter((v) => v.start && v.end && v.start <= v.end);

  if (intervals.length === 0) {
    const fallbackStart = addDaysISO(
      today.toLocaleDateString("en-CA"),
      FALLBACK_START_DAYS_FROM_NOW
    );
    return {
      startDate: fallbackStart,
      endDate: addDaysISO(fallbackStart, TRIP_WINDOW_DAYS - 1),
      voterCount: 0,
    };
  }

  const rangeStart = intervals.reduce((min, iv) => (iv.start < min ? iv.start : min), intervals[0].start);
  const rangeEnd = intervals.reduce((max, iv) => (iv.end > max ? iv.end : max), intervals[0].end);

  let best: TripDateWindow | null = null;
  let bestOverlapDays = -1;
  // Starts as early as (rangeStart - (TRIP_WINDOW_DAYS - 1)) - the
  // earliest a window could start and still touch the earliest voted
  // day - through rangeEnd, the latest a window could start and still
  // touch the latest voted day. Anything outside that span overlaps no
  // one.
  let cursor = addDaysISO(rangeStart, -(TRIP_WINDOW_DAYS - 1));
  while (cursor <= rangeEnd) {
    const windowEnd = addDaysISO(cursor, TRIP_WINDOW_DAYS - 1);
    const window = { start: cursor, end: windowEnd };
    const voterCount = intervals.filter((iv) => overlapDays(iv, window) > 0).length;
    // Total days of overlap, summed across every voter, is the tie-break
    // once voterCount ties - a window that just grazes the last day of
    // two voters' ranges shouldn't beat one that sits fully inside both,
    // even though a plain overlap/no-overlap count treats them the same.
    const totalOverlapDays = intervals.reduce((sum, iv) => sum + overlapDays(iv, window), 0);
    if (
      !best ||
      voterCount > best.voterCount ||
      (voterCount === best.voterCount && totalOverlapDays > bestOverlapDays)
    ) {
      best = { startDate: cursor, endDate: windowEnd, voterCount };
      bestOverlapDays = totalOverlapDays;
    }
    cursor = addDaysISO(cursor, 1);
  }

  return best as TripDateWindow;
}
