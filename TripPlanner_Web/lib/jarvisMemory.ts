import { NextResponse } from "next/server";
import { z } from "zod";
import {
  RELATION_STATUSES,
  type MemoryEntityChanges,
  type MemoryGraph,
  type MemoryLink,
  type MemoryNode,
  type RelationStatus,
} from "@/lib/jarvis/memory";
import { readIngestStatus, type DocumentMemory, type IngestState } from "@/lib/jarvis/documentMemory";
import { checkRateLimit } from "@/lib/rateLimit";

/**
 * The memory door: the Python brain's /memory endpoints (api_server.py, memory_graph.py),
 * which read and correct Jarvis's long-term memory of one person - the knowledge graph the
 * brain's memory tool keeps in Postgres (jarvis_memory). The Memory page reaches them only
 * through this app's /api/memory/* routes, which add the signed-in person.
 *
 *   GET    {JARVIS_MEMORY_URL}/graph                 -> { nodes: [{ id, name, type, description }],
 *                                                         links: [{ id, source, target, relation_type, weight }] }
 *   PATCH  {JARVIS_MEMORY_URL}/entities/<id>  { name?, type?, description? }
 *                                                     -> { node, reembedded }
 *   DELETE {JARVIS_MEMORY_URL}/entities/<id>         -> { deleted, links: [ids] }   its relations go with it
 *   PATCH  {JARVIS_MEMORY_URL}/relations/<id>  { status: "active" | "completed" | "archived" }
 *                                                     -> { link }
 *   DELETE {JARVIS_MEMORY_URL}/relations/<id>        -> { deleted }
 *   GET    {JARVIS_MEMORY_URL}/ingest-status/<job>   -> { job_id, state, source_files, saved_memories, count }
 *                                                     a Save to Memory job of the chat (fetchIngestStatus)
 *   errors: { code, detail }   401 no_user, 404 not_found, 409 name_taken, 400 invalid | me_locked |
 *           reserved | refused, 503 not_configured | unavailable | embedding_failed
 *
 * JARVIS_MEMORY_URL defaults to JARVIS_BRAIN_URL with its /chat swapped for /memory.
 *
 * WHOSE MEMORY. Every call names the person in the header X-Jarvis-User-Id (their User.id,
 * from the session - never from the browser). The brain maps it to the same memory its
 * tools use for that person (the owner's account to the owner's, which the voice shares),
 * and only ever reaches that one: someone else's entry is a 404.
 *
 * The brain's own error text (`detail`) never leaves this file - it can name the database
 * login. The page gets a fixed sentence per `code` instead (MEMORY_MESSAGES).
 */

const BRAIN_URL = process.env.JARVIS_BRAIN_URL ?? "http://127.0.0.1:8000/chat";
const MEMORY_URL = (process.env.JARVIS_MEMORY_URL ?? BRAIN_URL.replace(/\/chat\/?$/, "/memory")).replace(/\/$/, "");
// Reading is a couple of queries; an edit may wait for a new embedding (OpenAI, up to ~25 s).
const READ_TIMEOUT_MS = 15_000;
const EDIT_TIMEOUT_MS = 45_000;

export type MemoryFailureCode =
  | "not_found"
  | "name_taken"
  | "invalid"
  | "me_locked"
  | "reserved"
  | "refused"
  | "embedding_failed"
  | "not_configured"
  | "unavailable"
  | "offline";

/** What the page says for each failure. Fixed copy: never the brain's text. */
export const MEMORY_MESSAGES: Record<MemoryFailureCode, string> = {
  not_found: "That's no longer in Jarvis's memory - it may have been deleted already.",
  name_taken: "Another memory entry already has that name.",
  invalid: "That change wasn't accepted. Check the name, type, description or status.",
  me_locked: "This entry is you, so its name stays “me”.",
  reserved: "“me” is the name Jarvis keeps for you. Pick another name.",
  refused: "That looks like a password, card or ID number, so it wasn't saved.",
  embedding_failed: "Jarvis couldn't update its search index for this entry, so nothing was changed. Try again in a moment.",
  not_configured: "Jarvis's memory isn't set up on this server.",
  unavailable: "Jarvis's memory can't be reached right now. Try again in a moment.",
  offline: "Jarvis isn't running right now, so its memory can't be opened.",
};

