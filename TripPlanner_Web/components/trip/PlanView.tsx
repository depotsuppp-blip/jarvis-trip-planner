"use client";

import { useId, useMemo, useState } from "react";
import { List, Map as MapIcon } from "lucide-react";
import { DayTabs, dayTabId } from "@/components/trip/DayTabs";
import { MapPanel } from "@/components/trip/MapPanel";
import { TimelineList } from "@/components/trip/TimelineList";
import { TripBar } from "@/components/trip/TripBar";
import { groupItineraryByDay } from "@/lib/itineraryDays";
import type { Itinerary } from "@/lib/tripTypes";

type MobileView = "list" | "map";

const switchSegment =
  "flex h-10 items-center gap-1.5 px-3.5 text-xs font-medium outline-none transition-[background-color,color,scale] duration-150 ease-out focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-rose-600 active:scale-[0.97]";

/** The phone's List / Map switch: at `lg` and up both are on screen and this is hidden. */
function ViewSwitch({ view, onChange }: { view: MobileView; onChange: (view: MobileView) => void }) {
  return (
    <div role="group" aria-label="Show" className="flex overflow-hidden rounded-md border border-slate-200">
      <button
        type="button"
        aria-pressed={view === "list"}
        onClick={() => onChange("list")}
        className={`${switchSegment} ${view === "list" ? "bg-slate-900 text-white" : "bg-white text-slate-600 hover:text-slate-900"}`}
      >
        <List className="size-3.5" aria-hidden="true" />
        List
      </button>
      <button
        type="button"
        aria-pressed={view === "map"}
        onClick={() => onChange("map")}
        className={`${switchSegment} ${view === "map" ? "bg-slate-900 text-white" : "bg-white text-slate-600 hover:text-slate-900"}`}
      >
        <MapIcon className="size-3.5" aria-hidden="true" />
        Map
      </button>
    </div>
  );
}

/**
 * The locked plan, shown by app/trip/poll/[id]/page.tsx once a trip has an
 * itinerary. A sticky one-row summary bar over a split screen: the
 * itinerary on the left, one day at a time behind a row of day tabs, and a
 * sticky route map filling the right half. Below `lg` the two share the
 * screen through a List / Map switch instead.
 *
 * This is where the state that ties the halves together lives: which day is
 * selected (the tabs choose it, the list and the map both follow it) and
 * which stop is lit (the list reports it, the map lights that stop's pin).
 * A stop is lit while it is pointed at or focused, and stays lit once its
 * number has been pressed - the "selected" stop, which is how the link
 * between list and map works on a phone, where they are separate views and
 * there is no pointer to hover with. Days with no stops are left out of the
 * grouping entirely, so there is no empty tab or card for them. When the
 * real map arrives it takes the same `activeGroup` and `activeStop` and
 * nothing else here changes.
 *
 * The bar's height is the CSS variable `--bar-h` set on the root: it differs
 * with the bar's layout (two rows below `lg`, one above), and the tab strip
 * and the map pane both stick `var(--bar-h)` from the top to sit under it.
 */
export function PlanView({
  itinerary,
  headcount,
  dateLabel,
  tripId,
  isAdmin,
  adminToken,
  onSwap,
}: {
  itinerary: Itinerary;
  headcount: number;
  dateLabel: string;
  tripId: string;
  isAdmin?: boolean;
  adminToken?: string;
  onSwap?: (itinerary: Itinerary) => void;
}) {
  const idPrefix = useId();
  const groups = useMemo(
    () => groupItineraryByDay(itinerary.days, itinerary.startDate),
    [itinerary.days, itinerary.startDate]
  );

  const [selectedDay, setSelectedDay] = useState<number | null>(null);
  const [hoveredStop, setHoveredStop] = useState<number | null>(null);
  const [selectedStop, setSelectedStop] = useState<number | null>(null);
  const [mobileView, setMobileView] = useState<MobileView>("list");

  // The chosen day, or the first one with stops when nothing (or a day that
  // has since gone away) is chosen.
  const activeGroup = groups.find((group) => group.day === selectedDay) ?? groups[0] ?? null;
  // Pointing at a stop previews it over the selected one; leaving falls back.
  const activeStop = hoveredStop ?? selectedStop;

  function handleDayChange(day: number) {
    setSelectedDay(day);
    setHoveredStop(null);
    setSelectedStop(null);
  }

  return (
    <div className="min-h-svh bg-white text-slate-900 [--bar-h:4.25rem] [--tabs-h:2.8125rem] lg:[--bar-h:3.5rem]">
      <TripBar
        destination={itinerary.destination}
        dateLabel={dateLabel}
        headcount={headcount}
        totalEstimatedCost={itinerary.totalTripEstimatedCost}
        currency={itinerary.currency}
        weather={itinerary.weather}
        tripId={tripId}
        trailing={<ViewSwitch view={mobileView} onChange={setMobileView} />}
      />

      {/* Selected text takes the accent too (the bar does the same for itself) - a browser default belongs to no design. */}
      <div className="selection:bg-rose-600 selection:text-white lg:grid lg:grid-cols-2">
        {/* On a phone's Map view the day tabs stay (so the route can still be changed day by day); only the list beneath them goes. */}
        <main className="min-w-0">
          {activeGroup ? (
            <>
              <div className="sticky top-[var(--bar-h)] z-20 bg-white">
                <div className="mx-auto max-w-xl px-4 sm:px-6 lg:px-8">
                  <DayTabs
                    groups={groups}
                    activeDay={activeGroup.day}
                    onChange={handleDayChange}
                    idPrefix={idPrefix}
                  />
                </div>
              </div>

              <div
                className={`mx-auto max-w-xl px-4 pb-24 pt-8 sm:px-6 lg:px-8 ${mobileView === "map" ? "max-lg:hidden" : ""}`}
              >
                <TimelineList
                  group={activeGroup}
                  currency={itinerary.currency}
                  tripId={tripId}
                  isAdmin={isAdmin}
                  adminToken={adminToken}
                  onSwap={onSwap}
                  activeStop={activeStop}
                  selectedStop={selectedStop}
                  onHoverStop={setHoveredStop}
                  onSelectStop={setSelectedStop}
                  panelId={`${idPrefix}-panel`}
                  tabId={dayTabId(idPrefix, activeGroup.day)}
                />

                {itinerary.notes && (
                  <section className="mt-12 border-t border-slate-200 pt-6">
                    <h3 className="text-sm font-semibold text-slate-900">Notes</h3>
                    <p className="mt-1.5 max-w-prose text-sm leading-6 text-slate-600">{itinerary.notes}</p>
                  </section>
                )}
              </div>
            </>
          ) : (
            <p className="mx-auto max-w-xl px-4 pt-12 text-sm text-slate-500 sm:px-6 lg:px-8">
              This plan has no stops yet.
            </p>
          )}
        </main>

        <aside
          aria-label="Route map"
          className={`h-[calc(100svh-var(--bar-h)-var(--tabs-h))] border-slate-200 lg:sticky lg:top-[var(--bar-h)] lg:h-[calc(100svh-var(--bar-h))] lg:border-l ${
            mobileView === "list" ? "max-lg:hidden" : ""
          }`}
        >
          <MapPanel group={activeGroup} activeStop={activeStop} selectedStop={selectedStop} />
        </aside>
      </div>
    </div>
  );
}
