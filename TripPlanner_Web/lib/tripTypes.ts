/**
 * Shared shapes for the Poll page and the components it composes
 * (TripSummary, TimelineList) - split out so those components don't each
 * redeclare the same itinerary/vote shapes the page already reads from
 * the API. Mirrors the server-side persisted shape in lib/itinerary.ts,
 * but as plain TS interfaces (no zod) since the frontend only ever reads
 * this as already-validated JSON from the API, never parses raw input
 * with it.
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

export type TravelMode = "WALK" | "TRANSIT" | "DRIVE";

export interface TravelLeg {
  durationMinutes: number;
  distanceMeters: number;
  // Optional: a trip locked before dynamic travel-mode selection existed
  // has no mode on its stored legs - see formatTravelMinutes' callers,
  // which treat a missing mode as "drive".
  mode?: TravelMode;
}

export interface LatLng {
  lat: number;
  lng: number;
}

export interface ItineraryStop {
  slotType: "activity" | "meal";
  text: string;
  // From the PRECEDING stop in this same day's array - null for a day's
  // first stop, or wherever Stage 2.5 (app/api/trigger-jarvis/route.ts)
  // had no route data (no coordinates for one of the two stops, or that
  // specific leg's Routes API call failed). A future-dated, historical-
  // pattern estimate, not live traffic - see the "~" in how this
  // renders.
  travelFromPrevious: TravelLeg | null;
  // The real venue's coordinates, when one was grounded for this stop -
  // absent on a trip locked before this field existed.
  location?: LatLng | null;
  // Estimated cost per person in the itinerary's `currency` - absent on a
  // trip locked before this field existed, or null for an ungrounded
  // stop.
  estimatedCostPerPerson?: number | null;
}

export interface ItineraryDay {
  day: number;
  summary: string;
  stops: ItineraryStop[];
}

export interface WeatherSummary {
  averageTempCelsius: number | null;
  conditionText: string;
  iconCode: string;
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
  currency?: string;
  totalTripEstimatedCost?: number | null;
  weather?: WeatherSummary | null;
}

// Rounded, never 0 - "~0 min" would read as broken rather than "very
// close by". The "(estimate)" wording sits next to this at the call
// site, not baked in here, since this also feeds the title tooltip.
export function formatTravelMinutes(minutes: number): string {
  return `~${Math.max(1, Math.round(minutes))} min`;
}

/** e.g. formatCost(120, "THB") -> "120 THB". */
export function formatCost(amount: number, currency: string): string {
  return `${amount.toLocaleString()} ${currency}`.trim();
}
