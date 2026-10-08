import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import { getSessionUserId } from "@/lib/auth";
import { prisma } from "@/lib/prisma";
import { checkRateLimit } from "@/lib/rateLimit";

// A person can't realistically swipe faster than a couple of cards a
// second; this just stops a runaway client loop from hammering the
// database, without ever getting in the way of real use.
const SWIPE_RATE_LIMIT = 200;
const SWIPE_RATE_WINDOW_MS = 60_000;

// Trip ids are uuid4 strings (plugins/trip_planner.py) and Google place ids
// are URL-safe base64-ish - both just bare correlator strings in the
// schema (see UserSwipeAction in prisma/schema.prisma), so this only keeps
// obvious junk out of the table rather than checking they exist.
const TRIP_ID_RE = /^[A-Za-z0-9_-]{1,128}$/;
const PLACE_ID_RE = /^[A-Za-z0-9_-]{1,512}$/;

const SwipeBodySchema = z.object({
  tripId: z.string().trim().regex(TRIP_ID_RE),
  googlePlaceId: z.string().trim().regex(PLACE_ID_RE),
  action: z.enum(["LIKE", "DISLIKE"]),
});

/**
 * POST /api/trip/swipe { tripId, googlePlaceId, action: "LIKE" | "DISLIKE" }
 * - records the signed-in user's verdict on one place from the swipe deck
 * (components/trip/SwipeDeck.tsx; "Pass" is DISLIKE) in UserSwipeAction.
 *
 * The user is always the session's, never anything in the body, so nobody
 * can record a swipe as someone else. A later swipe on the same (user,
 * trip, place) REPLACES the earlier one rather than adding a row - the
 * schema's @@unique([userId, tripId, googlePlaceId]) plus an upsert, the
 * same atomic "resubmission replaces" convention as addPollVote in
 * lib/store.ts, so a double-tap or a changed mind can't race into a
 * duplicate. createdAt is bumped on a replace so it always reads as "when
 * this verdict was given" (the model has no separate updatedAt).
 *
 * Only the place id is stored, deliberately: Google's Places policies
 * allow keeping ids indefinitely but forbid storing the rest of a place's
 * content. Resolve names/photos again via the Places API when needed.
 *
 * Responses: 200 { ok: true, ... }, 400 bad body, 401 signed out, 429
 * rate-limited, 500 the write failed (the cause is in the server log).
 */
export async function POST(request: NextRequest) {
  const userId = await getSessionUserId();
  if (!userId) {
    return NextResponse.json({ error: "Sign in to save your picks." }, { status: 401 });
  }

  const parsed = SwipeBodySchema.safeParse(await request.json().catch(() => null));
  if (!parsed.success) {
    return NextResponse.json(
      { error: "tripId, googlePlaceId and action (LIKE or DISLIKE) are required." },
      { status: 400 }
    );
  }
  const { tripId, googlePlaceId, action } = parsed.data;

  const rateLimit = checkRateLimit(`swipe:${userId}`, SWIPE_RATE_LIMIT, SWIPE_RATE_WINDOW_MS);
  if (!rateLimit.allowed) {
    return NextResponse.json(
      { error: "Too many swipes - please wait a moment." },
      { status: 429, headers: { "Retry-After": String(rateLimit.retryAfterSeconds) } }
    );
  }

  try {
    await prisma.userSwipeAction.upsert({
      where: { userId_tripId_googlePlaceId: { userId, tripId, googlePlaceId } },
      create: { userId, tripId, googlePlaceId, action },
      update: { action, createdAt: new Date() },
    });

    return NextResponse.json({ ok: true, tripId, googlePlaceId, action });
  } catch (error) {
    console.error(`POST /api/trip/swipe failed for trip ${tripId}:`, error);
    return NextResponse.json(
      { error: "Couldn't save your pick. Please try again." },
      { status: 500 }
    );
  }
}

/**
 * GET /api/trip/swipe?tripId=... - the signed-in user's own swipes for one
 * trip, as { swipes: [{ googlePlaceId, action }] }. The deck uses it to
 * pick up where someone left off after a refresh instead of dealing them
 * places they've already answered. Scoped to the session's user - there is
 * deliberately no way to read anyone else's swipes from here.
 */
export async function GET(request: NextRequest) {
  const userId = await getSessionUserId();
  if (!userId) {
    return NextResponse.json({ error: "Sign in to see your picks." }, { status: 401 });
  }

  const tripId = request.nextUrl.searchParams.get("tripId") ?? "";
  if (!TRIP_ID_RE.test(tripId)) {
    return NextResponse.json({ error: "tripId is required." }, { status: 400 });
  }

  try {
    const swipes = await prisma.userSwipeAction.findMany({
      where: { userId, tripId },
      select: { googlePlaceId: true, action: true },
    });

    return NextResponse.json({ tripId, swipes }, { headers: { "Cache-Control": "private, no-store" } });
  } catch (error) {
    console.error(`GET /api/trip/swipe failed for trip ${tripId}:`, error);
    return NextResponse.json({ error: "Couldn't load your picks." }, { status: 500 });
  }
}
