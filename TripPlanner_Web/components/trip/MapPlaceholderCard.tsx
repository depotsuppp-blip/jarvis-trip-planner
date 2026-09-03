import { Map } from "lucide-react";

/**
 * Stand-in for the real interactive map, which needs stop coordinates
 * wired end-to-end (not just the itinerary text this page already has) -
 * tracked as Phase 2. Renders a static grid pattern so the empty state
 * still reads as "a map" rather than a blank card.
 */
export function MapPlaceholderCard() {
  return (
    <div
      className="relative flex h-64 w-full items-center justify-center overflow-hidden rounded-3xl border border-zinc-800 bg-zinc-900/50 backdrop-blur-xl sm:h-80"
      style={{
        backgroundImage:
          "linear-gradient(to right, rgba(255,255,255,0.04) 1px, transparent 1px), linear-gradient(to bottom, rgba(255,255,255,0.04) 1px, transparent 1px)",
        backgroundSize: "28px 28px",
      }}
    >
      <div className="pointer-events-none absolute inset-0 bg-gradient-to-t from-zinc-950/60 via-transparent to-transparent" />
      <div className="relative flex flex-col items-center gap-3 px-6 text-center">
        <div className="flex h-12 w-12 items-center justify-center rounded-2xl border border-zinc-800 bg-zinc-950 text-indigo-300">
          <Map className="h-6 w-6" aria-hidden="true" />
        </div>
        <p className="text-sm font-semibold text-zinc-300">
          Interactive Map (Coming in Phase 2)
        </p>
        <p className="max-w-xs text-xs text-zinc-500">
          Stops and travel routes will be plotted here once map data is wired in.
        </p>
      </div>
    </div>
  );
}
