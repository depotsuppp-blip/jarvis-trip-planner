"use client";

import { useId, useLayoutEffect, useMemo, useRef } from "react";
import type { DayGroup } from "@/lib/itineraryDays";
import { sketchRoute, type RouteSketch } from "@/lib/routeSketch";

// One authored moment, played when a day is chosen: the route draws itself,
// then its pins settle in one after another. Strong ease-out, all of it well
// under half a second, skipped entirely under reduced motion.
const DRAW_MS = 320;
const PIN_MS = 200;
const PIN_STAGGER_MS = 40;
const EASE_OUT = "cubic-bezier(0.23, 1, 0.32, 1)";

// The largest square that fits the pane (container query units), less a margin.
const STAGE_SIZE = "w-[min(86cqw,86cqh)] h-[min(86cqw,86cqh)]";

/**
 * One day's route on its square stage. Remounted for every day (the parent
 * keys it by day), so the entrance effect below runs once per chosen day and
 * a plain re-render - a stop being hovered - never replays it.
 */
function RouteStage({
  group,
  sketch,
  activeStop,
  selectedStop,
}: {
  group: DayGroup;
  sketch: RouteSketch;
  activeStop: number | null;
  selectedStop: number | null;
}) {
  const lineRef = useRef<SVGPolylineElement | null>(null);
  const pinRefs = useRef<(HTMLSpanElement | null)[]>([]);

  useLayoutEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

    // pathLength="1" on the line makes a dash of 1 exactly its full length.
    lineRef.current?.animate(
      [
        { strokeDasharray: "1", strokeDashoffset: "1" },
        { strokeDasharray: "1", strokeDashoffset: "0" },
      ],
      { duration: DRAW_MS, easing: EASE_OUT }
    );
    pinRefs.current.forEach((pin, i) => {
      pin?.animate(
        [
          { opacity: 0, scale: "0.9" },
          { opacity: 1, scale: "1" },
        ],
        { duration: PIN_MS, delay: i * PIN_STAGGER_MS, easing: EASE_OUT, fill: "backwards" }
      );
    });
  }, []);

  if (sketch.pins.length === 0) {
    return (
      <p className="max-w-56 text-center text-sm text-slate-500">
        No map coordinates are stored for this day&apos;s stops.
      </p>
    );
  }

  return (
    <div
      role="img"
      aria-label={`Route sketch for day ${group.day}: ${sketch.pins.length} of ${group.stops.length} stops placed`}
      className={`relative ${STAGE_SIZE}`}
    >
      <svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true" className="absolute inset-0 size-full">
        {sketch.pins.length > 1 && (
          <polyline
            ref={lineRef}
            points={sketch.line}
            pathLength={1}
            fill="none"
            stroke="currentColor"
            strokeWidth={0.4}
            strokeLinecap="round"
            strokeLinejoin="round"
            className="text-slate-900"
          />
        )}
      </svg>

      {sketch.pins.map((pin, i) => (
        <span
          key={pin.number}
          ref={(node) => {
            pinRefs.current[i] = node;
          }}
          aria-hidden="true"
          data-active={activeStop === pin.number}
          data-selected={selectedStop === pin.number}
          style={{ left: `${pin.x}%`, top: `${pin.y}%` }}
          className="group/pin absolute flex size-6 -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full border border-slate-900 bg-white transition-[scale] duration-150 ease-out data-[active=true]:z-10 data-[active=true]:scale-110 data-[selected=true]:outline-2 data-[selected=true]:outline-offset-2 data-[selected=true]:outline-rose-600"
        >
          {/* Same lit layer as the timeline's numbered node: fades in, sits under the number; a pressed (selected) stop also keeps a ring. */}
          <span className="absolute -inset-px rounded-full bg-rose-600 opacity-0 transition-opacity duration-150 ease-out group-data-[active=true]/pin:opacity-100" />
          <span className="relative text-[11px] font-medium tabular-nums text-slate-900 transition-colors duration-150 ease-out group-data-[active=true]/pin:text-white">
            {pin.number}
          </span>
        </span>
      ))}
    </div>
  );
}

/**
 * The map pane: a hairline-gridded canvas holding the selected day's route
 * sketch. There is no basemap yet - the sketch plots the coordinates the plan
 * already stores, and the pane says so - but the contract a real map must
 * keep is already here: pins carry the timeline's stop numbers, and the
 * pane follows the selected day and the stop being pointed at.
 *
 * Sizing is the parent's: it fills whatever box it is given (the sticky
 * half of the screen on desktop, the whole screen under the bar on a phone)
 * and measures it as a size container so the stage can be the largest square
 * that fits.
 *
 * "Google Maps" is the text attribution Google's rules ask for wherever
 * Places data (here, the stops' coordinates) is shown without a Google map.
 */
export function MapPanel({
  group,
  activeStop,
  selectedStop,
}: {
  group: DayGroup | null;
  activeStop: number | null;
  selectedStop: number | null;
}) {
  const gridId = useId();
  const sketch = useMemo(() => (group ? sketchRoute(group.stops) : null), [group]);

  // Said plainly: the sketch is not a map, and a stop it could not place is
  // not silently missing from it.
  const caption =
    sketch && sketch.pins.length > 0
      ? [
          "Route sketch · not to scale",
          sketch.missing > 0
            ? `${sketch.missing} ${sketch.missing === 1 ? "stop has" : "stops have"} no coordinates`
            : null,
        ]
          .filter(Boolean)
          .join(" · ")
      : null;

  return (
    <div className="relative size-full overflow-hidden bg-white [container-type:size]">
      <svg aria-hidden="true" className="absolute inset-0 size-full text-slate-900/[0.07]">
        <defs>
          <pattern id={gridId} width="32" height="32" patternUnits="userSpaceOnUse">
            <path d="M32 0H0V32" fill="none" stroke="currentColor" strokeWidth="1" />
          </pattern>
        </defs>
        <rect width="100%" height="100%" fill={`url(#${gridId})`} />
      </svg>

      <div className="absolute inset-0 flex items-center justify-center">
        {group && sketch && (
          <div
            key={group.day}
            className="flex size-full items-center justify-center transition-opacity duration-200 ease-out starting:opacity-0 motion-reduce:transition-none"
          >
            <RouteStage group={group} sketch={sketch} activeStop={activeStop} selectedStop={selectedStop} />
          </div>
        )}
      </div>

      <div className="pointer-events-none absolute inset-x-4 bottom-4 flex items-end justify-between gap-4 text-xs text-slate-500 lg:inset-x-6 lg:bottom-5">
        <p>{caption}</p>
        <p className="shrink-0">Google Maps</p>
      </div>
    </div>
  );
}
