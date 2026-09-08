/**
 * Great-circle distance between two points, in meters - used by
 * lib/routes.ts to pick a travel mode BEFORE spending a billed Routes API
 * call, and by app/api/trip/alternative/route.ts's Nearby Search radius.
 * Accurate enough for mode selection and search radii; not meant to
 * substitute for real routing distance, which the Routes API itself
 * provides once a mode is chosen.
 */

import type { LatLng } from "./places";

const EARTH_RADIUS_METERS = 6_371_000;

export function haversineDistanceMeters(a: LatLng, b: LatLng): number {
  const toRad = (deg: number) => (deg * Math.PI) / 180;
  const dLat = toRad(b.lat - a.lat);
  const dLng = toRad(b.lng - a.lng);
  const lat1 = toRad(a.lat);
  const lat2 = toRad(b.lat);

  const sinDLat = Math.sin(dLat / 2);
  const sinDLng = Math.sin(dLng / 2);
  const h = sinDLat * sinDLat + Math.cos(lat1) * Math.cos(lat2) * sinDLng * sinDLng;
  return 2 * EARTH_RADIUS_METERS * Math.asin(Math.min(1, Math.sqrt(h)));
}
