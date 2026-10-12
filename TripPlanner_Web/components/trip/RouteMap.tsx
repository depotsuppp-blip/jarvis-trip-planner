"use client";

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  AdvancedMarker,
  APILoadingStatus,
  APIProvider,
  // Aliased: this file never wants to shadow the built-in Map.
  Map as GoogleMap,
  useApiLoadingStatus,
  useMap,
  useMapsLibrary,
} from "@vis.gl/react-google-maps";
import { MapLoading, MapMessage } from "@/components/trip/MapStates";
import { boundsOf, type BoundsLiteral, type MapStop } from "@/lib/mapStops";
import type { LatLng } from "@/lib/tripTypes";

declare global {
  interface Window {
    /** Google's documented hook, called once when it rejects the key (wrong referrer, API not enabled, billing off...). */
    gm_authFailure?: () => void;
  }
}

// The brand accent (Tailwind rose-600) as the hex the Maps API wants.
const BRAND = "#EC003F";

// One authored moment, played when a day is chosen: the camera moves to the
// day, the route draws itself, then its pins settle in one after another.
// Strong ease-out, all of it well under half a second, skipped entirely under
// reduced motion.
const DRAW_MS = 320;
const PIN_MS = 200;
const PIN_STAGGER_MS = 40;
const EASE_OUT = "cubic-bezier(0.23, 1, 0.32, 1)";

// Space kept clear between the fitted stops and the pane's edge: a pin is about
// 32px across (more with its halo and name chip), and the zoom buttons and
// attribution sit in the corners.
const FIT_PADDING = 72;
const FIT_PADDING_PHONE = 40;
const PHONE_WIDTH = 640;
// How close to the pane's edge a pin's name chip may come.
const CHIP_MARGIN = 8;
// A map click this soon after a pin press is the press itself, not a click on
// the bare map.
const PIN_CLICK_ECHO_MS = 150;

const isDev = process.env.NODE_ENV !== "production";

/**
 * A stop's number as a pin. The same disc as the node on the timeline (solid
 * accent, white number), with a white edge and a shadow so it holds on any
 * basemap. Pointed at or pressed, its list row is lit, and so is this: it
 * grows a halo (pressed: a stronger one) and, for as long as it is lit, names
 * the stop in a chip above it - which is how a stop is identified on a phone's
 * Map view, where no list is on screen.
 */
function Pin({ stop, order, lit }: { stop: MapStop; order: number; lit: "none" | "active" | "selected" }) {
  const map = useMap();
  const ref = useRef<HTMLDivElement | null>(null);
  const chipRef = useRef<HTMLSpanElement | null>(null);

  // Settles in when its day is chosen: the parent keys pins by day, so this
  // runs once per day and never on a hover.
  useLayoutEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    ref.current?.animate(
      [
        { opacity: 0, scale: "0.8" },
        { opacity: 1, scale: "1" },
      ],
      { duration: PIN_MS, delay: order * PIN_STAGGER_MS, easing: EASE_OUT, fill: "backwards" }
    );
  }, [order]);

  // A pin near the pane's edge would have its name cut off by it: slide the
  // chip back inside (measured once each time the pin is lit).
  useLayoutEffect(() => {
    const chip = chipRef.current;
    const pane = map?.getDiv().getBoundingClientRect();
    if (!chip || !pane) return;
    chip.style.setProperty("--chip-shift", "0px");
    const { left, right } = chip.getBoundingClientRect();
    const shift = Math.max(0, pane.left + CHIP_MARGIN - left) - Math.max(0, right - (pane.right - CHIP_MARGIN));
    chip.style.setProperty("--chip-shift", `${shift}px`);
  }, [map, lit]);

  // The chip sits beside the disc, not inside it, so it keeps its size when
  // the disc grows and can be measured against the pane's edge.
  return (
    <div className="relative size-7">
      <div
        ref={ref}
        data-lit={lit}
        className="absolute inset-0 flex items-center justify-center rounded-full bg-rose-600 text-xs font-bold tabular-nums text-white shadow-[0_0_0_2px_#fff,0_2px_6px_rgb(15_23_43/0.35)] transition-[scale,box-shadow] duration-150 ease-out data-[lit=active]:scale-[1.15] data-[lit=active]:shadow-[0_0_0_2px_#fff,0_0_0_8px_rgb(236_0_63/0.2),0_2px_6px_rgb(15_23_43/0.35)] data-[lit=selected]:scale-[1.15] data-[lit=selected]:shadow-[0_0_0_2px_#fff,0_0_0_8px_rgb(236_0_63/0.38),0_2px_6px_rgb(15_23_43/0.35)]"
      >
        {stop.number}
      </div>
      {lit !== "none" && stop.name && (
        <span
          ref={chipRef}
          className="pointer-events-none absolute bottom-[calc(100%+14px)] left-1/2 max-w-56 translate-x-[calc(-50%+var(--chip-shift,0px))] truncate rounded-md bg-slate-900 px-2.5 py-1.5 text-xs font-semibold text-white shadow-lg"
        >
          {stop.name}
        </span>
      )}
    </div>
  );
}

