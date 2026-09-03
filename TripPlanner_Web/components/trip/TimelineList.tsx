import { Car, UtensilsCrossed, MapPin } from "lucide-react";
import { formatTravelMinutes, type ItineraryDay } from "@/lib/tripTypes";

/**
 * The generated itinerary, one stylized card per day, instead of the
 * single long `<ul>` this replaced - each day now reads as its own unit
 * in the Bento grid rather than a run-on list.
 */
export function TimelineList({ days }: { days: ItineraryDay[] }) {
  return (
    <div className="space-y-4">
      {days.map((day) => (
        <div
          key={day.day}
          className="rounded-3xl border border-zinc-800 bg-zinc-900/50 p-5 backdrop-blur-xl"
        >
          <div className="flex items-center gap-3">
            <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-indigo-500/10 text-sm font-bold text-indigo-300">
              {day.day}
            </span>
            <p className="text-sm font-semibold text-white">{day.summary}</p>
          </div>

          <ol className="mt-4 space-y-4 border-l border-zinc-800 pl-4">
            {day.stops.map((stop, i) => (
              <li key={i} className="relative">
                <span className="absolute -left-[21px] top-1 h-2.5 w-2.5 rounded-full border-2 border-zinc-900 bg-indigo-400" />

                {stop.travelFromPrevious && (
                  <p
                    className="mb-1 flex items-center gap-1.5 text-sm text-zinc-400"
                    title="Estimated drive time for this future date, based on typical traffic patterns - not live traffic conditions."
                  >
                    <Car className="h-3.5 w-3.5 text-zinc-500" aria-hidden="true" />
                    {formatTravelMinutes(stop.travelFromPrevious.durationMinutes)} (estimate)
                  </p>
                )}

                <p className="flex items-start gap-1.5 text-sm text-zinc-200">
                  {stop.slotType === "meal" ? (
                    <UtensilsCrossed
                      className="mt-0.5 h-4 w-4 shrink-0 text-zinc-500"
                      aria-hidden="true"
                    />
                  ) : (
                    <MapPin className="mt-0.5 h-4 w-4 shrink-0 text-zinc-500" aria-hidden="true" />
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