const STATUS_OF: Record<MemoryFailureCode, number> = {
  not_found: 404,
  name_taken: 409,
  invalid: 400,
  me_locked: 400,
  reserved: 400,
  refused: 400,
  embedding_failed: 503,
  not_configured: 503,
  unavailable: 503,
  offline: 503,
};

export class JarvisMemoryError extends Error {
  readonly code: MemoryFailureCode;
  readonly status: number;

  constructor(code: MemoryFailureCode) {
    super(MEMORY_MESSAGES[code]);
    this.code = code;
    this.status = STATUS_OF[code];
  }
}

const Id = z.string().min(1).max(64);
const NodeSchema = z.object({
  id: Id,
  name: z.string().max(200),
  type: z.string().max(80),
  description: z.string().max(2000),
});
const LinkSchema = z.object({
  id: Id,
  source: Id,
  target: Id,
  relation_type: z.string().max(80),
  weight: z.number().finite(),
  // A brain from before relation statuses sends neither: every fact is then active, undated.
  status: z.enum(RELATION_STATUSES).catch("active").default("active"),
  updated_at: z.string().max(40).nullable().catch(null).default(null),
});
const GraphSchema = z.object({
  nodes: z.array(NodeSchema).max(5000),
  links: z.array(LinkSchema).max(20000),
});
const UpdatedSchema = z.object({ node: NodeSchema, reembedded: z.boolean() });
const DeletedEntitySchema = z.object({ deleted: Id, links: z.array(Id).max(20000) });
const DeletedRelationSchema = z.object({ deleted: Id });
const UpdatedRelationSchema = z.object({ link: LinkSchema });
const FailureSchema = z.object({ code: z.string() });
// The fields are checked one by one in readIngestStatus (lib/jarvis/documentMemory.ts); this only
// refuses an answer that is not a job at all.
const IngestStatusSchema = z.looseObject({ job_id: z.string().max(64), state: z.string().max(20) });

const KNOWN = new Set<string>(Object.keys(MEMORY_MESSAGES));

