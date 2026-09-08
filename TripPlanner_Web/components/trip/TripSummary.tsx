import { CalendarRange, CloudSun, MapPin, Users, Wallet } from "lucide-react";
import { formatCost, type WeatherSummary } from "@/lib/tripTypes";

function formatWeatherLabel(weather: WeatherSummary): string {
  const tempText =
    typeof weather.averageTempCelsius === "number" ? `~${Math.round(weather.averageTempCelsius)}°C` : null;
  const parts = [tempText, weather.conditionText || null].filter((p): p is string => Boolean(p));
  return parts.length > 0 ? parts.join(" · ") : "Unavailable";
}

/**
 * Left-rail summary card for the generated-plan Bento layout - the
 * itinerary's destination/dates/headcount/status at a glance, plus the
 * budget and weather mini-cards. Both are real data now (Stage 2's
 * per-stop cost estimates and Stage 2.6's forecast lookup - see
 * app/api/trigger-jarvis/route.ts) and fall back to a plain "Unavailable"
 * label when a trip has neither (an old trip locked before these fields
 * existed, or a trip window too far out for the weather API's forecast
 * horizon - see lib/weather.ts's docstring) rather than a fake "Coming
 * soon" that would never update.
 *
 * `h-full` plus a flex-col body lets this stretch to match
 * MapPlaceholderCard's fixed height in the top row's grid (see
 * app/trip/poll/[id]/page.tsx) rather than leaving empty space beside a
 * taller map - the budget/weather row absorbs the extra height via
 * `flex-1`.
 */
export function TripSummary({
  destination,
  dateLabel,
  headcount,
  totalEstimatedCost,
  currency,
  weather,
}: {
  destination: string;
  dateLabel: string;
  headcount: number;
  totalEstimatedCost?: number | null;
  currency?: string;
  weather?: WeatherSummary | null;
}) {
  return (
    <div className="flex h-full flex-col rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
      <span className="w-fit rounded-full border border-emerald-200 bg-emerald-50 px-3 py-1 text-[11px] font-semibold uppercase tracking-wide text-emerald-700">
        Locked
      </span>

      <div className="mt-4 flex items-start gap-3">
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-blue-50 text-blue-600">
          <MapPin className="h-5 w-5" aria-hidden="true" />
        </div>
        <div className="min-w-0">
          <p className="text-[11px] uppercase tracking-widest text-slate-400">
            Destination
          </p>
          <p className="truncate text-lg font-bold text-slate-900">{destination}</p>
        </div>
      </div>

      <dl className="mt-4 grid grid-cols-2 gap-3 border-t border-slate-100 pt-4">
        <div>
          <dt className="flex items-center gap-1.5 text-[11px] uppercase tracking-widest text-slate-400">
            <CalendarRange className="h-3.5 w-3.5 text-blue-600" aria-hidden="true" />
            Dates
          </dt>
          <dd className="mt-1 text-sm font-semibold text-slate-900">{dateLabel}</dd>
        </div>
        <div>
          <dt className="flex items-center gap-1.5 text-[11px] uppercase tracking-widest text-slate-400">
            <Users className="h-3.5 w-3.5 text-blue-600" aria-hidden="true" />
            Headcount
          </dt>
          <dd className="mt-1 text-sm font-semibold text-slate-900">{headcount}</dd>
        </div>
      </dl>

      <div className="mt-4 grid flex-1 grid-cols-2 gap-3">
        <div className="flex flex-col justify-center rounded-2xl border border-slate-200 bg-slate-50 p-3">
          <Wallet className="h-4 w-4 text-slate-400" aria-hidden="true" />
          <p className="mt-2 text-xs font-medium text-slate-500">Est. Budget / person</p>
          <p
            className={`mt-0.5 text-sm font-semibold ${
              typeof totalEstimatedCost === "number" ? "text-slate-900" : "text-slate-400"
            }`}
          >
            {typeof totalEstimatedCost === "number"
              ? formatCost(totalEstimatedCost, currency ?? "")
              : "Unavailable"}
          </p>
        </div>
        <div className="flex flex-col justify-center rounded-2xl border border-slate-200 bg-slate-50 p-3">
          <CloudSun className="h-4 w-4 text-slate-400" aria-hidden="true" />
          <p className="mt-2 text-xs font-medium text-slate-500">Weather</p>
          <p
            className={`mt-0.5 text-sm font-semibold ${weather ? "text-slate-900" : "text-slate-400"}`}
          >
            {weather ? formatWeatherLabel(weather) : "Unavailable"}
          </p>
        </div>
      </div>
    </div>
  );
}
