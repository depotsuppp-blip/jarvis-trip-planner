import { NextRequest, NextResponse } from "next/server";

// Bounded wait for the Jarvis backend to acknowledge the request. Jarvis's
// own trigger (finalize_trip_plan_async in plugins/trip_planner.py)
// deliberately starts a background thread and responds immediately rather
// than waiting for the itinerary itself to finish - so a slow response here
// means the backend is unreachable, not that planning is still running.
const JARVIS_TIMEOUT_MS = 10_000;

export async function POST(request: NextRequest) {
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
