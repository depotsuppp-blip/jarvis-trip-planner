/**
 * Google Routes API (New) - Compute Routes, used to enrich a finalized
 * itinerary's day-by-day stop sequence with real travel-time/distance
 * estimates - see app/api/trigger-jarvis/route.ts's Stage 2.5, and
 * app/api/trip/alternative/route.ts's recompute of the legs on either
 * side of a swapped stop.
 *
 * NOT the legacy Distance Matrix API (maps.googleapis.com/maps/api/
 * distancematrix) - "Routes API" is a separate product from that and
 * from lib/places.ts's Places API (New), needing its own enablement on
 * the Google Cloud project even though GOOGLE_MAPS_API_KEY is the same
 * key already used by both.
 *
 * TRAVEL MODE is now picked per LEG (each consecutive stop pair), not
 * once for the whole day - see pickTravelMode. That means one Compute
 * Routes call per leg instead of one multi-stop call per day (Compute
 * Routes has no way to vary travelMode within a single request), run in
 * parallel via Promise.all in computeDayRoute so a day with N stops still
 * costs one round-trip's worth of latency, not N sequential ones.
 */

import type { LatLng } from "./places";
import { haversineDistanceMeters } from "./geo";

export type TravelMode = "WALK" | "TRANSIT" | "DRIVE";

export interface TravelLeg {
  durationMinutes: number;
  distanceMeters: number;
  mode: TravelMode;
}

const COMPUTE_ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes";

// Only the numbers this app ever renders (see TimelineList.tsx) - a
// broader mask (polyline, steps, per-step navigation instructions, ...)
// both costs more and is unused.
const FIELD_MASK = "routes.legs.duration,routes.legs.distanceMeters";

interface ComputeRoutesResponse {
  routes?: {
    legs?: { duration?: string; distanceMeters?: number }[];
  }[];
}

/** Routes API returns duration as a Protobuf Duration string, e.g. "352s". */
function parseDurationSeconds(duration: string | undefined): number | null {
  const match = /^(\d+(?:\.\d+)?)s$/.exec(duration ?? "");
  return match ? Number(match[1]) : null;
}

// Below this straight-line distance, walking is faster door-to-door than
// waiting for transit or a car once boarding/parking is factored in - not
// worth a routed estimate to confirm.
const WALK_THRESHOLD_METERS = 1000;

// Above this straight-line distance, transit is attempted first (see
// requestLegWithFallback) but driving is always the fallback if that
// attempt fails - beyond it, driving is the default outright rather than
// spending a call on a transit attempt unlikely to apply (an inter-city
// hop, e.g.) in most of the destinations this app plans for.
const TRANSIT_ATTEMPT_MAX_METERS = 6000;

function pickTravelMode(straightLineMeters: number): "WALK" | "TRANSIT_THEN_DRIVE" | "DRIVE" {
  if (straightLineMeters < WALK_THRESHOLD_METERS) return "WALK";
  if (straightLineMeters <= TRANSIT_ATTEMPT_MAX_METERS) return "TRANSIT_THEN_DRIVE";
  return "DRIVE";
}

/**
 * One Compute Routes call for exactly one origin->destination leg in the
 * given mode. Returns null (never throws) for any failure - missing key,
 * network error, non-2xx response, or an unusable response - same
 * "degrade to no data" contract as the rest of this module.
 */
async function requestLeg(
  origin: LatLng,
  destination: LatLng,
  mode: TravelMode,
  departureTime: string,
  apiKey: string
): Promise<TravelLeg | null> {
  const toWaypoint = (p: LatLng) => ({
    location: { latLng: { latitude: p.lat, longitude: p.lng } },
  });

  const body: Record<string, unknown> = {
    origin: toWaypoint(origin),
    destination: toWaypoint(destination),
    travelMode: mode,
  };
  if (mode === "DRIVE") {
    // routingPreference (traffic-aware) only applies to DRIVE/TWO_WHEELER
    // - the Routes API rejects it for WALK/TRANSIT.
    body.routingPreference = "TRAFFIC_AWARE";
    body.departureTime = departureTime;
  } else if (mode === "TRANSIT") {
    body.departureTime = departureTime;
  }
  // WALK: no routingPreference, no departureTime - neither is supported
  // for walking directions.

  let response: Response;
  try {
    response = await fetch(COMPUTE_ROUTES_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": apiKey,
        "X-Goog-FieldMask": FIELD_MASK,
      },
      body: JSON.stringify(body),
    });
  } catch (err) {
    console.error(
      `[routes] computeRoutes (${mode}) request failed: ${err instanceof Error ? err.message : String(err)}`
    );
    return null;
  }

  if (!response.ok) {
    const errBody = await response.text().catch(() => "");
    console.error(`[routes] computeRoutes (${mode}) returned HTTP ${response.status}: ${errBody.slice(0, 500)}`);
    return null;
  }

  const data: ComputeRoutesResponse = await response.json().catch(() => ({}));
  const leg = data.routes?.[0]?.legs?.[0];
  const seconds = parseDurationSeconds(leg?.duration);
  if (seconds === null || typeof leg?.distanceMeters !== "number") {
    return null;
  }
  return { durationMinutes: seconds / 60, distanceMeters: leg.distanceMeters, mode };
}

