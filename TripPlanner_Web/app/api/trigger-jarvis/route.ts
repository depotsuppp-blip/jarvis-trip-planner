import { NextRequest, NextResponse } from "next/server";
import { verifyBearerLineToken } from "@/lib/lineAuth";
import { checkRateLimit } from "@/lib/rateLimit";

// Bounded wait for the Jarvis backend to acknowledge the request. Jarvis's
// own trigger (finalize_trip_plan_async in plugins/trip_planner.py)
// deliberately starts a background thread and responds immediately rather
// than waiting for the itinerary itself to finish - so a slow response here
// means the backend is unreachable, not that planning is still running.
const JARVIS_TIMEOUT_MS = 10_000;

// Locking a poll spends real LLM quota (Anthropic/Gemini, see
// finalize_trip_plan_async) and pushes a LINE message - a much costlier
// action per call than casting a vote, so the allowance here is tight.
const TRIGGER_RATE_LIMIT = 3;
const TRIGGER_RATE_WINDOW_MS = 5 * 60_000;

export async function POST(request: NextRequest) {
  // Without this, anyone who obtains a trip's poll link (forwarded into
  // a group chat, so not exactly secret) could call this route directly
  // - no UI needed - and repeatedly burn LLM quota and spam the trip
  // owner's LINE account. This only proves the caller is SOME real LINE
  // user, not that they're the trip's organizer - see the isAdmin TODO
  // in app/trip/poll/[id]/page.tsx for the still-open next step.
  const lineUserId = await verifyBearerLineToken(request.headers.get("authorization"));
  if (!lineUserId) {
    return NextResponse.json(
      { error: "Missing, invalid, or expired LINE ID token." },
      { status: 401 }
    );
  }

  const rateLimit = checkRateLimit(`trigger:${lineUserId}`, TRIGGER_RATE_LIMIT, TRIGGER_RATE_WINDOW_MS);
  if (!rateLimit.allowed) {
    return NextResponse.json(
      { error: "Too many plan-generation requests - please wait a few minutes and try again." },
      { status: 429, headers: { "Retry-After": String(rateLimit.retryAfterSeconds) } }
    );
  }

  const body = await request.json().catch(() => null);
  const tripId = typeof body?.trip_id === "string" ? body.trip_id.trim() : "";

  if (!tripId) {
    return NextResponse.json(
      { error: "trip_id is required." },
      { status: 400 }
    );
  }

  const webhookUrl = process.env.JARVIS_WEBHOOK_URL;
  if (!webhookUrl) {
    return NextResponse.json(
      { error: "JARVIS_WEBHOOK_URL is not configured." },
      { status: 500 }
    );
  }

  try {
    const response = await fetch(webhookUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ trip_id: tripId }),
      signal: AbortSignal.timeout(JARVIS_TIMEOUT_MS),
    });

    if (!response.ok) {
      const details = await response.text().catch(() => "");
      return NextResponse.json(
        { error: `Jarvis backend responded with ${response.status}.`, details },
        { status: 502 }
      );
    }

    const data = await response.json().catch(() => null);
    return NextResponse.json({ ok: true, tripId, data });
  } catch (error) {
    const message =
      error instanceof Error && error.name === "TimeoutError"
        ? "Jarvis backend did not respond in time."
        : "Failed to reach the Jarvis backend.";
    return NextResponse.json({ error: message }, { status: 502 });
  }
}
