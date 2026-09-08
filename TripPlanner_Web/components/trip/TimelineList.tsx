"use client";

import { useState } from "react";
import { Bus, Car, Footprints, RefreshCw, UtensilsCrossed, MapPin } from "lucide-react";
import {
  formatCost,
  formatTravelMinutes,
  type Itinerary,
  type ItineraryDay,
  type TravelMode,
} from "@/lib/tripTypes";

// Icon + tooltip label per travel mode - a missing mode (a trip locked
// before dynamic mode selection existed) falls back to the DRIVE
// treatment, since that was every leg's implicit mode back then.
const TRAVEL_MODE_DISPLAY: Record<TravelMode, { Icon: typeof Car; label: string }> = {
  WALK: { Icon: Footprints, label: "Walking" },
  TRANSIT: { Icon: Bus, label: "Public transit" },
  DRIVE: { Icon: Car, label: "Driving" },
};

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
 * The generated itinerary, one stylized card per day, laid out in a
 * responsive grid rather than a single full-width column - stacking every
 * day (however many there are) one per row made the page's bottom half
 * far taller than the top row beside it. Spreading days across 2-3
 * columns on wider screens keeps the whole itinerary closer to a single
 * screenful.
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
  days,
  currency,
  tripId,
  isAdmin,
  adminToken,
  onSwap,
}: {
  days: ItineraryDay[];
  currency?: string;
  tripId?: string;
  isAdmin?: boolean;
  adminToken?: string;
  onSwap?: (itinerary: Itinerary) => void;
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

  return (
    <div className="space-y-3">
      {swapError && <p className="text-sm text-red-600">{swapError}</p>}

      <div className="grid grid-cols-1 gap-6 md:grid-cols-2 xl:grid-cols-3">
        {days.map((day) => (
          <div
            key={day.day}
            className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm"
          >
            <div className="flex items-center gap-3">
              <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-rose-100 text-sm font-bold text-rose-600">
                {day.day}
              </span>
              <p className="text-sm font-semibold text-slate-900">{day.summary}</p>
            </div>

            <ol className="mt-4 space-y-4 border-l border-slate-200 pl-4">
              {day.stops.map((stop, i) => {
                const { Icon: ModeIcon, label: modeLabel } =
                  TRAVEL_MODE_DISPLAY[stop.travelFromPrevious?.mode ?? "DRIVE"];
                const isSwapping = swappingKey === `${day.day}-${i}`;

                return (
                  <li key={i} className="relative">
                    <span className="absolute -left-[21px] top-1 h-2.5 w-2.5 rounded-full border-2 border-white bg-rose-500" />

                    {stop.travelFromPrevious && (
                      <p
                        className="mb-1 flex items-center gap-1.5 text-sm text-slate-500"
                        title={`${modeLabel} - estimated for this future date, based on typical patterns, not live conditions.`}
                      >
                        <ModeIcon className="h-3.5 w-3.5 text-slate-400" aria-hidden="true" />
                        {formatTravelMinutes(stop.travelFromPrevious.durationMinutes)} (estimate)
                      </p>
                    )}

                    <div className="flex items-start justify-between gap-2">
                      <p className="flex items-start gap-1.5 text-sm text-slate-700">
                        {stop.slotType === "meal" ? (
                          <UtensilsCrossed
                            className="mt-0.5 h-4 w-4 shrink-0 text-slate-400"
                            aria-hidden="true"
                          />
                        ) : (
                          <MapPin className="mt-0.5 h-4 w-4 shrink-0 text-slate-400" aria-hidden="true" />
                        )}
                        {stop.text}
                      </p>

                      {canSwap && (
                        <button
                          type="button"
                          onClick={() => handleFindAlternative(day.day, i)}
                          disabled={isSwapping}
                          title="Find a real-time alternative for this stop"
                          className="shrink-0 rounded-full border border-slate-200 p-1.5 text-slate-500 transition hover:border-rose-300 hover:text-rose-600 disabled:opacity-50"
                        >
                          <RefreshCw
                            className={`h-3.5 w-3.5 ${isSwapping ? "animate-spin" : ""}`}
                            aria-hidden="true"
                          />
                          <span className="sr-only">Find alternative</span>
                        </button>
                      )}
                    </div>

                    {typeof stop.estimatedCostPerPerson === "number" && (
                      <p className="mt-1 pl-6 text-xs font-medium text-emerald-600">
                        ~{formatCost(stop.estimatedCostPerPerson, currency ?? "")} / person
                      </p>
                    )}
                  </li>
                );
              })}
            </ol>
          </div>
        ))}
      </div>
    </div>
  );
}
