import { NextRequest, NextResponse } from "next/server";
import { getSessionUserId } from "@/lib/auth";
import { CARDS_PER_CATEGORY, DECK_CATEGORIES, normalizeDeckCategory } from "@/lib/placeDeck";
import { prisma } from "@/lib/prisma";
import { checkRateLimit } from "@/lib/rateLimit";
import type { DeckPlaceCategory, SwipeChoice } from "@/lib/tripTypes";

// Saving a profile is once per person (plus a retake or a retry), so this
// only stops a runaway client loop from hammering the database.
const TASTE_RATE_LIMIT = 10;
const TASTE_RATE_WINDOW_MS = 60_000;

// Google place ids are URL-safe base64-ish bare strings (see UserSwipeAction in
// prisma/schema.prisma); this only keeps obvious junk out of the table.
const PLACE_ID_RE = /^[A-Za-z0-9_-]{1,512}$/;

// The most a deck can hold - every theme's full share - so more distinct places
// than this are not a set of quiz answers.
const MAX_ANSWERS = DECK_CATEGORIES.length * CARDS_PER_CATEGORY;

// How many refused answers one request logs, so a hostile body cannot flood the log.
const MAX_LOGGED_REJECTIONS = 5;

// NextAuth's session cookie, plain and "__Secure-" prefixed (https): present
// without a session behind it means expired or revoked, absent means signed out.
const SESSION_COOKIES = ["next-auth.session-token", "__Secure-next-auth.session-token"];

interface Answer {
  googlePlaceId: string;
  category: DeckPlaceCategory;
  action: SwipeChoice;
}

function normalizePlaceId(value: unknown): string | null {
  if (typeof value !== "string") return null;
  // Google's resource name for the same place is "places/<id>".
  const id = value.trim().replace(/^places\//, "");
  return PLACE_ID_RE.test(id) ? id : null;
}

function normalizeAction(value: unknown): SwipeChoice | null {
  if (typeof value !== "string") return null;
  switch (value.trim().toUpperCase()) {
    case "LIKE":
    case "LIKED":
      return "LIKE";
    case "DISLIKE":
    case "DISLIKED":
    case "PASS":
    case "PASSED":
      return "DISLIKE";
    default:
      return null;
  }
}

function clip(value: unknown): string {
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return (text ?? "undefined").slice(0, 40);
}

/**
 * Reads the body's answers one by one: the good ones, and why each other one was
 * left out. A bad answer costs only itself - one odd place id must not throw
 * away the other 23 - and the caller refuses the request only when none is left.
 */
function readAnswers(body: unknown): { answers: Answer[]; rejected: string[] } | null {
  const raw = (body as { answers?: unknown } | null)?.answers;
  if (!Array.isArray(raw)) return null;

  const answers: Answer[] = [];
  const rejected: string[] = [];
  raw.forEach((item: unknown, index) => {
    const entry = (item ?? {}) as Record<string, unknown>;
    const googlePlaceId = normalizePlaceId(entry.googlePlaceId);
    const category = normalizeDeckCategory(entry.category);
    const action = normalizeAction(entry.action);
    if (googlePlaceId && category && action) {
      answers.push({ googlePlaceId, category, action });
      return;
    }
    const why = [
      googlePlaceId ? null : `googlePlaceId=${clip(entry.googlePlaceId)}`,
      category ? null : `category=${clip(entry.category)}`,
      action ? null : `action=${clip(entry.action)}`,
    ].filter(Boolean);
    rejected.push(`#${index}: ${why.join(", ")}`);
  });
  return { answers, rejected };
}

/**
 * What went wrong with the write, in words a log reader can act on, from the
 * error Prisma (or the driver under it) threw. `status` is what the client is
 * told: a missing user row is a sign-in problem (401), an unreachable database
 * is 503, everything else 500.
 */
function classifyFailure(error: unknown): { code: string; status: number; detail: string; prismaCode: string } {
  const prismaCode = String((error as { code?: unknown } | null)?.code ?? "");
  const message = error instanceof Error ? error.message : String(error);
  const detail = message.split("\n").find((line) => line.trim()) ?? message;

  if (prismaCode === "P2021" || prismaCode === "P2022" || /(relation|column|table) .* does not exist/i.test(message)) {
    return {
      code: "SCHEMA_OUT_OF_DATE",
      status: 500,
      detail: `the database is missing the TasteSwipe table or User.hasCompletedTasteProfile - run \`prisma db push\` against it (${detail})`,
      prismaCode,
    };
  }
  if (prismaCode === "P2003" || prismaCode === "P2025") {
    return { code: "USER_NOT_FOUND", status: 401, detail: `no User row for this session (${detail})`, prismaCode };
  }
  if (["P1001", "P1002", "P1008", "P1017", "P2024"].includes(prismaCode) || /ECONNREFUSED|ETIMEDOUT|timeout/i.test(message)) {
    return { code: "DATABASE_UNREACHABLE", status: 503, detail: `the database did not answer (${detail})`, prismaCode };
  }
  return { code: "SAVE_FAILED", status: 500, detail, prismaCode };
}

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
 * TasteSwipe's @@unique([userId, googlePlaceId]) means a retake - or a retry of
 * a save that was only half seen by the client - REPLACES the earlier answer
 * instead of adding a row, the same "resubmission replaces" convention as the
 * swipe route and addPollVote in lib/store.ts. A place named twice in one
 * request keeps its later answer. The replace is a delete + insert in the one
 * transaction (three statements however many answers there are, where an
 * upsert per answer was 24 round trips to a database that may be an ocean
 * away); `skipDuplicates` makes two copies of the request racing each other
 * harmless, the loser simply finds the rows already there.
 *
 * WHO. Always the NextAuth session's user, never anything in the body, and
 * there is no other way in: TasteSwipe.userId is a foreign key to User, and
 * every other identity in the app (a poll vote's "user:<id>" voterKey, a trip
 * participant) is derived from that same session, so there is no separate voter
 * or participant token to fall back on when the session is gone - a signed-out
 * caller is told so (401) rather than saved under a guess.
 *
 * WHAT. `category` is the client's word for which taste a card measured (the
 * server deals a fresh deck per request and remembers none), matched leniently
 * (normalizeDeckCategory: case, plural, the on-screen label) and stored as the
 * canonical id; `action` likewise. A single unreadable answer is skipped, not
 * fatal: the request fails (400) only when no answer is readable at all. Only
 * place ids are stored: Google's Places policies forbid keeping anything else
 * about a place.
 *
 * Every refusal is logged with a "[taste-profile]" prefix and a `code`, and the
 * code is in the JSON too, so "wasn't saved" can be traced to a reason.
 *
 * Responses: 200 { ok: true, saved, skipped, completed: true }, 400 no readable
 * answer, 401 signed out (or no User row), 429 rate-limited, 503 database
 * unreachable, 500 the write failed. Errors are { error, code }.
 */
