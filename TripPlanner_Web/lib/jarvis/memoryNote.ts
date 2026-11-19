/**
 * What Jarvis's memory really did during one turn - the pill under a reply in the hub
 * ("Saved to memory · 2 entries"). Pure: no React, no network; used by the server (to check
 * what the brain sent) and by the page (to draw it).
 *
 * The brain records every call of its memory tool's `remember` as it happens
 * (`memory_events` on the final event, see lib/jarvisBrain.ts): what was saved, whether it was
 * all already known, or that it failed. That record comes from the tool, not from the model's
 * words, so a reply that says "got it" without saving anything shows no pill - and one whose
 * save failed shows that it failed.
 *
 * The note is for the response it came with: like the spoken audio, it is not stored with the
 * message, so a session opened later shows its replies without pills.
 */

export type MemoryAction = "saved" | "confirmed" | "failed";

/** One `remember` call, as the brain reported it. */
export interface MemoryWrite {
  action: MemoryAction;
  /** Entries (people, places, projects...) added, or updated with a fuller description. */
  entries: number;
  /** Facts added, or whose status changed (a project marked completed). */
  facts: number;
  /** New mentions recognised as entries already in memory, and merged into them. */
  merged: number;
  /** The facts written, in a line ("Nok sister of me; me building Jarvis (completed)"). */
  summary: string;
}

/** A turn's memory writes, summed up for its reply. */
export interface MemoryNote {
  /** saved: something was written. confirmed: all of it was already known. failed: nothing could be saved. */
  status: MemoryAction;
  entries: number;
  facts: number;
  /** remember calls that failed alongside ones that saved. */
  failures: number;
  summary: string;
}

const MAX_EVENTS = 20;
const MAX_SUMMARY_CHARS = 240;
const ACTIONS: readonly MemoryAction[] = ["saved", "confirmed", "failed"];

function count(value: unknown): number {
  return typeof value === "number" && Number.isInteger(value) && value > 0 ? Math.min(value, 10_000) : 0;
}

function oneLine(value: unknown, max: number): string {
  if (typeof value !== "string") return "";
  const text = value.replace(/[\u0000-\u001f\u007f]+/g, " ").replace(/\s+/g, " ").trim();
  return text.length > max ? `${text.slice(0, max - 1).trimEnd()}…` : text;
}

/** The brain's `memory_events`, checked: well-formed writes only, at most MAX_EVENTS. */
export function readMemoryEvents(value: unknown): MemoryWrite[] {
  if (!Array.isArray(value)) return [];
  const writes: MemoryWrite[] = [];
  for (const item of value.slice(0, MAX_EVENTS)) {
    if (!item || typeof item !== "object") continue;
    const event = item as Record<string, unknown>;
    const action = ACTIONS.find((known) => known === event.action);
    if (!action) continue;
    writes.push({
      action,
      entries: count(event.entries),
      facts: count(event.facts),
      merged: count(event.merged),
      summary: oneLine(event.summary, MAX_SUMMARY_CHARS),
    });
  }
  return writes;
}

/** A turn's writes as one note for its reply, or null when memory was not written to at all. */
export function memoryNote(writes: MemoryWrite[]): MemoryNote | null {
  if (writes.length === 0) return null;
  const saved = writes.filter((write) => write.action === "saved");
  const failed = writes.filter((write) => write.action === "failed");
  const status: MemoryAction = saved.length ? "saved" : failed.length ? "failed" : "confirmed";
  const counted = status === "saved" ? saved : writes;
  return {
    status,
    entries: counted.reduce((sum, write) => sum + write.entries, 0),
    facts: counted.reduce((sum, write) => sum + write.facts, 0),
    failures: saved.length ? failed.length : 0,
    summary: oneLine(
      counted
        .map((write) => write.summary)
        .filter(Boolean)
        .join("; "),
      MAX_SUMMARY_CHARS,
    ),
  };
}

/** A note the page received (from its own server), checked again before it is drawn. */
export function readMemoryNote(value: unknown): MemoryNote | null {
  if (!value || typeof value !== "object") return null;
  const note = value as Record<string, unknown>;
  const status = ACTIONS.find((known) => known === note.status);
  if (!status) return null;
  return {
    status,
    entries: count(note.entries),
    facts: count(note.facts),
    failures: count(note.failures),
    summary: oneLine(note.summary, MAX_SUMMARY_CHARS),
  };
}

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** The pill's words: "Saved to memory · 2 entries", "Already in memory", "Couldn't save to memory". */
export function memoryNoteText(note: MemoryNote): string {
  if (note.status === "failed") return "Couldn't save to memory";
  if (note.status === "confirmed") return "Already in memory";
  const amount = note.entries
    ? plural(note.entries, "entry", "entries")
    : note.facts
      ? plural(note.facts, "fact", "facts")
      : "";
  const failures = note.failures ? ` · ${plural(note.failures, "save", "saves")} failed` : "";
  return `Saved to memory${amount ? ` · ${amount}` : ""}${failures}`;
}
