/**
 * Shared shapes for the Poll page and the components it composes
 * (TripSummary, TimelineList) - split out so those components don't each
 * redeclare the same itinerary/vote shapes the page already reads from
 * the API.
 */

export interface PollVote {
  name: string;
  lineUserId: string;
  startDate: string;
  endDate: string;
  wishlist: string;
  vibes: string[];
  submittedAt: string;
}

export interface TravelLeg {
  durationMinutes: number;
  distanceMeters: number;
}

export interface ItineraryStop {
  slotType: "activity" | "meal";
  text: string;
  // From the PRECEDING stop in this same day's array - null for a day's
  // first stop, or wherever Stage 2.5 (app/api/trigger-jarvis/route.ts)
  // had no route data (no coordinates for one of the two stops, or the
  // Routes API call for that day failed). A future-dated, historical-
  // pattern estimate, not live traffic - see the "~" in how this
  // renders.
  travelFromPrevious: TravelLeg | null;
}

export interface ItineraryDay {
  day: number;
  summary: string;
  stops: ItineraryStop[];
}

export interface Itinerary {
  destination: string;
  days: ItineraryDay[];
  notes: string;
  // The specific 5-day trip window this itinerary was generated for (see
  // app/api/trigger-jarvis/route.ts's tripWindow) - optional since a plan
  // generated before this field existed still parses without it; the
  // poll page falls back to the raw vote date range when absent.
  startDate?: string;
  endDate?: string;
}

// Rounded, never 0 - "~0 min" would read as broken rather than "very
// close by". The "(estimate)" wording sits next to this at the call
// site, not baked in here, since this also feeds the title tooltip.
export function formatTravelMinutes(minutes: number): string {
  return `~${Math.max(1, Math.round(minutes))} min`;
}
