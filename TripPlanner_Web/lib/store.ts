/**
 * Mock "database" for the Trip Planner foundation.
 *
 * Backed by two flat JSON files under .data/ (created on first write) rather
 * than a bare in-memory array or object: Next.js's dev server reloads route
 * handler modules on file changes, which would silently wipe a plain
 * module-level array between edits. A file on disk survives that, and a
 * real database can replace these functions later without touching any
 * caller - every route handler only ever imports the functions below, never
 * the file paths themselves.
 */

import { promises as fs } from "fs";
import path from "path";

const DATA_DIR = path.join(process.cwd(), ".data");

async function ensureDataDir(): Promise<void> {
  await fs.mkdir(DATA_DIR, { recursive: true });
}

async function readJsonFile<T>(filename: string, fallback: T): Promise<T> {
  await ensureDataDir();
  const filePath = path.join(DATA_DIR, filename);
  try {
    const raw = await fs.readFile(filePath, "utf-8");
    return JSON.parse(raw) as T;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") {
      return fallback;
    }
    throw error;
  }
}

async function writeJsonFile<T>(filename: string, data: T): Promise<void> {
  await ensureDataDir();
  const filePath = path.join(DATA_DIR, filename);
  await fs.writeFile(filePath, JSON.stringify(data, null, 2), "utf-8");
}

/**
 * Serializes read-modify-write cycles per file within this process, so
 * two requests racing each other (e.g. a double-tap POST /api/poll/[id])
 * can't both read the same starting state and each write back a result
 * that drops the other's change. Only holds within one server
 * instance/process - a real database's transactions would be needed for
 * a cross-instance guarantee, which this mock store deliberately defers
 * (see the module docstring above).
 */
const writeQueues = new Map<string, Promise<unknown>>();

async function withFileLock<T>(filename: string, fn: () => Promise<T>): Promise<T> {
  const previous = writeQueues.get(filename) ?? Promise.resolve();
  const run = previous.then(fn, fn);
  writeQueues.set(
    filename,
    run.then(
      () => undefined,
      () => undefined
    )
  );
  return run;
}

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

const POLLS_FILE = "polls.json";

export async function getPollVotes(tripId: string): Promise<PollVote[]> {
  const all = await readJsonFile<Record<string, PollVote[]>>(POLLS_FILE, {});
  return all[tripId] ?? [];
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
function isSameVoter(a: PollVote, b: PollVote): boolean {
  if (a.lineUserId || b.lineUserId) {
    return a.lineUserId === b.lineUserId && a.lineUserId !== "";
  }
  return a.name.trim().toLowerCase() === b.name.trim().toLowerCase();
}

export async function addPollVote(
  tripId: string,
  vote: PollVote
): Promise<PollVote[]> {
  return withFileLock(POLLS_FILE, async () => {
    const all = await readJsonFile<Record<string, PollVote[]>>(POLLS_FILE, {});
    // One vote per voter per trip - a resubmission (a double-tap on the
    // button, or someone changing their mind and voting again) replaces
    // their previous entry instead of appending a duplicate. See
    // isSameVoter for what "per voter" means for an anonymous vote.
    const existing = (all[tripId] ?? []).filter((v) => !isSameVoter(v, vote));
    const votes = [...existing, vote];
    all[tripId] = votes;
    await writeJsonFile(POLLS_FILE, all);
    return votes;
  });
}

// ---------------------------------------------------------------------
// Solo draft board - one trip id maps to a single evolving text blob
// ---------------------------------------------------------------------

export interface TripDraft {
  text: string;
  updatedAt: string;
}

const DRAFTS_FILE = "drafts.json";

export async function getDraft(tripId: string): Promise<TripDraft | null> {
  const all = await readJsonFile<Record<string, TripDraft>>(DRAFTS_FILE, {});
  return all[tripId] ?? null;
}

export async function saveDraft(
  tripId: string,
  text: string
): Promise<TripDraft> {
  return withFileLock(DRAFTS_FILE, async () => {
    const all = await readJsonFile<Record<string, TripDraft>>(DRAFTS_FILE, {});
    const draft: TripDraft = { text, updatedAt: new Date().toISOString() };
    all[tripId] = draft;
    await writeJsonFile(DRAFTS_FILE, all);
    return draft;
  });
}
