/**
 * Persistence for the Trip Planner foundation, backed by Postgres (Neon)
 * via Prisma - see prisma/schema.prisma. This used to be two flat JSON
 * files under .data/, which worked in local dev (writable disk) but
 * crashed in production: Vercel serverless functions have a read-only
 * filesystem outside of /tmp, and /tmp itself is ephemeral and not
 * shared across instances, so votes would vanish or 500 depending on
 * which instance handled the next request.
 */

import { prisma } from "./prisma";

// ---------------------------------------------------------------------
// Consensus poll votes - one trip id maps to a list of friends' entries
// ---------------------------------------------------------------------

export interface PollVote {
  name: string;
  /**
   * The verified LINE user id of a vote cast before signing in became
   * required to vote, otherwise "". A vote is now identified by the
   * NextAuth account that cast it (the row's voterKey, see
   * pollVoterKey below), not by anything stored in this field.
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
 * The dedup key for a signed-in voter: "user:<User.id>" - one vote per
 * account per trip, the same on every device. Rows written before signing
 * in was required to vote carry "line:<lineUserId>", "anon:<anonId>" or a
 * one-off "once:<random>" instead (see voterKey in prisma/schema.prisma);
 * they stay exactly as they are and simply never match a signed-in voter
 * again. voterKey is a plain string under @@unique([tripId, voterKey]), so
 * this needs no schema change.
 */
export function pollVoterKey(userId: string): string {
  return `user:${userId}`;
}

/**
 * Records `userId`'s vote on a trip, replacing their previous entry if they
 * had one, and says which of the two happened: `created` is true only the
 * first time this account votes on this trip (the poll page uses it to send
 * a first-time voter on to the swipe step).
 *
 * The INSERT ... ON CONFLICT DO NOTHING behind createMany's skipDuplicates
 * is what decides "first": the @@unique([tripId, voterKey]) constraint, not
 * an application-level look-before-you-write, so two overlapping requests
 * from one voter (a double-tap) can neither both create a row nor both
 * report `created`. The loser of that race takes the update below.
 */
export async function addPollVote(
  tripId: string,
  vote: PollVote,
  userId: string
): Promise<{ votes: PollVote[]; created: boolean }> {
  const voterKey = pollVoterKey(userId);
  const entry = {
    name: vote.name,
    startDate: vote.startDate,
    endDate: vote.endDate,
    wishlist: vote.wishlist,
    vibes: vote.vibes,
    submittedAt: new Date(vote.submittedAt),
  };

  const inserted = await prisma.pollVote.createMany({
    data: [{ tripId, voterKey, ...entry }],
    skipDuplicates: true,
  });
  const created = inserted.count === 1;

  if (!created) {
    await prisma.pollVote.update({
      where: { tripId_voterKey: { tripId, voterKey } },
      data: entry,
    });
  }

  return { votes: await getPollVotes(tripId), created };
}

/** Whether `userId` already has an entry on this trip. */
export async function hasPollVote(tripId: string, userId: string): Promise<boolean> {
  const row = await prisma.pollVote.findUnique({
    where: { tripId_voterKey: { tripId, voterKey: pollVoterKey(userId) } },
    select: { id: true },
  });
  return row !== null;
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

export async function lockPoll(tripId: string): Promise<void> {
  await prisma.poll.upsert({
    where: { tripId },
    create: { tripId, locked: true, lockedAt: new Date(), generating: false },
    update: { locked: true, lockedAt: new Date(), generating: false },
  });
}

// ---------------------------------------------------------------------
// Admin token - the access-control boundary for "Lock & Generate Plan"
// (POST /api/trigger-jarvis). See lib/adminToken.ts for hashing and
// verification; this file only ever touches the stored hash, never a
// raw token.
// ---------------------------------------------------------------------

/**
 * Stores this trip's admin token hash AND its requested trip length,
 * minted/captured once by POST /api/poll/[id]/admin-token right after a
 * poll is created by voice (plugins/trip_planner.py's
 * _run_consensus_poll). An upsert since no Poll row exists yet at that
 * point in the normal flow - same pattern as lockPoll/saveDraft
 * elsewhere in this file. durationDays lives here (not on TripDraft,
 * which doesn't exist yet at this point for a consensus trip) precisely
 * so it's already in the database before generation ever needs to read
 * it - see the field's own comment in prisma/schema.prisma.
 */
export async function createPollAdminToken(
  tripId: string,
  tokenHash: string,
  durationDays: number
): Promise<void> {
  await prisma.poll.upsert({
    where: { tripId },
    create: { tripId, adminTokenHash: tokenHash, durationDays },
    update: { adminTokenHash: tokenHash, durationDays },
  });
}

/** "" if no Poll row exists yet, or none was ever issued for this trip. */
export async function getPollAdminTokenHash(tripId: string): Promise<string> {
  const row = await prisma.poll.findUnique({ where: { tripId } });
  return row?.adminTokenHash ?? "";
}

/**
 * The organizer's requested trip length - Prisma's own column default
 * (5) covers a trip created before this field existed; this function's
 * `?? 5` only covers the (normally impossible) case of no Poll row at
 * all, e.g. a stale/invalid tripId slipping past this function's caller.
 */
export async function getPollDurationDays(tripId: string): Promise<number> {
  const row = await prisma.poll.findUnique({ where: { tripId } });
  return row?.durationDays ?? 5;
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
