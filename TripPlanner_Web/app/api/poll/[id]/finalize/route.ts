import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import { generationFailureResponse } from "@/lib/generationErrors";
import type { Itinerary } from "@/lib/itinerary";
import { lockAndGeneratePoll } from "@/lib/pollLock";
import { checkRateLimit } from "@/lib/rateLimit";
import { verifySignedRequest } from "@/lib/serverAuth";
import { getPollCreator } from "@/lib/store";
import { tasteLabels } from "@/lib/tasteSummary";

// Exactly the budget of the route this shares its pipeline with
// (POST /api/trigger-jarvis, see lib/pollLock.ts): two Haiku calls, Places,
// Routes and weather, ~15-20s for a real trip, the second Haiku call dominating.
export const maxDuration = 60;

// params is a Promise in this Next.js version, not a plain object - see
// node_modules/next/dist/docs/01-app/03-api-reference/03-file-conventions/route.md.
type RouteParams = { params: Promise<{ id: string }> };

// Same allowance as the organizer's button (trigger-jarvis): locking spends real
// Anthropic and Google quota. Counted only after the caller has been allowed to
// lock this poll at all, so a refused request cannot use up the real one's turns.
const FINALIZE_RATE_LIMIT = 3;
const FINALIZE_RATE_WINDOW_MS = 5 * 60_000;

const FinalizeBodySchema = z.object({
  // The signed-in person Jarvis is acting for - the web chat's session user. Absent
  // means the voice assistant: the owner of the machine, with no web account.
  userId: z.string().trim().min(1).max(128).optional(),
});

function summarize(itinerary: Itinerary) {
  return {
    destination: itinerary.destination,
    days: itinerary.days.length,
    stops: itinerary.days.reduce((total, day) => total + day.stops.length, 0),
  };
}

/**
 * POST /api/poll/[id]/finalize { userId? } - Jarvis locks a poll and generates
 * its plan from the votes, without the organizer's admin token: "finalize my
 * Phuket poll" in the web chat, or by voice. Answers
 *
 *   201 { tripId, path, locked: true, alreadyLocked: false, destination, days,
 *         stops, voters, tasteVoters, personalized, taste }   a plan was
 *         generated; `path` ("/trip/poll/<id>") is the page that shows it - the
 *         caller prefixes its own origin to make the link; `personalized`/`taste`
 *         say whether the voters' Taste Profiles shaped it (the same labels as
 *         POST /api/trip/create) and `tasteVoters` how many of the `voters` had
 *         a finished quiz to be read
 *   200 { ..., alreadyLocked: true, destination, days, stops }   it was locked
 *         already; this is the plan that exists, nothing new was generated
 *
 * and 400 bad body or no votes yet, 401 bad/missing signature, 403 not allowed
 * (below), 404 no such poll, 409 a generation is already running, 429 rate
 * limited (ours or Anthropic's), 502 Places/Anthropic failed, 503 no
 * API_SECRET_KEY configured, 500 anything else.
 *
 * AUTHORIZATION has two parts, both required, and the second is why this is not
 * simply "anyone with the shared secret may lock":
 *
 *  1. The request is signed with the shared secret over its path and body
 *     (lib/serverAuth.ts - the same scheme as POST /api/trip/create), so only
 *     Jarvis can call it and a captured request cannot be replayed with a
 *     different userId.
 *  2. The caller must be allowed to lock THIS poll. The admin token used to be
 *     the whole answer - only the organizer, who was sent it privately, could lock
 *     - and the poll's voting link, which every voter holds, is public. Without
 *     a rule here, any signed-in person could tell Jarvis "finalize <that link>"
 *     in the chat and lock a poll that is not theirs. The rule is Poll.createdByUserId
 *     (written when Jarvis creates a poll from the chat, see POST
 *     /api/poll/[id]/admin-token):
 *       - a web caller (userId) may lock a poll whose creator is that same userId;
 *       - a voice caller (no userId: the owner at the microphone) may lock a poll
 *         whose creator is null - one made by voice, or before the column existed;
 *       - anything else is a 403, and nothing has been claimed or spent.
 *     A poll made from the chat is therefore not lockable by voice, and a poll
 *     made by voice is not lockable from the chat; the organizer's link still
 *     works for both (POST /api/trigger-jarvis).
 *
 * The poll must already exist: it is looked up (read-only) before anything else,
 * because claiming a trip id that has no row would create one.
 */
