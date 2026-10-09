/**
 * The persisted/rendered itinerary shape - shared by every route that
 * reads or writes a locked trip's plan: app/api/trigger-jarvis/route.ts
 * (which generates it) and app/api/trip/alternative/route.ts (which
 * patches one stop in it later). Kept in one place so both agree on
 * exactly what's stored in TripDraft.text (see prisma/schema.prisma) -
 * two independently-drifting copies of this schema is exactly how a
 * "the alternative route wrote a shape trigger-jarvis can't parse back"
 * bug would happen.
 *
 * Every field added after the original shape (location,
 * estimatedCostPerPerson, currency, totalTripEstimatedCost, weather, and
 * TravelLeg's `mode`) is optional - a trip locked before that field
 * existed must still parse via parseStoredItinerary rather than being
 * rejected outright. Follow this pattern for any future addition too.
 */

import { z } from "zod";

export const LatLngSchema = z.object({ lat: z.number(), lng: z.number() });

export const TravelModeSchema = z.enum(["WALK", "TRANSIT", "DRIVE"]);
export type TravelMode = z.infer<typeof TravelModeSchema>;

export const TravelLegSchema = z.object({
  durationMinutes: z.number(),
  distanceMeters: z.number(),
  // Optional: a trip locked before dynamic travel-mode selection existed
  // has no mode on its stored legs - the frontend treats a missing mode
  // as "drive" (see formatTravelMinutes' caller in TimelineList.tsx).
  mode: TravelModeSchema.optional(),
});

export const ItineraryStopSchema = z.object({
  slotType: z.enum(["activity", "meal"]),
  // Always written. For a stop with the two fields below it is composed from
  // them ("Name: caption", see lib/stopCopy.ts's composeStopText) so older
  // readers and prompts that quote a stop still get one plain string; for a
  // plan stored before they existed it is the model's whole sentence.
  text: z.string(),
  // The venue's real name (the Places candidate the stop's placeIndex chose
  // - never the model's own spelling) and ONE brief caption for it, at most
  // 15 words (lib/stopCopy.ts's clampShortDescription). What the plan view
  // shows: the name in bold, the caption under it. Optional: absent on a
  // plan stored before they existed (the view falls back to `text`), and
  // placeName is absent on a stop with no real venue behind it.
  placeName: z.string().optional(),
  shortDescription: z.string().optional(),
  travelFromPrevious: TravelLegSchema.nullable(),
  // The real venue's coordinates, when Stage 1.5 grounded this stop in
  // one - null for a stop with no real venue found. Needed (not just
  // nice-to-have) so a later /api/trip/alternative swap can recompute the
  // travel legs on either side of the swapped stop without re-deriving
  // coordinates from scratch.
  location: LatLngSchema.nullable().optional(),
  estimatedCostPerPerson: z.number().nullable().optional(),
});

export const ItineraryDaySchema = z.object({
  day: z.number(),
  summary: z.string(),
  // A short label for the day ("Riverside"), shown after "Day N" when
  // present. Optional: nothing writes it yet (the plan view deliberately
  // does not show `summary`, which is a sentence or more), and a plan
  // stored before this field existed has none.
  title: z.string().optional(),
  stops: z.array(ItineraryStopSchema),
});

export const WeatherSummarySchema = z.object({
  averageTempCelsius: z.number().nullable(),
  conditionText: z.string(),
  iconCode: z.string(),
});

export const ItinerarySchema = z.object({
  destination: z.string(),
  days: z.array(ItineraryDaySchema),
  notes: z.string(),
  // The specific 5-day window (see lib/tripDates.ts) this itinerary was
  // generated for - optional so a plan stored before this field existed
  // still parses.
  startDate: z.string().optional(),
  endDate: z.string().optional(),
  // ISO 4217-ish code the LLM chose for the destination (e.g. "THB"),
  // paired with every stop's estimatedCostPerPerson.
  currency: z.string().optional(),
  // Always code-computed (see computeTotalEstimatedCost below), never
  // trusted directly from the model - same reasoning as this pipeline's
  // existing distrust of a model's own counts/indices (skeleton day
  // count, placeIndex): summing numbers it already wrote is cheap to
  // verify in code and an easy place for a model to be subtly wrong.
  totalTripEstimatedCost: z.number().nullable().optional(),
  weather: WeatherSummarySchema.nullable().optional(),
});

export type Itinerary = z.infer<typeof ItinerarySchema>;
export type ItineraryDay = z.infer<typeof ItineraryDaySchema>;
export type ItineraryStop = z.infer<typeof ItineraryStopSchema>;
export type TravelLeg = z.infer<typeof TravelLegSchema>;
export type WeatherSummary = z.infer<typeof WeatherSummarySchema>;

/**
 * The generated plan is stored as JSON in the trip's TripDraft row
 * (shared with the solo draft board feature - see that model's comment
 * in prisma/schema.prisma) so a repeat "Lock & Generate Plan" click on an
 * already-locked poll, or a later /api/trip/alternative swap, can read
 * back the exact same structured plan rather than re-deriving it from a
 * lossy text format.
 */
export function formatItineraryForStorage(itinerary: Itinerary): string {
  return JSON.stringify(itinerary, null, 2);
}

export function parseStoredItinerary(text: string): Itinerary | null {
  try {
    return ItinerarySchema.parse(JSON.parse(text));
  } catch {
    return null;
  }
}

/**
 * Sums every stop's estimatedCostPerPerson across the whole trip. Null
 * only when NO stop has a cost estimate at all (an old trip locked before
 * this feature existed, or every venue was ungrounded) - a trip where
 * some stops have estimates and others don't still returns the partial
 * sum, on the theory that a known-incomplete total is more useful than
 * hiding it entirely.
 */
export function computeTotalEstimatedCost(itinerary: Pick<Itinerary, "days">): number | null {
  const costs = itinerary.days.flatMap((day) =>
    day.stops
      .map((stop) => stop.estimatedCostPerPerson)
      .filter((cost): cost is number => typeof cost === "number")
  );
  if (costs.length === 0) return null;
  return Math.round(costs.reduce((sum, cost) => sum + cost, 0) * 100) / 100;
}
