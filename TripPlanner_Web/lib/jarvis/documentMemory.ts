/**
 * Save to Memory: what Jarvis saved into its long-term memory from a document the person attached
 * with the dock's "Save to Memory" switch on - the collapsible pill under the reply ("Saved 6 facts
 * from Spec_v2.pdf"). Pure: no React, no network; used by the server (to check what the brain sent)
 * and by the page (to check it again before drawing it).
 *
 * The brain reads the file's few high-signal facts - projects, people, the stack, deadlines,
 * budgets, constraints; at most 10 - into the person's memory graph with the file's name on each
 * (memory_ingest.py), in a background job once it has answered (ingest_jobs.py): the reply comes
 * with the job (DocumentIngest, below), and the page asks how it went until the facts are saved
 * (GET /api/memory/ingest-status/<id>). A fact a newer document replaced - Spec_v1.pdf's
 * deadline when Spec_v2.pdf gives a new one - is listed too, archived and marked `replaced`.
 * (A brain from before the jobs reports the saved facts on its final event, `saved_memories`.)
 *
 * Like the memory note, it is for the response it came with: it is not stored with the message.
 */

export type FactStatus = "active" | "completed" | "archived";

/** One fact as it was saved: subject - relation - object. */
export interface DocumentFact {
  subject: string;
  /** snake_case, as memory keeps it ("deadline_is"); relationWords() reads it out. */
  relation: string;
  object: string;
  status: FactStatus;
  /** An older value this document's new one replaced (now archived), not a fact the document states. */
  replaced?: boolean;
}

/** What one attached file added to memory. */
export interface DocumentMemory {
  /** The file's name, as memory cites it. */
  sourceFile: string;
  /** Facts the document stated that were saved (the replaced values are not counted). */
  count: number;
  facts: DocumentFact[];
}

// The brain saves at most 10 facts per file, each of which may archive an older value; the rest is headroom.
const MAX_FILES = 5;
const MAX_FACTS = 30;
const MAX_COUNT = 50;
const MAX_NAME_CHARS = 200;
const MAX_PART_CHARS = 160;
const STATUSES: readonly FactStatus[] = ["active", "completed", "archived"];

function oneLine(value: unknown, max: number): string {
  if (typeof value !== "string") return "";
  const text = value.replace(/[\u0000-\u001f\u007f]+/g, " ").replace(/\s+/g, " ").trim();
  return text.length > max ? `${text.slice(0, max - 1).trimEnd()}…` : text;
}

function readFact(value: unknown): DocumentFact | null {
  if (!value || typeof value !== "object") return null;
  const item = value as Record<string, unknown>;
  const subject = oneLine(item.subject, MAX_PART_CHARS);
  const relation = oneLine(item.relation, MAX_PART_CHARS);
  const object = oneLine(item.object, MAX_PART_CHARS);
  const status = STATUSES.find((known) => known === item.status);
  if (!subject || !relation || !object || !status) return null;
  return { subject, relation, object, status, ...(item.replaced === true ? { replaced: true } : {}) };
}

function readOne(item: Record<string, unknown>, nameKey: "source_file" | "sourceFile"): DocumentMemory | null {
  const sourceFile = oneLine(item[nameKey], MAX_NAME_CHARS);
  const count = item.count;
  if (!sourceFile || typeof count !== "number" || !Number.isInteger(count) || count < 1) return null;
  const facts = Array.isArray(item.facts)
    ? item.facts
        .slice(0, MAX_FACTS)
        .map(readFact)
        .filter((fact): fact is DocumentFact => fact !== null)
    : [];
  return { sourceFile, count: Math.min(count, MAX_COUNT), facts };
}

function readList(value: unknown, nameKey: "source_file" | "sourceFile"): DocumentMemory[] {
  if (!Array.isArray(value)) return [];
  const memories: DocumentMemory[] = [];
  for (const item of value.slice(0, MAX_FILES)) {
    if (!item || typeof item !== "object") continue;
    const memory = readOne(item as Record<string, unknown>, nameKey);
    if (memory) memories.push(memory);
  }
  return memories;
}

/** The brain's `saved_memories` (snake_case), checked: files that saved at least one fact, well-formed facts only. */
export function readSavedMemories(value: unknown): DocumentMemory[] {
  return readList(value, "source_file");
}

