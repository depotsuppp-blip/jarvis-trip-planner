/**
 * Persistence for the Trip Planner foundation, backed by Postgres (Neon)
 * via Prisma - see prisma/schema.prisma. This used to be two flat JSON
 * files under .data/, which worked in local dev (writable disk) but
 * crashed in production: Vercel serverless functions have a read-only
 * filesystem outside of /tmp, and /tmp itself is ephemeral and not
 * shared across instances, so votes would vanish or 500 depending on
 * which instance handled the next request.
 */

import crypto from "crypto";
import { prisma } from "./prisma";

// ---------------------------------------------------------------------
// Consensus poll votes - one trip id maps to a list of friends' entries
// ---------------------------------------------------------------------

export interface PollVote {
  name: string;
  /**
   * The verified LINE user id, or "" for a vote submitted without a
   * LINE session - see app/api/poll/[id]/route.ts's POST handler,
   * which now accepts both. An anonymous vote has no real identity
   * behind it beyond the self-reported `name`.
   */
  lineUserId: string;
  startDate: string;
  endDate: string;
  wishlist: string;
  vibes: string[];
  submittedAt: string;
}

export async function getPollVotes(tripId: string): Promise<PollVote[]> {
  const rows = await prisma.pollVote.findMany({
    where: { tripId },
    orderBy: { submittedAt: "asc" },
  });
  return rows.map((row) => ({
    name: row.name,
    lineUserId: row.lineUserId,
    startDate: row.startDate,
    endDate: row.endDate,
    wishlist: row.wishlist,
    vibes: row.vibes,
    submittedAt: row.submittedAt.toISOString(),
  }));
}

/**
 * "line:<lineUserId>" or "anon:<anonId>" - whichever identity this vote
 * actually carries - or a one-off value that can never collide when
 * neither is available (an old cached client that predates anonId, or
 * localStorage unavailable), so that submission is simply never deduped
 * rather than colliding with an unrelated voter. See voterKey's comment
 * in prisma/schema.prisma for why this can't just be lineUserId or
 * anonId directly: both default to "" and every anonymous row would
 * otherwise collide with every other anonymous row on lineUserId="" (and
 * likewise every verified row on anonId="").
 */
function computeVoterKey(lineUserId: string, anonId: string): string {
  if (lineUserId) return `line:${lineUserId}`;
  if (anonId) return `anon:${anonId}`;
  return `once:${crypto.randomUUID()}`;
}

/**
 * anonId is the dedup key for a resubmission with no verified LINE
 * session - a client-generated UUID persisted in the voter's browser
 * localStorage (see anonVoterId: in app/trip/poll/[id]/page.tsx), NOT
 * the typed name. Two different people can type the same or a
 * similarly-cased name; matching on that would silently let one
 * overwrite the other's vote. anonId has no such collision risk in
 * practice, same as lineUserId for a verified voter.
 */
export async function addPollVote(
  tripId: string,
  vote: PollVote,
  anonId: string
): Promise<PollVote[]> {
  const resolvedAnonId = vote.lineUserId ? "" : anonId;
  const voterKey = computeVoterKey(vote.lineUserId, resolvedAnonId);

  // One vote per voter per trip - a resubmission (a double-tap on the
  // button, or someone changing their mind and voting again) replaces
  // their previous entry instead of appending a duplicate. This is a
  // single atomic INSERT ... ON CONFLICT DO UPDATE against the
  // @@unique([tripId, voterKey]) constraint in prisma/schema.prisma, so
  // two concurrent requests from the same voter (a genuine double-tap
  // firing overlapping requests) can't both pass a "no existing row"
  // check and each insert - the database itself serializes them, unlike
  // an application-level check-then-write.
  await prisma.pollVote.upsert({
    where: { tripId_voterKey: { tripId, voterKey } },
    create: {
      tripId,
      voterKey,
      name: vote.name,
      lineUserId: vote.lineUserId,
      anonId: resolvedAnonId,
      startDate: vote.startDate,
      endDate: vote.endDate,
      wishlist: vote.wishlist,
      vibes: vote.vibes,
      submittedAt: new Date(vote.submittedAt),
    },
    update: {
      name: vote.name,
      startDate: vote.startDate,
      endDate: vote.endDate,
      wishlist: vote.wishlist,
      vibes: vote.vibes,
      submittedAt: new Date(vote.submittedAt),
    },
  });

  return getPollVotes(tripId);
}

