/**
 * The HTTP answer for a failure of the grounded trip-generation pipeline
 * (lib/tripGeneration.ts), shared by every route that runs it - POST
 * /api/trigger-jarvis, POST /api/poll/[id]/finalize and POST /api/trip/create -
 * so the same outage reads the same everywhere and a caller (the plan page,
 * Jarvis) can tell "Google is not set up" from "try again in a minute".
 */

import Anthropic from "@anthropic-ai/sdk";
import { NextResponse } from "next/server";
import { PlacesApiError } from "./places";

/**
 * The response for `error` when it is one the pipeline is known to raise, or
 * null when it is not - the route then logs and answers its own generic 500,
 * since only it knows what it was doing. `routeLabel` ("POST /api/trigger-jarvis")
 * prefixes the server log line.
 *
 *   PlacesApiError            502 { error, reason }  Google's own reason, e.g.
 *                             SERVICE_DISABLED or MISSING_API_KEY
 *   Anthropic.RateLimitError  429                    a quota that resets
 *   Anthropic.APIError        502                    Anthropic itself failed
 */
export function generationFailureResponse(error: unknown, routeLabel: string): NextResponse | null {
  if (error instanceof PlacesApiError) {
    console.error(`${routeLabel}: Places API error (${error.reason}):`, error.message);
    return NextResponse.json(
      { error: `Venue search is unavailable: ${error.message}`, reason: error.reason },
      { status: 502 }
    );
  }
  if (error instanceof Anthropic.RateLimitError) {
    return NextResponse.json(
      { error: "The plan generator is temporarily rate-limited - please try again shortly." },
      { status: 429 }
    );
  }
  if (error instanceof Anthropic.APIError) {
    console.error(`${routeLabel} failed (Anthropic ${error.status}):`, error.message);
    return NextResponse.json(
      { error: "The plan generator is temporarily unavailable. Please try again." },
      { status: 502 }
    );
  }
  return null;
}