export async function POST(request: NextRequest) {
  const userId = await getSessionUserId();
  if (!userId) {
    console.warn(
      `[taste-profile] refused: no session (session cookie ${
        SESSION_COOKIES.some((name) => request.cookies.has(name)) ? "present but not valid - expired or revoked" : "absent"
      })`
    );
    return NextResponse.json(
      { error: "Sign in to save your taste profile.", code: "NOT_SIGNED_IN" },
      { status: 401 }
    );
  }

  const read = readAnswers(await request.json().catch(() => null));
  if (!read || read.answers.length === 0) {
    console.warn(
      `[taste-profile] refused for user ${userId}: ${
        read ? `none of ${read.rejected.length} answers is readable (${read.rejected.slice(0, MAX_LOGGED_REJECTIONS).join("; ")})` : "the body has no answers list"
      }`
    );
    return NextResponse.json(
      {
        error:
          "answers must list at least one answer with googlePlaceId, a known category and action (LIKE or DISLIKE).",
        code: "NO_READABLE_ANSWERS",
      },
      { status: 400 }
    );
  }

  const rateLimit = checkRateLimit(`taste:${userId}`, TASTE_RATE_LIMIT, TASTE_RATE_WINDOW_MS);
  if (!rateLimit.allowed) {
    console.warn(`[taste-profile] refused for user ${userId}: rate limited`);
    return NextResponse.json(
      { error: "Too many requests - please wait a moment.", code: "RATE_LIMITED" },
      { status: 429, headers: { "Retry-After": String(rateLimit.retryAfterSeconds) } }
    );
  }

  // One answer per place (the later one wins), and no more than a deck holds.
  const distinct = [...new Map(read.answers.map((answer) => [answer.googlePlaceId, answer] as const)).values()];
  const answers = distinct.slice(0, MAX_ANSWERS);
  const skipped = read.rejected.length + (distinct.length - answers.length);
  if (skipped > 0) {
    console.warn(
      `[taste-profile] user ${userId}: saving ${answers.length} answers, skipped ${skipped}` +
        (read.rejected.length > 0 ? ` (unreadable: ${read.rejected.slice(0, MAX_LOGGED_REJECTIONS).join("; ")})` : "")
    );
  }

  try {
    await prisma.$transaction([
      prisma.tasteSwipe.deleteMany({
        where: { userId, googlePlaceId: { in: answers.map((answer) => answer.googlePlaceId) } },
      }),
      prisma.tasteSwipe.createMany({
        data: answers.map(({ googlePlaceId, category, action }) => ({ userId, googlePlaceId, category, action })),
        skipDuplicates: true,
      }),
      prisma.user.update({ where: { id: userId }, data: { hasCompletedTasteProfile: true } }),
    ]);

    console.log(`[taste-profile] saved ${answers.length} answers for user ${userId}`);
    return NextResponse.json({ ok: true, saved: answers.length, skipped, completed: true });
  } catch (error) {
    const failure = classifyFailure(error);
    console.error(
      `[taste-profile] save failed for user ${userId}: ${failure.code}${failure.prismaCode ? ` (${failure.prismaCode})` : ""} - ${failure.detail}`
    );
    return NextResponse.json(
      {
        error:
          failure.status === 401
            ? "Sign in again to save your taste profile."
            : "Couldn't save your taste profile. Please try again.",
        code: failure.code,
      },
      { status: failure.status }
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
    console.error(`[taste-profile] read failed for user ${userId}:`, error);
    return NextResponse.json({ error: "Couldn't load your taste profile." }, { status: 500 });
  }
}
