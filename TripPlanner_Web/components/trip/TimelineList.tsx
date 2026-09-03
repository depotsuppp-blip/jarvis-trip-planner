import { Car, UtensilsCrossed, MapPin } from "lucide-react";
import { formatTravelMinutes, type ItineraryDay } from "@/lib/tripTypes";

/**
 * The generated itinerary, one stylized card per day, laid out in a
 * responsive grid rather than a single full-width column - stacking every
 * day (however many there are) one per row made the page's bottom half
 * far taller than the top row beside it. Spreading days across 2-3
 * columns on wider screens keeps the whole itinerary closer to a single
 * screenful.
 */
export function TimelineList({ days }: { days: ItineraryDay[] }) {
  return (
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
            {day.stops.map((stop, i) => (
              <li key={i} className="relative">
                <span className="absolute -left-[21px] top-1 h-2.5 w-2.5 rounded-full border-2 border-white bg-rose-500" />

                {stop.travelFromPrevious && (
                  <p
                    className="mb-1 flex items-center gap-1.5 text-sm text-slate-500"
                    title="Estimated drive time for this future date, based on typical traffic patterns - not live traffic conditions."
                  >
                    <Car className="h-3.5 w-3.5 text-slate-400" aria-hidden="true" />
                    {formatTravelMinutes(stop.travelFromPrevious.durationMinutes)} (estimate)
                  </p>
                )}

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
              </li>
            ))}
          </ol>
        </div>
      ))}
    </div>
  );
}
