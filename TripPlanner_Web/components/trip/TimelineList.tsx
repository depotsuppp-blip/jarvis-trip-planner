"use client";

import { useState } from "react";
import { Bus, Car, Footprints, RefreshCw, UtensilsCrossed } from "lucide-react";
import type { DayGroup } from "@/lib/itineraryDays";
import { formatDayDate } from "@/lib/itineraryDays";
import { formatCost, formatTravelMinutes, type Itinerary, type TravelMode } from "@/lib/tripTypes";

// Icon, tooltip label and the word that follows the minutes ("14 min walk")
// per travel mode - a missing mode (a trip locked before dynamic mode
// selection existed) falls back to the DRIVE treatment, since that was every
// leg's implicit mode back then.
const TRAVEL_MODE_DISPLAY: Record<TravelMode, { Icon: typeof Car; label: string; verb: string }> = {
  WALK: { Icon: Footprints, label: "Walking", verb: "walk" },
  TRANSIT: { Icon: Bus, label: "Public transit", verb: "by transit" },
  DRIVE: { Icon: Car, label: "Driving", verb: "drive" },
};

// A stop's own facts (meal, cost) are small tracked caps in Muted grey; the
// travel leg between stops is plain sentence case ("~14 min walk") so the two
// never read as one line - a cost is about the stop above it, a leg is the
// way to the stop below.
const metaClass = "text-[11px] font-medium uppercase tracking-[0.08em] tabular-nums text-slate-500";
const legClass = "text-xs tabular-nums text-slate-500";

function getCurrentPosition(): Promise<GeolocationPosition> {
  return new Promise((resolve, reject) => {
    if (!navigator.geolocation) {
      reject(new Error("This browser can't share your location, so no alternative can be found."));
      return;
    }
    navigator.geolocation.getCurrentPosition(resolve, reject, { timeout: 10_000 });
  });
}

/**
 * One day of the itinerary: its "Day N" heading (plus the day's short title
 * when the plan has one), a single meta line, and the stops as a numbered
 * timeline. The stop numbers are the numbers on the map's pins
 * (components/trip/MapPanel.tsx). A stop's number is a button: pointing at a
 * stop, or focusing its number, lights that pin for as long as it lasts
 * (`onHoverStop`), and pressing the number selects the stop (`onSelectStop`)
 * so its pin stays lit - which is also how someone on a phone, or on the
 * keyboard, finds a stop on the map. Pressing it again, or Escape, clears it.
 *
 * A stop is its venue's name in bold with ONE brief caption under it
 * (`placeName` / `shortDescription`, at most 15 words - see lib/stopCopy.ts).
 * A plan stored before those fields existed, and a stop with no real venue,
 * show what they have: the whole `text` sentence, or the caption alone.
 *
 * Travel time sits between stops, on the connector that leads INTO the stop
 * it belongs to (it is measured from the previous stop). Cost sits under its
 * stop. Both are small grey estimates - "~" marks them, and one footnote
 * says so rather than every line repeating "(estimate)" and "/ person".
 *
 * The per-stop "Find Alternative" control is admin-only, gated by the
 * SAME `isAdmin`/`adminToken` the poll page already derives from the
 * ?admin=<token> URL param for "Lock & Generate Plan" - see
 * app/trip/poll/[id]/page.tsx. This is UX only: POST
 * /api/trip/alternative independently re-verifies the token server-side
 * before doing anything (see that route's docstring), so a caller
 * without one gets a plain 403 regardless of what this component
 * renders. A non-admin viewer never sees the control at all - the
 * itinerary is strictly read-only for them.
 */
