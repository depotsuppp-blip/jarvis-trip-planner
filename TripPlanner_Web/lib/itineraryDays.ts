import type { ItineraryDay, ItineraryStop } from "@/lib/tripTypes";

/**
 * How the plan view reads a stored itinerary: grouped by day, with the
 * numbers and dates the screen shows worked out once. The itinerary list and
 * the route map both take their data from this one grouping, so what a day
 * contains can't drift between them - and choosing a day (the tabs' state,
 * see components/trip/PlanView.tsx) is just picking one DayGroup.
 */

export interface DayStop {
  stop: ItineraryStop;
  /**
   * Position in the day's stored `stops` array - what POST
   * /api/trip/alternative calls `stopIndex`. Stops are never filtered out
   * of a day, so this is also `number - 1`.
   */
  index: number;
  /** 1-based number shown on the timeline and on this stop's pin on the map. */
  number: number;
}

export interface DayGroup {
  day: number;
  /** The short title when the plan has one (see ItineraryDaySchema.title). */
  title: string | null;
  /** Calendar date (YYYY-MM-DD) of this day, when the plan carries its start date. */
  date: string | null;
  stops: DayStop[];
  /** Sum of the day's per-person cost estimates; null when no stop has one. */
  estimatedCost: number | null;
}

const ISO_DATE = /^(\d{4})-(\d{2})-(\d{2})$/;

/**
 * Day N falls N-1 days after the plan's start date (the generator numbers
 * its days in calendar order from the start of the window). UTC arithmetic,
 * so the result doesn't shift with the viewer's timezone.
 */
export function dateForDay(startDate: string | undefined, day: number): string | null {
  const match = startDate ? ISO_DATE.exec(startDate) : null;
  if (!match) return null;

  const date = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]) + day - 1));
  return Number.isNaN(date.getTime()) ? null : date.toISOString().slice(0, 10);
}

const dayDateFormat = new Intl.DateTimeFormat("en-US", {
  weekday: "short",
  month: "short",
  day: "numeric",
  timeZone: "UTC",
});

/** "2026-11-12" -> "Thu, Nov 12" (same style as lib/tripSummary.ts's formatShortDate). */
export function formatDayDate(isoDate: string): string {
  return dayDateFormat.format(new Date(`${isoDate}T00:00:00Z`));
}

/**
 * One group per day that has anything to show. A day with no stops is
 * dropped rather than rendered as an empty card - the generator is asked
 * for a full day, but a short or failed one must not leave a hollow "Day 5".
 * The remaining days keep their own numbers, so a hidden Day 3 leaves a gap
 * in the tabs rather than renaming Day 4.
 */
export function groupItineraryByDay(days: ItineraryDay[], startDate?: string): DayGroup[] {
  return days
    .filter((day) => day.stops.length > 0)
    .map((day) => {
      const costs = day.stops
        .map((stop) => stop.estimatedCostPerPerson)
        .filter((cost): cost is number => typeof cost === "number");

      return {
        day: day.day,
        title: day.title?.trim() || null,
        date: dateForDay(startDate, day.day),
        stops: day.stops.map((stop, index) => ({ stop, index, number: index + 1 })),
        estimatedCost:
          costs.length > 0 ? Math.round(costs.reduce((sum, cost) => sum + cost, 0) * 100) / 100 : null,
      };
    });
}
