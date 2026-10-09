import type { DayStop } from "@/lib/itineraryDays";

/**
 * Lays one day's stops out as a route sketch: where each numbered pin goes
 * on a square stage, and the line that joins them in order. No tiles and no
 * map library - just the coordinates the plan already stores, so it works
 * today and keeps the contract a real basemap must later honour (the pin
 * numbers are the timeline's stop numbers).
 */

export interface SketchPin {
  number: number;
  /** Percent of the stage, 0-100, left to right. */
  x: number;
  /** Percent of the stage, 0-100, top to bottom (north is up). */
  y: number;
}

export interface RouteSketch {
  pins: SketchPin[];
  /** The stops' pins joined in order, as an SVG `points` value on a 0-100 viewBox. */
  line: string;
  /** Stops of the day that have no stored coordinates and so are not drawn. */
  missing: number;
}

// Keeps a pin (24px on a stage of a few hundred) from touching the edge.
const STAGE_PADDING = 12;
// Pins closer than this (percent of the stage) are pushed apart so numbers
// stay readable when several stops share a neighbourhood.
const MIN_PIN_GAP = 7;

export function sketchRoute(stops: DayStop[]): RouteSketch {
  const located = stops.flatMap(({ stop, number }) => {
    const location = stop.location;
    return location && Number.isFinite(location.lat) && Number.isFinite(location.lng)
      ? [{ number, lat: location.lat, lng: location.lng }]
      : [];
  });
  const missing = stops.length - located.length;

  if (located.length === 0) {
    return { pins: [], line: "", missing };
  }

  // Degrees of longitude shrink with latitude; scaling by cos(latitude)
  // keeps the sketch's shape true at city scale instead of stretching it.
  const meanLat = located.reduce((sum, p) => sum + p.lat, 0) / located.length;
  const lngScale = Math.max(Math.cos((meanLat * Math.PI) / 180), 0.01);
  const planar = located.map((p) => ({ number: p.number, px: p.lng * lngScale, py: -p.lat }));

  const xs = planar.map((p) => p.px);
  const ys = planar.map((p) => p.py);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  const span = Math.max(maxX - minX, maxY - minY);
  const scale = span > 0 ? (100 - 2 * STAGE_PADDING) / span : 0;

  const pins: SketchPin[] = planar.map((p) => ({
    number: p.number,
    x: 50 + (p.px - (minX + maxX) / 2) * scale,
    y: 50 + (p.py - (minY + maxY) / 2) * scale,
  }));

  // One pass, in stop order: a pin too close to an earlier one steps away
  // from it (along the line between them, or to the right when they sit on
  // exactly the same spot).
  for (let i = 1; i < pins.length; i++) {
    for (let j = 0; j < i; j++) {
      const dx = pins[i].x - pins[j].x;
      const dy = pins[i].y - pins[j].y;
      const distance = Math.hypot(dx, dy);
      if (distance >= MIN_PIN_GAP) continue;

      const [ux, uy] = distance > 0 ? [dx / distance, dy / distance] : [1, 0];
      pins[i].x = pins[j].x + ux * MIN_PIN_GAP;
      pins[i].y = pins[j].y + uy * MIN_PIN_GAP;
    }
  }

  const clamp = (value: number) => Math.min(Math.max(value, STAGE_PADDING / 2), 100 - STAGE_PADDING / 2);
  const rounded = pins.map((p) => ({
    number: p.number,
    x: Math.round(clamp(p.x) * 100) / 100,
    y: Math.round(clamp(p.y) * 100) / 100,
  }));

  return { pins: rounded, line: rounded.map((p) => `${p.x},${p.y}`).join(" "), missing };
}