/**
 * Resolves one leg's real travel mode and estimate. WALK and DRIVE are
 * single attempts; the TRANSIT_THEN_DRIVE band tries public transit
 * first and silently falls back to driving if that fails - transit
 * coverage/reliability varies enough by destination (secondary cities
 * with sparse GTFS data, etc.) that "no transit route found" is a common,
 * expected outcome here, not a real failure to surface as one.
 */
async function computeLegRoute(
  origin: LatLng,
  destination: LatLng,
  departureTime: string,
  apiKey: string
): Promise<TravelLeg | null> {
  const straightLineMeters = haversineDistanceMeters(origin, destination);
  const modeChoice = pickTravelMode(straightLineMeters);

  if (modeChoice === "WALK") {
    return requestLeg(origin, destination, "WALK", departureTime, apiKey);
  }
  if (modeChoice === "DRIVE") {
    return requestLeg(origin, destination, "DRIVE", departureTime, apiKey);
  }
  const transitLeg = await requestLeg(origin, destination, "TRANSIT", departureTime, apiKey);
  if (transitLeg) return transitLeg;
  return requestLeg(origin, destination, "DRIVE", departureTime, apiKey);
}

/**
 * One day's ordered stop-to-stop routes: N stops in visiting order -> N-1
 * legs, one per consecutive pair, each independently mode-selected (see
 * computeLegRoute) and requested in parallel. An individual leg's entry
 * is null (never the whole array) if that specific transition's routing
 * failed - a day-level failure can no longer be attributed to "the whole
 * day," since each leg is now its own independent request; every other
 * leg's real estimate is still worth keeping. Returns null outright only
 * when there's nothing to route (fewer than 2 stops) or no API key is
 * configured.
 */
export async function computeDayRoute(
  stops: LatLng[],
  departureTime: string
): Promise<(TravelLeg | null)[] | null> {
  if (stops.length < 2) return null;

  const apiKey = process.env.GOOGLE_MAPS_API_KEY;
  if (!apiKey) {
    console.error("[routes] GOOGLE_MAPS_API_KEY is not configured.");
    return null;
  }

  return Promise.all(
    stops.slice(1).map((stop, i) => computeLegRoute(stops[i], stop, departureTime, apiKey))
  );
}

// Mid-morning is a reasonable default departure for a day of sightseeing
// - not load-bearing precision, since TRAFFIC_AWARE's estimate for a
// date weeks out is a historical-pattern prediction regardless of the
// exact hour. Left in UTC rather than resolved to the destination's
// actual timezone (which this app doesn't otherwise track anywhere) -
// still lands the estimate on the right DAY, which is what matters for a
// weekday-vs-weekend traffic pattern.
const DEFAULT_DEPARTURE_HOUR_UTC = 10;

// Used only when no vote supplied a start date at all - still anchors the
// estimate to a plausible FUTURE date (required by Routes API's
// departureTime) rather than "now", which would request current traffic
// instead of a general future-pattern estimate. Mirrors
// FALLBACK_START_DAYS_FROM_NOW in lib/tripDates.ts, so an undated trip
// anchors to the same near-future date everywhere in this pipeline.
const FALLBACK_DAYS_FROM_NOW = 14;

/**
 * RFC3339 UTC timestamp for itinerary day N's departure - tripStartDate
 * (day 1's date) plus (dayNumber - 1) days, at a fixed mid-morning hour.
 * Clamped to at least tomorrow if that lands in the past (a poll whose
 * voted dates have already elapsed, or a stop being swapped well after
 * its original generation) or if tripStartDate is missing/unparseable
 * entirely - Routes API rejects a past departureTime.
 */
export function computeDepartureTimeForDay(tripStartDate: string | null, dayNumber: number): string {
  const fallback = new Date();
  fallback.setUTCDate(fallback.getUTCDate() + FALLBACK_DAYS_FROM_NOW);
  fallback.setUTCHours(DEFAULT_DEPARTURE_HOUR_UTC, 0, 0, 0);

  let departure = fallback;
  if (tripStartDate) {
    const parsed = new Date(`${tripStartDate}T00:00:00Z`);
    if (!Number.isNaN(parsed.getTime())) {
      parsed.setUTCDate(parsed.getUTCDate() + (dayNumber - 1));
      parsed.setUTCHours(DEFAULT_DEPARTURE_HOUR_UTC, 0, 0, 0);
      departure = parsed;
    }
  }

  const tomorrow = new Date();
  tomorrow.setUTCDate(tomorrow.getUTCDate() + 1);
  if (departure.getTime() < tomorrow.getTime()) {
    departure = tomorrow;
    departure.setUTCHours(DEFAULT_DEPARTURE_HOUR_UTC, 0, 0, 0);
  }

  return departure.toISOString();
}
