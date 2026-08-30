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
  // One vote per voter per trip - a resubmission (a double-tap on the
  // button, or someone changing their mind and voting again) replaces
  // their previous entry instead of appending a duplicate. Delete +
  // create run in one DB transaction so a concurrent read never
  // observes a voter with zero or two rows.
  await prisma.$transaction(async (tx) => {
    if (vote.lineUserId) {
      await tx.pollVote.deleteMany({
        where: { tripId, lineUserId: vote.lineUserId },
      });
    } else if (anonId) {
      await tx.pollVote.deleteMany({
        where: { tripId, lineUserId: "", anonId },
      });
    }
    // No lineUserId AND no anonId (e.g. an old cached client that
    // predates anonId, or localStorage unavailable) - nothing to key a
    // dedup lookup off, so this submission is simply never matched
    // against a prior one. It still inserts below like a first-time
    // vote, rather than being rejected.

    await tx.pollVote.create({
      data: {
        tripId,
        name: vote.name,
        lineUserId: vote.lineUserId,
        anonId: vote.lineUserId ? "" : anonId,
        startDate: vote.startDate,
        endDate: vote.endDate,
        wishlist: vote.wishlist,
        vibes: vote.vibes,
        submittedAt: new Date(vote.submittedAt),
      },
    });
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
