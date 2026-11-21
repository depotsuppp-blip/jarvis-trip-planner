"use client";

import { useId, useState } from "react";
import Link from "next/link";
import { ArrowRight, ChevronDown, CircleAlert, CircleHelp, Hourglass, Sparkles } from "lucide-react";
import {
  documentIngestText,
  documentMemoryText,
  relationWords,
  type DocumentFact,
  type DocumentIngest,
  type DocumentMemory,
} from "@/lib/jarvis/documentMemory";
import { focusRing } from "./styles";

/**
 * A Save to Memory job that has not ended in saved facts (lib/jarvis/documentMemory.ts,
 * DocumentIngest): the brain reads the file AFTER it has answered, so the reply first carries this.
 * The DocumentMemoryPill's 28px chip, as plain text (nothing to open yet):
 *
 *   saving    an hourglass in Muted (a slow pulse; still under reduced motion) and
 *             "Saving key facts from CV.pdf…"
 *   nothing   the sparkle in Muted, "No new facts to save from CV.pdf"
 *   failed    an alert in rose-700, "Couldn't save CV.pdf to memory"
 *   unknown   a question mark in Muted, "Couldn't confirm what was saved from CV.pdf"
 *
 * Once facts are saved the DocumentMemoryPill ("Saved 6 facts from CV.pdf") takes its place. The
 * change is announced by MessageBubble's status line, not here.
 */
export function DocumentIngestPill({ ingest }: { ingest: DocumentIngest }) {
  const Icon = ingest.state === "saving" ? Hourglass : ingest.state === "nothing" ? Sparkles : ingest.state === "failed" ? CircleAlert : CircleHelp;
  return (
    <div className="mt-2 max-w-full" data-ingest-state={ingest.state}>
      <span className="inline-flex h-7 max-w-full items-center gap-1.5 rounded-md border border-slate-200 bg-slate-50 px-2 text-xs font-medium text-slate-700 pointer-coarse:h-9">
        <Icon
          className={`size-3.5 shrink-0 ${ingest.state === "failed" ? "text-rose-700" : "text-slate-500"} ${
            ingest.state === "saving" ? "motion-safe:animate-pulse" : ""
          }`}
          aria-hidden="true"
        />
        <span className="truncate">{documentIngestText(ingest)}</span>
      </span>
    </div>
  );
}

const STATUS_LABEL: Record<DocumentFact["status"], string> = {
  active: "Active",
  completed: "Done",
  archived: "Archived",
};

/**
 * What Save to Memory read into Jarvis's long-term memory from the file sent with the question
 * (lib/jarvis/documentMemory.ts), under the reply - one per file that saved anything:
 *
 *   folded   "Saved 6 facts from Spec_v2.pdf", a 28px chip like the MemoryPill (Hairline edge,
 *            Desk Mist, 6px, 12px 500) with a sparkle in Go Green and a chevron - a button
 *   open     under it, a white ruled panel: the facts as they were saved, one line each
 *            ("Atlas · deadline is · 2027-03-01"), a fact the document gives as done or ended with its
 *            status, then "Replaced" - the older values this file's new ones archived, struck
 *            through - and "View in Memory Graph →", a link to the Memory page
 *
 * Folded by default: the reply stays the thing to read. Only on a reply that just arrived (it is not
 * stored, like the memory pill).
 */
export function DocumentMemoryPill({ memory }: { memory: DocumentMemory }) {
  const [open, setOpen] = useState(false);
  const panel = useId();
  const stated = memory.facts.filter((fact) => !fact.replaced);
  const replaced = memory.facts.filter((fact) => fact.replaced);
  const text = documentMemoryText(memory);

  return (
    <div className="mt-2 max-w-full">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        aria-controls={panel}
        title={open ? "Hide the saved facts" : "Show the saved facts"}
        className={`inline-flex h-7 max-w-full items-center gap-1.5 rounded-md border border-slate-200 bg-slate-50 px-2 text-xs font-medium text-slate-700 transition-colors duration-150 hover:border-slate-400 hover:text-slate-900 aria-expanded:border-slate-400 aria-expanded:text-slate-900 pointer-coarse:h-9 ${focusRing}`}
      >
        <Sparkles className="size-3.5 shrink-0 text-emerald-700" aria-hidden="true" />
        <span className="truncate">{text}</span>
        <ChevronDown
          className={`size-3.5 shrink-0 text-slate-500 transition-transform duration-150 motion-reduce:transition-none ${open ? "rotate-180" : ""}`}
          aria-hidden="true"
        />
      </button>

      {open && (
        <div
          id={panel}
          className="mt-1.5 max-w-xl rounded-md border border-slate-200 bg-white px-3 py-2.5 text-xs leading-5 text-slate-700"
        >
          <FactList facts={stated} label={`Facts saved from ${memory.sourceFile}`} />
          {replaced.length > 0 && (
            <>
              <p className="mt-2.5 text-[11px] font-semibold uppercase tracking-wide text-slate-600">Replaced</p>
              <FactList facts={replaced} label="Older values this file replaced" replaced />
            </>
          )}
          <Link
            href="/jarvis/memory"
            className={`mt-2.5 inline-flex items-center gap-1 rounded font-semibold text-slate-900 underline-offset-2 hover:underline ${focusRing}`}
          >
            View in Memory Graph
            <ArrowRight className="size-3.5" aria-hidden="true" />
          </Link>
        </div>
      )}
    </div>
  );
}

function FactList({ facts, label, replaced = false }: { facts: DocumentFact[]; label: string; replaced?: boolean }) {
  if (facts.length === 0) return null;
  return (
    <ul aria-label={label} className="mt-1 space-y-1 first:mt-0">
      {facts.map((fact, index) => (
        <li key={`${fact.subject}|${fact.relation}|${fact.object}|${index}`} className="flex min-w-0 flex-wrap items-baseline gap-x-1.5 [overflow-wrap:anywhere]">
          <span className={replaced ? "text-slate-600 line-through decoration-slate-400" : ""}>
            <span className={`font-medium ${replaced ? "" : "text-slate-900"}`}>{fact.subject}</span>
            <span className="text-slate-600"> · {relationWords(fact.relation)} · </span>
            <span className={`font-medium ${replaced ? "" : "text-slate-900"}`}>{fact.object}</span>
          </span>
          {replaced ? (
            <span className="sr-only">(replaced, now archived)</span>
          ) : (
            fact.status !== "active" && (
              <span
                className={`rounded border px-1 text-[11px] leading-4 ${
                  fact.status === "completed" ? "border-emerald-200 text-emerald-800" : "border-dashed border-slate-300 text-slate-600"
                }`}
              >
                {STATUS_LABEL[fact.status]}
              </span>
            )
          )}
        </li>
      ))}
    </ul>
  );
}
