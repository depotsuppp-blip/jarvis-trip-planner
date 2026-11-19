import Link from "next/link";
import { CircleAlert, Network } from "lucide-react";
import { memoryNoteText, type MemoryNote } from "@/lib/jarvis/memoryNote";
import { focusRing } from "./styles";

/**
 * The quiet line under a reply that says what Jarvis's long-term memory really did while
 * answering (lib/jarvis/memoryNote.ts) - the tool's own record, so "Got it, I'll remember"
 * with no pill under it means nothing was saved:
 *
 *   saved       "Saved to memory · 2 entries", a link to the Memory page (the facts it wrote
 *               are its title)
 *   confirmed   "Already in memory", the same link
 *   failed      "Couldn't save to memory" in Alert Red, not a link
 *
 * A 28px chip with a Hairline edge on Desk Mist, 12px type: it belongs to the reply above it
 * and never competes with it.
 */
export function MemoryPill({ note }: { note: MemoryNote }) {
  const text = memoryNoteText(note);

  if (note.status === "failed") {
    return (
      <p className="mt-2 inline-flex h-7 items-center gap-1.5 rounded-md border border-red-200 bg-red-50 px-2 text-xs font-medium text-red-700">
        <CircleAlert className="size-3.5 shrink-0" aria-hidden="true" />
        {text}
      </p>
    );
  }

  return (
    <Link
      href="/jarvis/memory"
      title={note.summary ? `${note.summary} - open Memory` : "Open Memory"}
      className={`mt-2 inline-flex h-7 max-w-full items-center gap-1.5 rounded-md border border-slate-200 bg-slate-50 px-2 text-xs font-medium text-slate-700 transition-colors duration-150 hover:border-slate-400 hover:text-slate-900 pointer-coarse:h-9 ${focusRing}`}
    >
      <Network
        className={`size-3.5 shrink-0 ${note.status === "saved" ? "text-emerald-700" : "text-slate-500"}`}
        aria-hidden="true"
      />
      <span className="truncate">{text}</span>
      <span className="sr-only">. Open Memory</span>
    </Link>
  );
}
