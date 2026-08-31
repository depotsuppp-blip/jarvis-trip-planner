/**
 * Google Routes API (New) - Compute Routes, used to enrich a finalized
 * itinerary's day-by-day stop sequence with real drive-time/distance
 * estimates - see app/api/trigger-jarvis/route.ts's Stage 2.5.
 *
 * Deliberately Compute Routes, not Compute Route Matrix: a day's stops
 * are visited in one fixed sequence (origin -> intermediates ->
 * destination), not an all-pairs matrix - Route Matrix would compute
 * N*N pairs this app never uses and bill for all of them. Also NOT the
 * legacy Distance Matrix API (maps.googleapis.com/maps/api/
 * distancematrix) - "Routes API" is a separate product from both,
 * needing its own enablement on the Google Cloud project even though
 * GOOGLE_MAPS_API_KEY is the same key already used by lib/places.ts and
 * plugins/trip_planner.py's legacy Places calls.
 */

import type { LatLng } from "./places";

export interface TravelLeg {
  durationMinutes: number;
  distanceMeters: number;
}

const COMPUTE_ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes";

// Only the two numbers this app ever renders (see the "🚗 ~N min" label
// in app/trip/poll/[id]/page.tsx) - a broader mask (polyline, steps,
// per-step navigation instructions, ...) both costs more and is unused.
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

/**
 * One day's ordered stop-to-stop route: N stops in visiting order ->
 * N-1 legs, one per consecutive pair. Returns null (never throws) for
 * ANY failure - missing API key, network error, non-2xx response, or a
 * response with no usable route - so a single day's routing trouble
 * degrades to "no travel times shown for this day" rather than failing
 * the whole itinerary generation (which already spent real Anthropic +
 * Places quota by this point). The specific failure reason is logged
 * server-side only; callers get a plain null since none of the failure
 * modes are actionable by the end user mid-generation.
 */
export async function computeDayRoute(
  stops: LatLng[],
  departureTime: string
): Promise<TravelLeg[] | null> {
  if (stops.length < 2) return null;

  const apiKey = process.env.GOOGLE_MAPS_API_KEY;
  if (!apiKey) {
    console.error("[routes] GOOGLE_MAPS_API_KEY is not configured.");
    return null;
  }

  const [origin, ...rest] = stops;
  const destination = rest[rest.length - 1];
  const intermediates = rest.slice(0, -1);
  const toWaypoint = (p: LatLng) => ({
    location: { latLng: { latitude: p.lat, longitude: p.lng } },
  });

  let response: Response;
  try {
    response = await fetch(COMPUTE_ROUTES_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": apiKey,
        "X-Goog-FieldMask": FIELD_MASK,
      },
      body: JSON.stringify({
        origin: toWaypoint(origin),
        destination: toWaypoint(destination),
        intermediates: intermediates.map(toWaypoint),
        travelMode: "DRIVE",
        routingPreference: "TRAFFIC_AWARE",
        departureTime,
      }),
    });
  } catch (err) {
    console.error(
      `[routes] computeRoutes request failed: ${err instanceof Error ? err.message : String(err)}`
    );
    return null;
  }

  if (!response.ok) {
    const body = await response.text().catch(() => "");
    console.error(`[routes] computeRoutes returned HTTP ${response.status}: ${body.slice(0, 500)}`);
    return null;
  }

  const data: ComputeRoutesResponse = await response.json().catch(() => ({}));
  const legs = data.routes?.[0]?.legs;
  if (!legs || legs.length !== stops.length - 1) {
    console.error(
      `[routes] computeRoutes returned ${legs?.length ?? 0} legs for ${stops.length} stops - expected ${stops.length - 1}.`
    );
    return null;
  }

  const parsed: TravelLeg[] = [];
  for (const leg of legs) {
    const seconds = parseDurationSeconds(leg.duration);
    if (seconds === null || typeof leg.distanceMeters !== "number") {
      // A single unparseable leg invalidates the whole day's sequence -
      // callers render travel times per consecutive pair, and a gap
      // here would misattribute an adjacent leg's numbers to the wrong
      // transition. This is still "no travel time shown," never a
      // thrown error.
      console.error("[routes] a leg in computeRoutes' response was missing duration/distanceMeters.");
      return null;
    }
    parsed.push({ durationMinutes: seconds / 60, distanceMeters: leg.distanceMeters });
  }
  return parsed;
}
