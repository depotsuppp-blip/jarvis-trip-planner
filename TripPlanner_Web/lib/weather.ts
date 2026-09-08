/**
 * WeatherAPI.com forecast lookup (https://www.weatherapi.com/) - used by
 * app/api/trigger-jarvis/route.ts to attach an average-temperature/
 * condition summary for a locked trip's date window, stored on the
 * itinerary (see lib/itinerary.ts's `weather` field) so the poll page's
 * "Weather" card (components/trip/TripSummary.tsx) has real data instead
 * of "Coming soon". WEATHER_API_KEY is a WeatherAPI.com key - a different
 * product from GOOGLE_MAPS_API_KEY, with its own free tier.
 *
 * HONEST LIMITATION: WeatherAPI's forecast.json always forecasts forward
 * from TODAY, not from an arbitrary future date, and free-tier keys only
 * cover a few days ahead (paid tiers up to 14). This app's trips are
 * often planned weeks out (see FALLBACK_DAYS_FROM_NOW in
 * lib/tripDates.ts), which can be beyond what the API can forecast at
 * all - fetchWeatherSummary requests the maximum days a forecast call
 * supports and simply returns null if none of those days fall inside the
 * trip's window, rather than showing a stale or misleading number. This
 * mirrors lib/routes.ts's computeDayRoute: a real API limitation degrades
 * to "no data shown," never a thrown error or an invented value.
 */

import type { LatLng } from "./places";
import type { WeatherSummary } from "./itinerary";

// WeatherAPI's own documented ceiling for the `days` parameter, regardless
// of plan - a free-tier key just returns fewer days than requested, not an
// error, so it's always safe to ask for the max.
const FORECAST_REQUEST_DAYS = 14;

interface ForecastDay {
  date?: string;
  day?: {
    avgtemp_c?: number;
    condition?: { text?: string; icon?: string };
  };
}

interface ForecastResponse {
  forecast?: { forecastday?: ForecastDay[] };
}

/**
 * Never throws - a missing key, network failure, unusable response, or a
 * trip window outside the forecast horizon all degrade to null. A weather
 * lookup failing must never fail the itinerary generation that already
 * spent real Anthropic + Places quota by this point.
 */
export async function fetchWeatherSummary(
  location: LatLng,
  startDate: string,
  endDate: string
): Promise<WeatherSummary | null> {
  const apiKey = process.env.WEATHER_API_KEY;
  if (!apiKey) {
    console.error("[weather] WEATHER_API_KEY is not configured.");
    return null;
  }

  const url =
    "https://api.weatherapi.com/v1/forecast.json" +
    `?key=${encodeURIComponent(apiKey)}&q=${location.lat},${location.lng}` +
    `&days=${FORECAST_REQUEST_DAYS}&aqi=no&alerts=no`;

  let response: Response;
  try {
    response = await fetch(url);
  } catch (err) {
    console.error(
      `[weather] forecast request failed: ${err instanceof Error ? err.message : String(err)}`
    );
    return null;
  }

  if (!response.ok) {
    const body = await response.text().catch(() => "");
    console.error(`[weather] forecast returned HTTP ${response.status}: ${body.slice(0, 300)}`);
    return null;
  }

  const data: ForecastResponse = await response.json().catch(() => ({}));
  const daysInWindow = (data.forecast?.forecastday ?? []).filter(
    (d): d is ForecastDay & { date: string } =>
      typeof d.date === "string" && d.date >= startDate && d.date <= endDate
  );
  if (daysInWindow.length === 0) {
    return null;
  }

  const temps = daysInWindow
    .map((d) => d.day?.avgtemp_c)
    .filter((t): t is number => typeof t === "number");
  const averageTempCelsius =
    temps.length > 0
      ? Math.round((temps.reduce((sum, t) => sum + t, 0) / temps.length) * 10) / 10
      : null;

  // The middle day of the window is a more representative single
  // condition/icon than the first day alone, for a multi-day trip that
  // might open sunny and close rainy.
  const midDay = daysInWindow[Math.floor(daysInWindow.length / 2)];
  return {
    averageTempCelsius,
    conditionText: midDay.day?.condition?.text ?? "",
    iconCode: midDay.day?.condition?.icon ?? "",
  };
}
