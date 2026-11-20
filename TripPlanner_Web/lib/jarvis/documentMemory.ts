/**
 * Save to Memory: what Jarvis saved into its long-term memory from a document the person attached
 * with the dock's "Save to Memory" switch on - the collapsible pill under the reply ("Saved 6 facts
 * from Spec_v2.pdf"). Pure: no React, no network; used by the server (to check what the brain sent)
 * and by the page (to check it again before drawing it).
 *
 * The brain reads the file's few high-signal facts - projects, people, the stack, deadlines,
 * budgets, constraints; at most 10 - into the person's memory graph with the file's name on each
 * (memory_ingest.py), side by side with answering the message, and reports them on its final event
 * as `saved_memories` (lib/jarvisBrain.ts). A fact a newer document replaced - Spec_v1.pdf's
 * deadline when Spec_v2.pdf gives a new one - is listed too, archived and marked `replaced`.
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
