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

// ---------------------------------------------------------------------
// Consensus poll votes - one trip id maps to a list of friends' entries
// ---------------------------------------------------------------------

export interface PollVote {
  name: string;
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

export async function addPollVote(
  tripId: string,
  vote: PollVote
): Promise<PollVote[]> {
  const all = await readJsonFile<Record<string, PollVote[]>>(POLLS_FILE, {});
  const votes = [...(all[tripId] ?? []), vote];
  all[tripId] = votes;
  await writeJsonFile(POLLS_FILE, all);
  return votes;
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
  const all = await readJsonFile<Record<string, TripDraft>>(DRAFTS_FILE, {});
  const draft: TripDraft = { text, updatedAt: new Date().toISOString() };
  all[tripId] = draft;
  await writeJsonFile(DRAFTS_FILE, all);
  return draft;
}