/** What the page received from its own server (`savedMemories`), checked again before it is drawn. */
export function readDocumentMemories(value: unknown): DocumentMemory[] {
  return readList(value, "sourceFile");
}

/** The pill's words: "Saved 6 facts from Spec_v2.pdf". */
export function documentMemoryText(memory: DocumentMemory): string {
  return `Saved ${memory.count} ${memory.count === 1 ? "fact" : "facts"} from ${memory.sourceFile}`;
}

/** A relation read out: "deadline_is" -> "deadline is". */
export function relationWords(relation: string): string {
  return relation.replace(/_+/g, " ").trim();
}

// ---- the background job (the brain's ingest_jobs.py) -------------------------------------------

/**
 * How a Save to Memory job is going. The brain reads the file AFTER it has answered (production,
 * 2026-11-21: an 11-page PDF held the chat 47 s), so the reply arrives with the job's id and the page
 * asks GET /api/memory/ingest-status/<id> until it is done:
 *
 *   saving    being read and saved           "Saving key facts from CV.pdf…"
 *   saved     facts written                  the DocumentMemoryPill(s) take its place
 *   nothing   read, nothing new worth keeping "No new facts to save from CV.pdf"
 *   failed    could not be read or saved     "Couldn't save CV.pdf to memory"
 *   unknown   the brain no longer knows it (restarted), or stopped answering
 *                                            "Couldn't confirm what was saved from CV.pdf"
 */
export type IngestState = "saving" | "saved" | "nothing" | "failed" | "unknown";

export interface DocumentIngest {
  jobId: string;
  /** The files' names, as memory cites them. */
  sourceFiles: string[];
  state: IngestState;
}

/** The brain's job ids (secrets.token_urlsafe): letters, digits, "-" and "_". */
export const INGEST_JOB_ID = /^[A-Za-z0-9_-]{8,64}$/;
const INGEST_STATES: readonly IngestState[] = ["saving", "saved", "nothing", "failed", "unknown"];

function fileNames(value: unknown): string[] {
  return Array.isArray(value)
    ? value
        .slice(0, MAX_FILES)
        .map((name) => oneLine(name, MAX_NAME_CHARS))
        .filter(Boolean)
    : [];
}

/** The brain's final event (`ingest_job_id`, `ingest_source_files`) as a job just started, or null. */
export function readIngestStart(jobId: unknown, sourceFiles: unknown): DocumentIngest | null {
  if (typeof jobId !== "string" || !INGEST_JOB_ID.test(jobId)) return null;
  return { jobId, sourceFiles: fileNames(sourceFiles), state: "saving" };
}

/** What the page received from its own server (`ingest`), checked again before it is drawn. */
export function readDocumentIngest(value: unknown): DocumentIngest | null {
  if (!value || typeof value !== "object") return null;
  const item = value as Record<string, unknown>;
  const started = readIngestStart(item.jobId, item.sourceFiles);
  if (!started) return null;
  const state = INGEST_STATES.find((known) => known === item.state) ?? "saving";
  return { ...started, state };
}

/** GET /memory/ingest-status's answer (snake_case, from the brain), checked: the state and what was saved. */
export function readIngestStatus(value: unknown): { state: IngestState; sourceFiles: string[]; savedMemories: DocumentMemory[] } {
  const item = value && typeof value === "object" ? (value as Record<string, unknown>) : {};
  const savedMemories = readSavedMemories(item.saved_memories);
  // "cancelled" (the turn never answered) and anything unknown read as a failure.
  let state: IngestState = item.state === "saving" || item.state === "nothing" ? item.state : item.state === "saved" ? "saved" : "failed";
  // Saved, but nothing the page can draw: say so rather than show an empty pill.
  if (state === "saved" && savedMemories.length === 0) state = "nothing";
  return { state, sourceFiles: fileNames(item.source_files), savedMemories };
}

/** The pill's words for a job that has not ended in saved facts. */
export function documentIngestText(ingest: DocumentIngest): string {
  const name = ingest.sourceFiles.length === 1 ? ingest.sourceFiles[0] : ingest.sourceFiles.length > 1 ? `${ingest.sourceFiles.length} files` : "the file";
  switch (ingest.state) {
    case "saving":
      return `Saving key facts from ${name}…`;
    case "nothing":
      return `No new facts to save from ${name}`;
    case "failed":
      return `Couldn't save ${name} to memory`;
    default:
      return `Couldn't confirm what was saved from ${name}`;
  }
}