export async function POST(request: NextRequest, { params }: RouteParams) {
  const { id } = await params;
  const path = `/api/poll/${id}/finalize`;

  // The text, before any parsing: the signature covers these exact bytes.
  const rawBody = await request.text();

  const authFailure = verifySignedRequest(request.headers, "POST", path, rawBody);
  if (authFailure) {
    return NextResponse.json({ error: authFailure.error }, { status: authFailure.status });
  }

  let json: unknown = {};
  if (rawBody.trim() !== "") {
    try {
      json = JSON.parse(rawBody);
    } catch {
      return NextResponse.json({ error: "The body must be JSON." }, { status: 400 });
    }
  }
  const parsed = FinalizeBodySchema.safeParse(json);
  if (!parsed.success) {
    return NextResponse.json({ error: "userId, when sent, must be a non-empty string." }, { status: 400 });
  }
  const { userId } = parsed.data;

  try {
    const poll = await getPollCreator(id);
    if (!poll) {
      return NextResponse.json({ error: "There is no poll with that id." }, { status: 404 });
    }

    const allowed = userId !== undefined ? poll.createdByUserId === userId : poll.createdByUserId === null;
    if (!allowed) {
      if (userId !== undefined && poll.createdByUserId === null) {
        return NextResponse.json(
          {
            error:
              "This poll was created by voice, not from the chat, so it cannot be locked from here. " +
              "Use the organizer link that was sent to LINE.",
            reason: "CREATED_BY_VOICE",
          },
          { status: 403 }
        );
      }
      if (userId !== undefined) {
        return NextResponse.json(
          {
            error:
              "Only the person who created this poll in the chat can lock it from the chat. " +
              "Use the organizer link that was sent to LINE instead.",
            reason: "NOT_THE_CREATOR",
          },
          { status: 403 }
        );
      }
      return NextResponse.json(
        {
          error:
            "This poll was created from the web chat, so only its creator can lock it from there " +
            "(or anyone holding the organizer link).",
          reason: "CREATED_IN_CHAT",
        },
        { status: 403 }
      );
    }

    const rateLimit = checkRateLimit(`finalize:${id}`, FINALIZE_RATE_LIMIT, FINALIZE_RATE_WINDOW_MS);
    if (!rateLimit.allowed) {
      return NextResponse.json(
        { error: "Too many plan-generation requests for this poll - please wait a few minutes and try again." },
        { status: 429, headers: { "Retry-After": String(rateLimit.retryAfterSeconds) } }
      );
    }

    const result = await lockAndGeneratePoll(id, "poll-finalize");
    const base = { tripId: id, path: `/trip/poll/${id}`, locked: true };

    switch (result.kind) {
      case "generated":
        return NextResponse.json(
          {
            ...base,
            alreadyLocked: false,
            ...summarize(result.itinerary),
            voters: result.voters,
            // How many of those voters had a finished taste quiz to be read.
            tasteVoters: result.taste ? result.taste.taste.people : 0,
            personalized: result.taste !== null,
            taste: result.taste ? tasteLabels(result.taste.taste) : null,
          },
          { status: 201 }
        );

      case "already_locked":
        return NextResponse.json({ ...base, alreadyLocked: true, ...summarize(result.itinerary) });

      case "locked_without_plan":
        console.error(`POST ${path}: trip ${id} is locked but has no valid stored plan.`);
        return NextResponse.json(
          { error: "This poll is locked but its plan could not be loaded." },
          { status: 500 }
        );

      case "in_progress":
        return NextResponse.json(
          { error: "A plan is already being generated for this poll - please wait a moment and try again." },
          { status: 409 }
        );

      case "no_votes":
        return NextResponse.json(
          { error: "No one has voted on this poll yet - wait for votes before locking it." },
          { status: 400 }
        );
    }
  } catch (error) {
    const known = generationFailureResponse(error, `POST ${path}`);
    if (known) return known;
    console.error(`POST ${path} failed:`, error);
    return NextResponse.json(
      { error: "Something went wrong while generating the plan. Please try again." },
      { status: 500 }
    );
  }
}
