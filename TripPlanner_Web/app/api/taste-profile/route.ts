import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import { getSessionUserId } from "@/lib/auth";
import { CARDS_PER_CATEGORY, DECK_CATEGORIES, isDeckCategory } from "@/lib/placeDeck";
import { prisma } from "@/lib/prisma";
import { checkRateLimit } from "@/lib/rateLimit";
import type { DeckPlaceCategory } from "@/lib/tripTypes";

// Saving a profile is once per person (plus a retake or a retry), so this
// only stops a runaway client loop from hammering the database.
const TASTE_RATE_LIMIT = 10;
const TASTE_RATE_WINDOW_MS = 60_000;

// Google place ids are URL-safe base64-ish bare strings (see UserSwipeAction in
// prisma/schema.prisma); this only keeps obvious junk out of the table.
const PLACE_ID_RE = /^[A-Za-z0-9_-]{1,512}$/;

// The most a deck can hold - every theme's full share - so a longer list is
// not a set of quiz answers.
const MAX_ANSWERS = DECK_CATEGORIES.length * CARDS_PER_CATEGORY;

const AnswerSchema = z.object({
  googlePlaceId: z.string().trim().regex(PLACE_ID_RE),
  category: z.custom<DeckPlaceCategory>(isDeckCategory),
  action: z.enum(["LIKE", "DISLIKE"]),
});

const TasteProfileBodySchema = z.object({
  answers: z.array(AnswerSchema).min(1).max(MAX_ANSWERS),
});

/**
 * POST /api/taste-profile { answers: [{ googlePlaceId, category, action }] } -
 * saves a finished taste quiz as the signed-in person's Taste Profile and marks
 * the quiz as done, in ONE transaction: either every answer is stored AND
 * User.hasCompletedTasteProfile turns true, or nothing changes.
 *
 * Called once by components/trip/SwipeDeck.tsx when the last card has been
 * answered, with every answer in the deck. The quiz is the same worldwide deck
 * for everyone (GET /api/places/deck), so unlike UserSwipeAction (one row per
 * user, TRIP and place, written card by card) these rows belong to the PERSON:
 * TasteSwipe's @@unique([userId, googlePlaceId]) plus an upsert means a retake -
 * or a retry of a save that was only half seen by the client - REPLACES the
 * earlier answer instead of adding a row, the same atomic "resubmission
 * replaces" convention as the swipe route and addPollVote in lib/store.ts. A
 * place named twice in one request keeps its later answer.
 *
 * The user is always the session's, never anything in the body. `category` is
 * the client's word for which taste a card measured (the server deals a fresh
 * deck per request and remembers none), so it is only checked to be one of
 * DECK_CATEGORIES' ids, exactly as POST /api/trip/swipe does; it can only ever
 * describe the caller's own taste. Only place ids are stored: Google's Places
 * policies forbid keeping anything else about a place.
 *
 * Responses: 200 { ok: true, saved, completed: true }, 400 bad body (no
 * answers, more than a deck holds, an unknown category...), 401 signed out,
 * 429 rate-limited, 500 the write failed (the cause is in the server log).
 */
export async function POST(request: NextRequest) {
  const userId = await getSessionUserId();
  if (!userId) {
    return NextResponse.json({ error: "Sign in to save your taste profile." }, { status: 401 });
  }

  const parsed = TasteProfileBodySchema.safeParse(await request.json().catch(() => null));
  if (!parsed.success) {
    return NextResponse.json(
      {
        error: `answers must list 1 to ${MAX_ANSWERS} answers, each with googlePlaceId, a known category and action (LIKE or DISLIKE).`,
      },
      { status: 400 }
    );
  }

  const rateLimit = checkRateLimit(`taste:${userId}`, TASTE_RATE_LIMIT, TASTE_RATE_WINDOW_MS);
  if (!rateLimit.allowed) {
    return NextResponse.json(
      { error: "Too many requests - please wait a moment." },
      { status: 429, headers: { "Retry-After": String(rateLimit.retryAfterSeconds) } }
    );
  }

  const answers = [...new Map(parsed.data.answers.map((answer) => [answer.googlePlaceId, answer] as const)).values()];

  try {
    await prisma.$transaction([
      ...answers.map(({ googlePlaceId, category, action }) =>
        prisma.tasteSwipe.upsert({
          where: { userId_googlePlaceId: { userId, googlePlaceId } },
          create: { userId, googlePlaceId, category, action },
          update: { category, action, createdAt: new Date() },
        })
      ),
      prisma.user.update({ where: { id: userId }, data: { hasCompletedTasteProfile: true } }),
    ]);

    return NextResponse.json({ ok: true, saved: answers.length, completed: true });
  } catch (error) {
    console.error(`POST /api/taste-profile failed for user ${userId}:`, error);
    return NextResponse.json(
      { error: "Couldn't save your taste profile. Please try again." },
      { status: 500 }
    );
  }
}

/**
 * GET /api/taste-profile - { completed: boolean }: whether the signed-in
 * person has already finished the taste quiz (User.hasCompletedTasteProfile),
 * so the poll page need not send them to it and the swipe page can send them
 * back (see lib/tasteProfile.ts, which is how both ask). Scoped to the
 * session's user; there is deliberately no way to read anyone else's.
 *
 * Responses: 200 { completed }, 401 signed out, 500 the read failed.
 */
export async function GET() {
  const userId = await getSessionUserId();
  if (!userId) {
    return NextResponse.json({ error: "Sign in to see your taste profile." }, { status: 401 });
  }

  try {
    const user = await prisma.user.findUnique({
      where: { id: userId },
      select: { hasCompletedTasteProfile: true },
    });

    return NextResponse.json(
      { completed: user?.hasCompletedTasteProfile === true },
      { headers: { "Cache-Control": "private, no-store" } }
    );
  } catch (error) {
    console.error(`GET /api/taste-profile failed for user ${userId}:`, error);
    return NextResponse.json({ error: "Couldn't load your taste profile." }, { status: 500 });
  }
}