/** Where `fraction` (0-1) of the path's length has got to, as a shorter path. */
function pathUpTo(path: LatLng[], fraction: number): LatLng[] {
  // Planar distances are plenty: this only paces a 320ms draw-on.
  const cosLat = Math.cos((path[0].lat * Math.PI) / 180);
  const lengthOf = (a: LatLng, b: LatLng) => Math.hypot(b.lat - a.lat, (b.lng - a.lng) * cosLat);

  let total = 0;
  for (let i = 1; i < path.length; i++) total += lengthOf(path[i - 1], path[i]);

  let remaining = total * fraction;
  const drawn = [path[0]];
  for (let i = 1; i < path.length; i++) {
    const from = path[i - 1];
    const to = path[i];
    const length = lengthOf(from, to);
    if (remaining >= length) {
      drawn.push(to);
      remaining -= length;
      continue;
    }
    const t = length > 0 ? remaining / length : 0;
    drawn.push({ lat: from.lat + (to.lat - from.lat) * t, lng: from.lng + (to.lng - from.lng) * t });
    break;
  }
  return drawn;
}

/**
 * The day's route: the stops joined in order by one accent line over a white
 * casing (so it reads on parks, water and roads alike), drawn on over
 * DRAW_MS whenever the day changes. Plain straight segments between stops -
 * the travel time between them is on the timeline.
 */
function RouteLine({ path }: { path: LatLng[] }) {
  const map = useMap();
  const mapsLibrary = useMapsLibrary("maps");

  useEffect(() => {
    if (!map || !mapsLibrary || path.length < 2) return;

    const casing = new mapsLibrary.Polyline({
      map,
      clickable: false,
      zIndex: 1,
      strokeColor: "#ffffff",
      strokeOpacity: 0.95,
      strokeWeight: 8,
    });
    const line = new mapsLibrary.Polyline({
      map,
      clickable: false,
      zIndex: 2,
      strokeColor: BRAND,
      strokeOpacity: 1,
      strokeWeight: 4,
    });
    const show = (shown: LatLng[]) => {
      casing.setPath(shown);
      line.setPath(shown);
    };

    let frame = 0;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      show(path);
    } else {
      const start = performance.now();
      const draw = (now: number) => {
        const progress = Math.min(1, (now - start) / DRAW_MS);
        show(pathUpTo(path, 1 - (1 - progress) ** 4));
        if (progress < 1) frame = requestAnimationFrame(draw);
      };
      frame = requestAnimationFrame(draw);
    }

    return () => {
      cancelAnimationFrame(frame);
      casing.setMap(null);
      line.setMap(null);
    };
  }, [map, mapsLibrary, path]);

  return null;
}

/** Moves the camera to `bounds` now and whenever they change (a new day) or the pane comes back into view. */
function FitTo({ bounds, visible }: { bounds: BoundsLiteral; visible: boolean }) {
  const map = useMap();

  useEffect(() => {
    if (!map || !visible) return;
    // A frame late: a pane that has only just been shown has not been laid
    // out when this runs, and a fit measured against no size lands on the
    // wrong zoom.
    const frame = requestAnimationFrame(() => {
      map.fitBounds(bounds, map.getDiv().clientWidth < PHONE_WIDTH ? FIT_PADDING_PHONE : FIT_PADDING);
    });
    return () => cancelAnimationFrame(frame);
  }, [map, visible, bounds]);

  return null;
}

/** Brings the pressed stop's pin into view when the camera has been moved away from it. */
function RevealSelected({ stops, selectedStop }: { stops: MapStop[]; selectedStop: number | null }) {
  const map = useMap();

  useEffect(() => {
    if (!map || selectedStop === null) return;
    const stop = stops.find((candidate) => candidate.number === selectedStop);
    if (stop && !map.getBounds()?.contains(stop.position)) map.panTo(stop.position);
  }, [map, stops, selectedStop]);

  return null;
}

