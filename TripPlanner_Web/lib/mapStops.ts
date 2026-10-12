import type { DayStop } from "@/lib/itineraryDays";
import type { LatLng } from "@/lib/tripTypes";

/**
 * What the route map reads from one day's stops: the ones it can place (a real,
 * in-range coordinate pair), each with the number it carries on the timeline,
 * and a count of the ones it cannot. The timeline and the map both come from
 * the same DayGroup, so a pin's number is always its stop's number.
 */

export interface MapStop {
  /** 1-based number shown on the timeline and on this stop's pin. */
  number: number;
  /** The venue's own name, when the plan has one (a plan stored earlier has only a sentence). */
  name: string | null;
  position: LatLng;
}

export interface LocatedStops {
  stops: MapStop[];
  /** Stops of the day that have no usable coordinates and so are not on the map. */
  missing: number;
}

/** A box on the map, in the shape the Maps API takes (google.maps.LatLngBoundsLiteral). */
export interface BoundsLiteral {
  north: number;
  south: number;
  east: number;
  west: number;
}

function isUsable(location: LatLng | null | undefined): location is LatLng {
  return (
    location != null &&
    Number.isFinite(location.lat) &&
    Number.isFinite(location.lng) &&
    Math.abs(location.lat) <= 90 &&
    Math.abs(location.lng) <= 180
  );
}

export function locateStops(stops: DayStop[]): LocatedStops {
  const located = stops.flatMap(({ stop, number }) =>
    isUsable(stop.location)
      ? [{ number, name: stop.placeName?.trim() || null, position: { lat: stop.location.lat, lng: stop.location.lng } }]
      : []
  );
  return { stops: located, missing: stops.length - located.length };
}

// A day whose stops all sit within a few hundred metres (or a single stop)
// would otherwise be fitted at the map's maximum zoom, which shows a pin and
// no street around it. The fitted area is never smaller than this many
// degrees on a side (about 900 m at the equator, a little less east-west
// further north).
const MIN_SPAN_DEGREES = 0.008;

/**
 * The smallest box holding every point, grown to MIN_SPAN_DEGREES on each axis
 * where it is smaller. Plain min/max, so it needs nothing from the Maps API
 * and can be computed before that has loaded; a day that crosses the
 * antimeridian is not a case this app plans for.
 */
export function boundsOf(points: LatLng[]): BoundsLiteral | null {
  if (points.length === 0) return null;

  let south = Infinity;
  let north = -Infinity;
  let west = Infinity;
  let east = -Infinity;
  for (const { lat, lng } of points) {
    south = Math.min(south, lat);
    north = Math.max(north, lat);
    west = Math.min(west, lng);
    east = Math.max(east, lng);
  }

  const growLat = Math.max(0, (MIN_SPAN_DEGREES - (north - south)) / 2);
  const growLng = Math.max(0, (MIN_SPAN_DEGREES - (east - west)) / 2);

  return {
    south: Math.max(-90, south - growLat),
    north: Math.min(90, north + growLat),
    west: Math.max(-180, west - growLng),
    east: Math.min(180, east + growLng),
  };
}
