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
 * Whether two vote entries represent the same voter, for dedup on
 * resubmission. Verified identities (lineUserId set) only ever match
 * the SAME verified id - a shared display name is never enough to
 * overwrite a real voter's entry, since name is spoofable and
 * lineUserId isn't. Two anonymous entries (no lineUserId on either
 * side) are matched by name instead, since that self-reported string
 * is the only identity an anonymous submission has.
 */
function isSameVoterName(a: string, b: string): boolean {
  return a.trim().toLowerCase() === b.trim().toLowerCase();
}

export async function addPollVote(
  tripId: string,
  vote: PollVote
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
    } else {
      const anonymousRows = await tx.pollVote.findMany({
        where: { tripId, lineUserId: "" },
        select: { id: true, name: true },
      });
      const staleIds = anonymousRows
        .filter((row) => isSameVoterName(row.name, vote.name))
        .map((row) => row.id);
      if (staleIds.length > 0) {
        await tx.pollVote.deleteMany({ where: { id: { in: staleIds } } });
      }
    }

    await tx.pollVote.create({
      data: {
        tripId,
        name: vote.name,
        lineUserId: vote.lineUserId,
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