function Stage({
  mapId,
  day,
  stops,
  fallbackPoints,
  visible,
  activeStop,
  selectedStop,
  onHoverStop,
  onSelectStop,
}: Omit<RouteMapProps, "apiKey">) {
  const status = useApiLoadingStatus();
  // When a pin is pressed. Whether that press also reaches the map as a click
  // is Google's to decide (and has differed between releases and between
  // mouse and touch), so a map click that arrives right behind a pin press
  // is that same press, and must not undo the selection it just made.
  const pinPressedAt = useRef(0);

  const path = useMemo(() => stops.map((stop) => stop.position), [stops]);
  // A day with no placeable stop still gets a map: of the plan's other stops.
  const bounds = useMemo(() => boundsOf(path.length > 0 ? path : fallbackPoints), [path, fallbackPoints]);

  if (status === APILoadingStatus.FAILED) {
    return (
      <MapMessage title="The map couldn't be loaded">
        Check your connection and reload. The stops are still in the list.
      </MapMessage>
    );
  }
  if (!bounds) return null;

  return (
    <>
      {status !== APILoadingStatus.LOADED && (
        <div className="absolute inset-0">
          <MapLoading />
        </div>
      )}

      <GoogleMap
        className="absolute inset-0"
        mapId={mapId}
        defaultBounds={{ ...bounds, padding: FIT_PADDING }}
        gestureHandling="greedy"
        disableDefaultUI
        zoomControl
        clickableIcons={false}
        onClick={() => {
          if (performance.now() - pinPressedAt.current > PIN_CLICK_ECHO_MS) onSelectStop(null);
        }}
      >
        <FitTo bounds={bounds} visible={visible} />
        <RevealSelected stops={stops} selectedStop={selectedStop} />
        <RouteLine path={path} />

        {stops.map((stop, order) => {
          const selected = selectedStop === stop.number;
          const lit = selected ? "selected" : activeStop === stop.number ? "active" : "none";
          return (
            <AdvancedMarker
              key={`${day}-${stop.number}`}
              position={stop.position}
              title={stop.name ? `${stop.number}. ${stop.name}` : `Stop ${stop.number}`}
              zIndex={lit === "none" ? 10 : 20}
              anchorLeft="-50%"
              anchorTop="-50%"
              onClick={() => {
                pinPressedAt.current = performance.now();
                onSelectStop(selected ? null : stop.number);
              }}
              onMouseEnter={() => onHoverStop(stop.number)}
              onMouseLeave={() => onHoverStop(null)}
            >
              <Pin stop={stop} order={order} lit={lit} />
            </AdvancedMarker>
          );
        })}
      </GoogleMap>
    </>
  );
}

interface RouteMapProps {
  apiKey: string;
  mapId: string;
  /** The chosen day; pins are keyed by it so each day's pins settle in afresh. */
  day: number;
  /** The day's stops that have coordinates, numbered as on the timeline. */
  stops: MapStop[];
  /** Where to look when the day has no placeable stop: every placeable stop in the plan. */
  fallbackPoints: LatLng[];
  /** False while the pane is hidden (a phone's List view): a hidden map can't be fitted. */
  visible: boolean;
  activeStop: number | null;
  selectedStop: number | null;
  onHoverStop: (stopNumber: number | null) => void;
  onSelectStop: (stopNumber: number | null) => void;
}

/**
 * The Google map behind the plan view's right half (see MapPanel, which loads
 * this on demand, only where there is a key, something to show and a pane
 * that is on screen). One map for the whole plan: choosing another day moves
 * its camera and swaps its pins and route, it does not make a new map.
 *
 * Needs a Map ID because advanced markers - the numbered pins - are not
 * available without one.
 */
export default function RouteMap(props: RouteMapProps) {
  const [authFailed, setAuthFailed] = useState(false);

  // Google reports a rejected key (wrong referrer, API not enabled, billing
  // off) only through this global, and answers with its own error overlay.
  useEffect(() => {
    const previous = window.gm_authFailure;
    window.gm_authFailure = () => {
      setAuthFailed(true);
      previous?.();
    };
    return () => {
      window.gm_authFailure = previous;
    };
  }, []);

  if (authFailed) {
    return (
      <MapMessage title="The map couldn't be loaded">
        {isDev
          ? "Google rejected the key. Check that the Maps JavaScript API is enabled, that billing is on, and that the key's website restrictions allow this address."
          : "The stops are still in the list."}
      </MapMessage>
    );
  }

  return (
    <APIProvider apiKey={props.apiKey}>
      <Stage {...props} />
    </APIProvider>
  );
}
