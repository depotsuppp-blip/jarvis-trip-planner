"use client";

import dynamic from "next/dynamic";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { MapPinOff } from "lucide-react";
import { MapLoading, MapMessage } from "@/components/trip/MapStates";
import type { DayGroup } from "@/lib/itineraryDays";
import { locateStops } from "@/lib/mapStops";

// Both are read as `process.env.NEXT_PUBLIC_...` written out in full, which is
// the only form Next.js inlines into the browser bundle at build time.
const API_KEY = process.env.NEXT_PUBLIC_GOOGLE_MAPS_API_KEY?.trim() ?? "";
// Advanced markers (the numbered pins) cannot load without a map id. Google's
// DEMO_MAP_ID is for testing; a real one (free, made in the Cloud Console) is
// what a deployed site should carry, and is also where the basemap's style
// is chosen.
const MAP_ID = process.env.NEXT_PUBLIC_GOOGLE_MAP_ID?.trim() || "DEMO_MAP_ID";

const isDev = process.env.NODE_ENV !== "production";

// The map and its library stay out of the bundle of every page that is not
// showing a plan, and of the plan itself until its map pane is on screen.
const RouteMap = dynamic(() => import("@/components/trip/RouteMap"), {
  ssr: false,
  loading: () => <MapLoading />,
});

/**
 * The map pane: Google Maps showing the selected day's stops as numbered pins
 * (the same numbers as the timeline's) joined by a route line, following the
 * selected day and the stop being pointed at or pressed. Pointing at a pin
 * lights its row in the list, and pressing it selects the stop there.
 *
 * Sizing is the parent's: it fills whatever box it is given (the sticky
 * half of the screen on desktop, the whole screen under the bar on a phone).
 * Below `lg` that box is hidden behind the List | Map switch, so the pane
 * watches its own size and only creates the map once it has been seen -
 * Google bills a map when it loads, and most phone visitors never open it.
 *
 * Without a browser key, or in a plan whose stops carry no coordinates, the
 * pane says so instead; the plan itself reads fine without it.
 */
export function MapPanel({
  group,
  groups,
  activeStop,
  selectedStop,
  onHoverStop,
  onSelectStop,
}: {
  group: DayGroup | null;
  /** Every day in the plan: where to look when the chosen day has no placeable stop. */
  groups: DayGroup[];
  activeStop: number | null;
  selectedStop: number | null;
  onHoverStop: (stopNumber: number | null) => void;
  onSelectStop: (stopNumber: number | null) => void;
}) {
  const paneRef = useRef<HTMLDivElement | null>(null);
  const [visible, setVisible] = useState(false);
  const [everVisible, setEverVisible] = useState(false);

  useEffect(() => {
    const pane = paneRef.current;
    if (!pane) return;

    const observer = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect;
      const isVisible = width > 0 && height > 0;
      setVisible(isVisible);
      if (isVisible) setEverVisible(true);
    });
    observer.observe(pane);
    return () => observer.disconnect();
  }, []);

  const day = useMemo(() => (group ? locateStops(group.stops) : null), [group]);
  const planPoints = useMemo(
    () => groups.flatMap((planGroup) => locateStops(planGroup.stops).stops.map((stop) => stop.position)),
    [groups]
  );

  let body: ReactNode = null;
  if (!API_KEY) {
    body = (
      <MapMessage title="Map unavailable">
        {isDev ? (
          <>
            Set <span className="font-mono text-xs">NEXT_PUBLIC_GOOGLE_MAPS_API_KEY</span> in{" "}
            <span className="font-mono text-xs">.env.local</span> and restart the dev server.
          </>
        ) : (
          "The stops are still in the list."
        )}
      </MapMessage>
    );
  } else if (planPoints.length === 0) {
    body = (
      <MapMessage title="No map locations">
        No coordinates are stored for this plan&apos;s stops, so there is nothing to place on a map.
      </MapMessage>
    );
  } else if (everVisible && day && group) {
    body = (
      <RouteMap
        apiKey={API_KEY}
        mapId={MAP_ID}
        day={group.day}
        stops={day.stops}
        fallbackPoints={planPoints}
        visible={visible}
        activeStop={activeStop}
        selectedStop={selectedStop}
        onHoverStop={onHoverStop}
        onSelectStop={onSelectStop}
      />
    );
  }

  // Said plainly: a stop the map could not place is not silently missing from it.
  const notice =
    day && planPoints.length > 0 && day.missing > 0
      ? day.stops.length === 0
        ? "None of this day's stops have a map location"
        : `${day.missing} ${day.missing === 1 ? "stop has" : "stops have"} no map location`
      : null;

  return (
    <div ref={paneRef} className="relative size-full overflow-hidden bg-slate-100">
      {body}

      {notice && (
        <p className="pointer-events-none absolute left-3 top-3 flex items-center gap-1.5 rounded-md bg-white px-2.5 py-1.5 text-xs font-medium text-slate-700 shadow-sm ring-1 ring-slate-200 lg:left-4 lg:top-4">
          <MapPinOff className="size-3.5 text-slate-500" aria-hidden="true" />
          {notice}
        </p>
      )}
    </div>
  );
}