// ---------------------------------------------------------------------
// Solo draft board - one trip id maps to a single evolving text blob
// ---------------------------------------------------------------------

export interface TripDraft {
  text: string;
  updatedAt: string;
}

export async function getDraft(tripId: string): Promise<TripDraft | null> {
  const row = await prisma.tripDraft.findUnique({ where: { tripId } });
  if (!row) return null;
  return { text: row.text, updatedAt: row.updatedAt.toISOString() };
}

export async function saveDraft(
  tripId: string,
  text: string
): Promise<TripDraft> {
  const row = await prisma.tripDraft.upsert({
    where: { tripId },
    create: { tripId, text },
    update: { text },
  });
  return { text: row.text, updatedAt: row.updatedAt.toISOString() };
}

// ---------------------------------------------------------------------
// Poll lock status - set once "Lock & Generate Plan" (POST
// /api/trigger-jarvis) has generated an itinerary for a trip
// ---------------------------------------------------------------------

export async function isPollLocked(tripId: string): Promise<boolean> {
  const row = await prisma.poll.findUnique({ where: { tripId } });
  return row?.locked ?? false;
}

/**
 * lockedByLineUserId is metadata only, recorded when the caller happened
 * to have a verified LINE session at trigger time - "" otherwise. It is
 * never an access check: claimPollForGeneration is what actually
 * prevents duplicate spend for a trip, regardless of who is calling.
 */
export async function lockPoll(tripId: string, lockedByLineUserId: string): Promise<void> {
  await prisma.poll.upsert({
    where: { tripId },
    create: { tripId, locked: true, lockedAt: new Date(), generating: false, lockedByLineUserId },
    update: { locked: true, lockedAt: new Date(), generating: false, lockedByLineUserId },
  });
}

// A claim older than this is treated as abandoned - see Poll.generating's
// comment in prisma/schema.prisma for why (a platform-level timeout kill
// does not reliably run a `finally` block). Comfortably longer than
// app/api/trigger-jarvis/route.ts's maxDuration=60 cap.
const GENERATION_CLAIM_STALE_MS = 90_000;

export type PollClaimResult = "claimed" | "locked" | "in_progress";

/**
 * Atomically claims the right to generate this trip's plan, so two
 * near-simultaneous "Lock & Generate Plan" clicks can't both pass a
 * check-then-write race into two paid Anthropic calls. A single INSERT
 * ... ON CONFLICT ... WHERE ... RETURNING statement, not a
 * read-then-write - Prisma's upsert() has no conditional-update clause,
 * so this needs raw SQL. tripId and staleCutoff are passed through
 * Prisma's tagged-template parameterization, never string-concatenated.
 *
 * Returns:
 *   "claimed"     - this call now owns the generation; the caller must
 *                   follow up with either lockPoll (on success) or
 *                   releasePollClaim (on failure).
 *   "locked"      - a plan already exists; read it via getDraft instead
 *                   of generating a new one.
 *   "in_progress" - another request currently holds the claim; the
 *                   caller should ask the user to wait rather than
 *                   starting a second generation.
 */
export async function claimPollForGeneration(tripId: string): Promise<PollClaimResult> {
  const staleCutoff = new Date(Date.now() - GENERATION_CLAIM_STALE_MS);
  const claimed = await prisma.$queryRaw<{ tripId: string }[]>`
    INSERT INTO "Poll" ("tripId", "generating", "generatingStartedAt")
    VALUES (${tripId}, true, now())
    ON CONFLICT ("tripId") DO UPDATE
    SET "generating" = true, "generatingStartedAt" = now()
    WHERE "Poll"."locked" = false
      AND ("Poll"."generating" = false OR "Poll"."generatingStartedAt" < ${staleCutoff})
    RETURNING "tripId"
  `;
  if (claimed.length > 0) {
    return "claimed";
  }

  const existing = await prisma.poll.findUnique({ where: { tripId } });
  return existing?.locked ? "locked" : "in_progress";
}

/**
 * Releases a claim taken by claimPollForGeneration without marking the
 * poll locked - call this when generation failed, so a retry isn't
 * permanently blocked by a stuck "in_progress" claim (see
 * GENERATION_CLAIM_STALE_MS above for the fallback if even this never
 * runs, e.g. the instance is killed mid-request).
 */
export async function releasePollClaim(tripId: string): Promise<void> {
  await prisma.poll.updateMany({
    where: { tripId, locked: false },
    data: { generating: false },
  });
}