async function call<T>(
  path: string,
  userId: string,
  schema: z.ZodType<T>,
  init: { method?: string; body?: unknown; timeoutMs: number },
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${MEMORY_URL}${path}`, {
      method: init.method ?? "GET",
      headers: {
        "X-Jarvis-User-Id": userId,
        ...(init.body === undefined ? {} : { "Content-Type": "application/json" }),
      },
      body: init.body === undefined ? undefined : JSON.stringify(init.body),
      cache: "no-store",
      signal: AbortSignal.timeout(init.timeoutMs),
    });
  } catch {
    // Not running, not reachable from here (a Vercel deployment), or too slow.
    throw new JarvisMemoryError("offline");
  }

  const payload: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const failure = FailureSchema.safeParse(payload);
    const code = failure.success && KNOWN.has(failure.data.code) ? (failure.data.code as MemoryFailureCode) : null;
    if (code) throw new JarvisMemoryError(code);
    if (response.status === 404) throw new JarvisMemoryError("not_found"); // a brain from before these endpoints
    console.error(`Jarvis memory ${init.method ?? "GET"} ${path.split("/")[1]} answered ${response.status}`);
    throw new JarvisMemoryError("unavailable");
  }
  const parsed = schema.safeParse(payload);
  if (!parsed.success) {
    console.error(`Jarvis memory ${init.method ?? "GET"} ${path.split("/")[1]} sent an unexpected body`);
    throw new JarvisMemoryError("unavailable");
  }
  return parsed.data;
}

function segment(id: string): string {
  return encodeURIComponent(id);
}

/** The person's whole memory graph. */
export function fetchMemoryGraph(userId: string): Promise<MemoryGraph> {
  return call("/graph", userId, GraphSchema, { timeoutMs: READ_TIMEOUT_MS });
}

/** Changes an entry's name, type and/or description; the brain re-embeds it when its words change. */
export function updateMemoryEntity(
  userId: string,
  id: string,
  changes: MemoryEntityChanges,
): Promise<{ node: MemoryNode; reembedded: boolean }> {
  return call(`/entities/${segment(id)}`, userId, UpdatedSchema, {
    method: "PATCH",
    body: changes,
    timeoutMs: EDIT_TIMEOUT_MS,
  });
}

/** Deletes an entry and every relation it is in; answers the ids of those relations too. */
export function deleteMemoryEntity(userId: string, id: string): Promise<{ deleted: string; links: string[] }> {
  return call(`/entities/${segment(id)}`, userId, DeletedEntitySchema, { method: "DELETE", timeoutMs: READ_TIMEOUT_MS });
}

/**
 * How a Save to Memory job of the chat is going (the brain's GET /memory/ingest-status/<id>,
 * ingest_jobs.py): its state and, once saved, what was saved - checked (readIngestStatus). Only the
 * person's own job: anyone else's is not_found, like one the brain no longer knows (it restarted).
 */
export async function fetchIngestStatus(
  userId: string,
  jobId: string,
): Promise<{ state: IngestState; sourceFiles: string[]; savedMemories: DocumentMemory[] }> {
  return readIngestStatus(await call(`/ingest-status/${segment(jobId)}`, userId, IngestStatusSchema, { timeoutMs: READ_TIMEOUT_MS }));
}

/** Marks one relation active, completed or archived; its date moves to now. */
export function updateMemoryRelation(userId: string, id: string, status: RelationStatus): Promise<{ link: MemoryLink }> {
  return call(`/relations/${segment(id)}`, userId, UpdatedRelationSchema, {
    method: "PATCH",
    body: { status },
    timeoutMs: READ_TIMEOUT_MS,
  });
}

/** Deletes one relation; both of its entries stay. */
export function deleteMemoryRelation(userId: string, id: string): Promise<{ deleted: string }> {
  return call(`/relations/${segment(id)}`, userId, DeletedRelationSchema, { method: "DELETE", timeoutMs: READ_TIMEOUT_MS });
}

// ---- for the /api/memory routes ---------------------------------------------------------

export const MEMORY_SIGNED_OUT = "Sign in to see Jarvis's memory.";

/** The JSON a /api/memory route answers when the brain refused or failed: { error, code } and its status. */
export function memoryFailure(error: unknown, route: string): NextResponse {
  if (error instanceof JarvisMemoryError) {
    return NextResponse.json({ error: error.message, code: error.code }, { status: error.status });
  }
  console.error(`${route} failed:`, error);
  return NextResponse.json({ error: "Something went wrong. Please try again.", code: "server" }, { status: 500 });
}

// Edits can each cost an embedding on the owner's OpenAI key; a person fixing their memory by
// hand never needs more than this.
const WRITE_LIMIT = 60;
const WRITE_WINDOW_MS = 60_000;

/** null when the person may change their memory now, else the 429 to answer. */
export function memoryWriteLimit(userId: string): NextResponse | null {
  const limit = checkRateLimit(`memory-write:${userId}`, WRITE_LIMIT, WRITE_WINDOW_MS);
  if (limit.allowed) return null;
  return NextResponse.json(
    { error: "That's a lot of changes at once. Wait a moment and try again.", code: "rate_limited" },
    { status: 429, headers: { "Retry-After": String(limit.retryAfterSeconds) } },
  );
}

/** An id from the address is passed on only when it could be one (the brain's are UUIDs). */
export function plausibleId(id: string | undefined): id is string {
  return typeof id === "string" && /^[0-9a-fA-F-]{32,40}$/.test(id);
}