export function TimelineList({
  group,
  currency,
  tripId,
  isAdmin,
  adminToken,
  onSwap,
  activeStop,
  selectedStop,
  onHoverStop,
  onSelectStop,
  panelId,
  tabId,
}: {
  group: DayGroup;
  currency?: string;
  tripId?: string;
  isAdmin?: boolean;
  adminToken?: string;
  onSwap?: (itinerary: Itinerary) => void;
  /** Number of the stop shown lit - the one pointed at or focused, else the selected one. */
  activeStop: number | null;
  /** Number of the stop picked by pressing its number, if any. */
  selectedStop: number | null;
  onHoverStop: (stopNumber: number | null) => void;
  onSelectStop: (stopNumber: number | null) => void;
  /** The tabpanel's id, which DayTabs' tabs point at with aria-controls. */
  panelId: string;
  /** The id of the selected day's tab, which labels this panel. */
  tabId: string;
}) {
  const [swappingKey, setSwappingKey] = useState<string | null>(null);
  const [swapError, setSwapError] = useState("");

  const canSwap = Boolean(isAdmin && tripId && adminToken);

  async function handleFindAlternative(dayNumber: number, stopIndex: number) {
    if (!tripId || !adminToken) return;
    setSwapError("");

    const placeType = window.prompt(
      "What should replace this stop? A category (e.g. cafe, restaurant, museum) or a specific place " +
        "name both work, in any language."
    );
    if (!placeType || !placeType.trim()) return;

    const minutesInput = window.prompt("How many minutes are available for this stop?", "60");
    if (minutesInput === null) return;
    const availableMinutes = Number(minutesInput);
    if (!Number.isFinite(availableMinutes) || availableMinutes <= 0) {
      setSwapError("Enter a valid number of minutes to find an alternative.");
      return;
    }

    const key = `${dayNumber}-${stopIndex}`;
    setSwappingKey(key);
    try {
      const position = await getCurrentPosition();
      const response = await fetch("/api/trip/alternative", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          trip_id: tripId,
          admin_token: adminToken,
          dayNumber,
          stopIndex,
          lat: position.coords.latitude,
          lng: position.coords.longitude,
          availableMinutes,
          placeType: placeType.trim(),
        }),
      });

      const data = await response.json().catch(() => null);
      if (!response.ok) {
        throw new Error(typeof data?.error === "string" ? data.error : "Couldn't find an alternative.");
      }
      if (data?.itinerary) {
        onSwap?.(data.itinerary as Itinerary);
      }
    } catch (error) {
      setSwapError(
        error instanceof Error && error.message
          ? error.message
          : "Couldn't find an alternative. Please try again."
      );
    } finally {
      setSwappingKey(null);
    }
  }

  const stopCount = group.stops.length;
  const meta = [
    group.date ? formatDayDate(group.date) : null,
    `${stopCount} ${stopCount === 1 ? "stop" : "stops"}`,
    group.estimatedCost !== null ? `~${formatCost(group.estimatedCost, currency ?? "")}` : null,
  ]
    .filter(Boolean)
    .join(" · ");

  const hasEstimates = group.stops.some(
    ({ stop }) => stop.travelFromPrevious || typeof stop.estimatedCostPerPerson === "number"
  );
  const hasCosts = group.stops.some(({ stop }) => typeof stop.estimatedCostPerPerson === "number");

  return (
    <div id={panelId} role="tabpanel" aria-labelledby={tabId}>
      {/* Keyed by day: a new day mounts fresh and fades in (opacity only, 150ms). */}
      <div
        key={group.day}
        className="transition-opacity duration-150 ease-out starting:opacity-0 motion-reduce:transition-none"
      >
        <h2 className="text-balance text-[22px] font-semibold leading-tight tracking-[-0.02em] text-slate-900">
          Day {group.day}
          {group.title && <span className="font-normal text-slate-500">: {group.title}</span>}
        </h2>
        <p className="mt-1.5 text-[13px] tabular-nums text-slate-500">{meta}</p>

        {swapError && (
          <p role="alert" className="mt-4 text-sm text-red-700">
            {swapError}
          </p>
        )}

        <ol
          className="mt-8"
          onKeyDown={(event) => {
            if (event.key === "Escape" && selectedStop !== null) onSelectStop(null);
          }}
        >
          {group.stops.map(({ stop, index, number }, position) => {
            const isFirst = position === 0;
            const isLast = position === stopCount - 1;
            const {
              Icon: ModeIcon,
              label: modeLabel,
              verb: modeVerb,
            } = TRAVEL_MODE_DISPLAY[stop.travelFromPrevious?.mode ?? "DRIVE"];
            const isSwapping = swappingKey === `${group.day}-${index}`;
            const isMeal = stop.slotType === "meal";
            const costLabel =
              typeof stop.estimatedCostPerPerson === "number"
                ? `~${formatCost(stop.estimatedCostPerPerson, currency ?? "")}`
                : null;

            return (
              <li
                key={index}
                data-day={group.day}
                data-stop-index={index}
                data-active={activeStop === number}
                data-selected={selectedStop === number}
                onPointerEnter={() => onHoverStop(number)}
                onPointerLeave={() => onHoverStop(null)}
                onFocus={() => onHoverStop(number)}
                onBlur={() => onHoverStop(null)}
                className="group/stop grid grid-cols-[1.5rem_1fr] gap-x-3"
              >
                {/* The connector leading into this stop: the spine, with the travel mode sitting on it and the time beside it. */}
                {!isFirst && (
                  <>
                    <div aria-hidden="true" className="relative">
                      <span className="absolute inset-y-0 left-1/2 w-px -translate-x-1/2 bg-slate-200" />
                      {stop.travelFromPrevious && (
                        <span className="absolute left-1/2 top-1/2 flex size-6 -translate-x-1/2 -translate-y-1/2 items-center justify-center bg-white">
                          <ModeIcon className="size-3.5 text-slate-400" />
                        </span>
                      )}
                    </div>
                    <div className={`flex items-center ${stop.travelFromPrevious ? "min-h-9" : "min-h-6"}`}>
                      {stop.travelFromPrevious && (
                        <p
                          className={legClass}
                          title={`${modeLabel} - estimated for this future date, based on typical patterns, not live conditions.`}
                        >
                          {formatTravelMinutes(stop.travelFromPrevious.durationMinutes)} {modeVerb}
                        </p>
                      )}
                    </div>
                  </>
                )}

                <div className="relative">
                  {/* The number is the control: 40px to press (it takes 24px of room). Pointing at it lights the fill; pressing it adds a ring that stays (the keyboard focus ring looks the same, and goes when focus does). The fill is its own layer that fades in (opacity only), over the border, under the number. */}
                  <button
                    type="button"
                    aria-pressed={selectedStop === number}
                    aria-label={`Show stop ${number} on the map`}
                    onClick={() => onSelectStop(selectedStop === number ? null : number)}
                    className="group/node relative z-10 -m-2 flex size-10 items-center justify-center rounded-full outline-none transition-[scale] duration-150 ease-out active:scale-[0.95]"
                  >
                    <span className="relative flex size-6 items-center justify-center rounded-full border border-slate-900 bg-white group-focus-visible/node:outline-2 group-focus-visible/node:outline-offset-2 group-focus-visible/node:outline-rose-600 group-data-[selected=true]/stop:outline-2 group-data-[selected=true]/stop:outline-offset-2 group-data-[selected=true]/stop:outline-rose-600">
                      <span className="absolute -inset-px rounded-full bg-rose-600 opacity-0 transition-opacity duration-150 ease-out group-data-[active=true]/stop:opacity-100" />
                      <span className="relative text-[11px] font-medium tabular-nums text-slate-900 transition-colors duration-150 ease-out group-data-[active=true]/stop:text-white">
                        {number}
                      </span>
                    </span>
                  </button>
                  {!isLast && (
                    <span
                      aria-hidden="true"
                      className="absolute bottom-0 left-1/2 top-6 w-px -translate-x-1/2 bg-slate-200"
                    />
                  )}
                </div>

                <div className="min-w-0">
                  <div className="flex items-start justify-between gap-3">
                    {stop.placeName || stop.shortDescription ? (
                      <div className="min-w-0">
                        {stop.placeName && (
                          <p className="text-pretty text-[15px] font-semibold leading-6 text-slate-900">
                            {stop.placeName}
                          </p>
                        )}
                        {stop.shortDescription && (
                          <p
                            className={`text-pretty ${
                              stop.placeName
                                ? "text-sm leading-5 text-slate-600"
                                : // No venue behind this stop: a plain statement in Muted, so it reads as a flagged state, not as content.
                                  "text-[15px] leading-6 text-slate-500"
                            }`}
                          >
                            {stop.shortDescription}
                          </p>
                        )}
                      </div>
                    ) : (
                      <p className="text-pretty text-[15px] leading-6 text-slate-700">{stop.text}</p>
                    )}

                    {canSwap && (
                      <button
                        type="button"
                        onClick={() => handleFindAlternative(group.day, index)}
                        disabled={isSwapping}
                        title="Find a real-time alternative for this stop"
                        className="-mr-2 -mt-2 flex size-10 shrink-0 items-center justify-center rounded-md border border-transparent text-slate-500 outline-none transition-[border-color,color,scale] duration-150 ease-out hover:border-slate-900 hover:text-slate-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-rose-600 active:scale-[0.97] disabled:opacity-60"
                      >
                        <RefreshCw
                          className={`size-3.5 ${isSwapping ? "animate-spin text-rose-600" : ""}`}
                          aria-hidden="true"
                        />
                        <span className="sr-only">Find alternative</span>
                      </button>
                    )}
                  </div>

                  {(isMeal || costLabel) && (
                    <p className={`mt-1.5 flex items-center gap-1.5 ${metaClass}`}>
                      {isMeal && (
                        <>
                          <UtensilsCrossed className="size-3.5 text-slate-400" aria-hidden="true" />
                          Meal
                        </>
                      )}
                      {isMeal && costLabel && <span aria-hidden="true">·</span>}
                      {costLabel && (
                        <span title="Estimated cost per person">
                          <span className="sr-only">Estimated cost per person: </span>
                          {costLabel}
                        </span>
                      )}
                    </p>
                  )}
                </div>
              </li>
            );
          })}
        </ol>

        {hasEstimates && (
          <p className="mt-8 border-t border-slate-200 pt-4 text-xs text-slate-500">
            ~ marks an estimate.{hasCosts ? " Costs are per person." : ""}
          </p>
        )}
      </div>
    </div>
  );
}
