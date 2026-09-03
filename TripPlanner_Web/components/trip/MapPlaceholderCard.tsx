import { Map } from "lucide-react";

/**
 * Stand-in for the real interactive map, which needs stop coordinates
 * wired end-to-end (not just the itinerary text this page already has) -
 * tracked as Phase 2. Renders a static grid pattern so the empty state
 * still reads as "a map" rather than a blank card.
 *
 * Fixed height (not h-full) - this is what sets the top row's height in
 * app/trip/poll/[id]/page.tsx's grid; TripSummary stretches to match via
 * its own h-full.
 */
export function MapPlaceholderCard() {
  return (
    <div
      className="relative flex h-64 w-full items-center justify-center overflow-hidden rounded-3xl border border-slate-200 bg-white shadow-sm sm:h-80"
      style={{
        backgroundImage:
          "linear-gradient(to right, rgba(15,23,42,0.05) 1px, transparent 1px), linear-gradient(to bottom, rgba(15,23,42,0.05) 1px, transparent 1px)",
        backgroundSize: "28px 28px",
      }}
    >
      <div className="relative flex flex-col items-center gap-3 px-6 text-center">
        <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-blue-50 text-blue-600">
          <Map className="h-6 w-6" aria-hidden="true" />
        </div>
        <p className="text-sm font-semibold text-slate-700">
          Interactive Map (Coming in Phase 2)
        </p>
        <p className="max-w-xs text-xs text-slate-400">
          Stops and travel routes will be plotted here once map data is wired in.
        </p>
      </div>
    </div>
  );
}
