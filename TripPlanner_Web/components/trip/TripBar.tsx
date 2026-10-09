import type { ReactNode } from "react";
import { CalendarRange, CloudSun, Users, Wallet, type LucideIcon } from "lucide-react";
import { formatCost, type WeatherSummary } from "@/lib/tripTypes";

// null when the forecast has neither a temperature nor a condition - the bar
// then leaves the item out rather than printing a placeholder.
function formatWeatherLabel(weather: WeatherSummary): string | null {
  const tempText =
    typeof weather.averageTempCelsius === "number" ? `~${Math.round(weather.averageTempCelsius)}°C` : null;
  const parts = [tempText, weather.conditionText || null].filter((p): p is string => Boolean(p));
  return parts.length > 0 ? parts.join(" · ") : null;
}

function BarItem({
  icon: Icon,
  label,
  hideOnPhone = false,
  children,
}: {
  icon: LucideIcon;
  /** What the icon stands for, read out by screen readers ahead of the value. */
  label: string;
  /** Left out below `sm`, where the second row has room for three items, not four. */
  hideOnPhone?: boolean;
  children: ReactNode;
}) {
  return (
    <li className={`flex shrink-0 items-center gap-1.5 ${hideOnPhone ? "max-sm:hidden" : ""}`}>
      <Icon className="size-4 text-slate-400" aria-hidden="true" />
      <span className="sr-only">{label}: </span>
      {children}
    </li>
  );
}

/**
 * The locked plan's summary as one sticky row: destination, then dates,
 * travelers, budget and weather, each an icon and a value. Budget and
 * weather are left out when the plan has none (a plan locked before either
 * existed) - nothing is guessed, and nothing is printed as "unavailable".
 *
 * Its height is the page's `--bar-h` (set on the plan view's root): the day
 * tabs and the map pane stick just below it. Below `lg` the row wraps into
 * two - destination and the List/Map switch above, the details beneath,
 * scrolling sideways if a phone is too narrow for them.
 */
export function TripBar({
  destination,
  dateLabel,
  headcount,
  totalEstimatedCost,
  currency,
  weather,
  tripId,
  trailing,
}: {
  destination: string;
  dateLabel: string;
  headcount: number;
  totalEstimatedCost?: number | null;
  currency?: string;
  weather?: WeatherSummary | null;
  tripId: string;
  /** Controls at the end of the first row, shown below `lg` only (the List/Map switch). */
  trailing?: ReactNode;
}) {
  const weatherLabel = weather ? formatWeatherLabel(weather) : null;

  return (
    <header className="sticky top-0 z-30 h-[var(--bar-h)] border-b border-slate-200 bg-white selection:bg-rose-600 selection:text-white">
      <div className="flex h-full flex-wrap content-center items-center gap-x-6 gap-y-1 px-4 lg:flex-nowrap lg:px-6">
        <div className="flex min-w-0 flex-1 flex-col justify-center lg:flex-none">
          <div className="flex min-w-0 items-center gap-3">
            <h1 className="truncate text-[15px] font-semibold tracking-tight text-slate-900 lg:max-w-72">
              {destination}
            </h1>
            <span className="inline-flex shrink-0 items-center gap-1.5 text-xs font-medium text-slate-600">
              <span aria-hidden="true" className="size-1.5 rounded-full bg-slate-900" />
              Locked
            </span>
          </div>
          {/* Below lg the trip code is a small line under the destination - the row already stands 40px tall for the List|Map switch, so it costs the bar no height. From lg it moves to the end of the detail row instead. */}
          <p className="truncate text-[11px] leading-4 text-slate-500 lg:hidden" title={`Trip code ${tripId}`}>
            Trip <span className="font-mono">{tripId.slice(0, 8).toUpperCase()}</span>
          </p>
        </div>

        <ul className="order-last flex w-full items-center gap-x-5 overflow-x-auto whitespace-nowrap text-[13px] tabular-nums text-slate-700 [scrollbar-width:none] lg:order-none lg:w-auto lg:flex-1 lg:overflow-visible [&::-webkit-scrollbar]:hidden">
          <BarItem icon={CalendarRange} label="Dates">
            {dateLabel}
          </BarItem>
          <BarItem icon={Users} label="Travelers">
            {headcount}
            <span className="text-slate-500 max-sm:sr-only">{headcount === 1 ? "traveler" : "travelers"}</span>
          </BarItem>
          {typeof totalEstimatedCost === "number" && (
            <BarItem icon={Wallet} label="Estimated budget per person">
              ~{formatCost(totalEstimatedCost, currency ?? "")}
              <span className="text-slate-500">/ person</span>
            </BarItem>
          )}
          {weatherLabel && (
            <BarItem icon={CloudSun} label="Weather" hideOnPhone>
              {weatherLabel}
            </BarItem>
          )}
          {/* The first eight characters are enough to tell this trip from another; hover shows the whole code. From lg only - below it the code sits under the destination. */}
          <li
            title={`Trip code ${tripId}`}
            className="ml-auto shrink-0 pl-5 font-mono text-[11px] text-slate-500 max-lg:hidden"
          >
            <span className="sr-only">Trip code: </span>
            {tripId.slice(0, 8).toUpperCase()}
          </li>
        </ul>

        {trailing && <div className="shrink-0 lg:hidden">{trailing}</div>}
      </div>
    </header>
  );
}
