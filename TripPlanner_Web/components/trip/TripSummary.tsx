import { CalendarRange, CloudSun, MapPin, Users, Wallet } from "lucide-react";

/**
 * Left-rail summary card for the generated-plan Bento layout - the
 * itinerary's destination/dates/headcount/status at a glance, plus
 * placeholder mini-cards for widgets that don't exist yet (budget,
 * weather). Those two are intentionally inert: no props, no data wiring,
 * just the visual slot for Phase 2 to fill in.
 */
export function TripSummary({
  destination,
  dateLabel,
  headcount,
}: {
  destination: string;
  dateLabel: string;
  headcount: number;
}) {
  return (
    <div className="space-y-4">
      <div className="rounded-3xl border border-zinc-800 bg-zinc-900/50 p-5 backdrop-blur-xl">
        <div className="flex items-center justify-between">
          <span className="inline-flex items-center gap-1.5 rounded-full border border-emerald-500/20 bg-emerald-500/10 px-3 py-1 text-[11px] font-semibold uppercase tracking-wide text-emerald-400/90">
            Locked
          </span>
        </div>

        <div className="mt-4 flex items-start gap-3">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl border border-zinc-800 bg-zinc-950 text-indigo-300">
            <MapPin className="h-5 w-5" aria-hidden="true" />
          </div>
          <div className="min-w-0">
            <p className="text-[11px] uppercase tracking-widest text-zinc-500">
              Destination
            </p>
            <p className="truncate text-lg font-bold text-white">{destination}</p>
          </div>
        </div>

        <dl className="mt-5 space-y-3 border-t border-zinc-800 pt-4">
          <div className="flex items-center justify-between gap-3">
            <dt className="flex items-center gap-2 text-sm text-zinc-400">
              <CalendarRange className="h-4 w-4 text-zinc-500" aria-hidden="true" />
              Dates
            </dt>
            <dd className="text-sm font-medium text-white">{dateLabel}</dd>
          </div>
          <div className="flex items-center justify-between gap-3">
            <dt className="flex items-center gap-2 text-sm text-zinc-400">
              <Users className="h-4 w-4 text-zinc-500" aria-hidden="true" />
              Headcount
            </dt>
            <dd className="text-sm font-medium text-white">{headcount}</dd>
          </div>
        </dl>
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div className="rounded-2xl border border-zinc-800 bg-zinc-900/50 p-4 backdrop-blur-xl">
          <Wallet className="h-4 w-4 text-zinc-500" aria-hidden="true" />
          <p className="mt-2 text-xs font-medium text-zinc-400">Est. Budget</p>
          <p className="mt-0.5 text-sm font-semibold text-zinc-600">Coming soon</p>
        </div>
        <div className="rounded-2xl border border-zinc-800 bg-zinc-900/50 p-4 backdrop-blur-xl">
          <CloudSun className="h-4 w-4 text-zinc-500" aria-hidden="true" />
          <p className="mt-2 text-xs font-medium text-zinc-400">Weather</p>
          <p className="mt-0.5 text-sm font-semibold text-zinc-600">Coming soon</p>
        </div>
      </div>
    </div>
  );
}
