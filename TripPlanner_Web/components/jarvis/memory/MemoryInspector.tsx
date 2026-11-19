"use client";

import { useEffect, useId, useRef, useState } from "react";
import { Check, Pencil, Trash2, X } from "lucide-react";
import {
  displayName,
  groupInfo,
  groupOf,
  isMe,
  MEMORY_LIMITS,
  relationLabel,
  type MemoryEntityChanges,
  type MemoryNode,
} from "@/lib/jarvis/memory";
import { focusRing, iconButton, thinScroll } from "../styles";

/** One relation of the open entry, with both ends resolved to names. */
export interface InspectorRelation {
  id: string;
  source: MemoryNode;
  target: MemoryNode;
  relationType: string;
  weight: number;
}

export type ActionOutcome = { ok: true } | { ok: false; message: string };

interface MemoryInspectorProps {
  node: MemoryNode;
  relations: InspectorRelation[];
  /** Every type already in memory, offered while typing one. */
  knownTypes: string[];
  onSave: (changes: MemoryEntityChanges) => Promise<ActionOutcome>;
  onDelete: () => Promise<ActionOutcome>;
  onDeleteRelation: (id: string) => Promise<ActionOutcome>;
  /** Opens another entry (a relation's other end). */
  onOpen: (id: string) => void;
  onClose: () => void;
}

const CONFIRM_MS = 4000;
const SAVED_MS = 2000;

const field =
  "mt-1.5 block w-full rounded-md border border-slate-300 bg-white px-3 text-[15px] text-slate-900 placeholder:text-slate-500 hover:border-slate-400 focus-visible:border-slate-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-rose-600 disabled:cursor-not-allowed disabled:bg-slate-50 disabled:text-slate-600";
const primary = `inline-flex h-10 items-center justify-center gap-2 rounded-md bg-slate-900 px-4 text-sm font-semibold text-white transition-[background-color,scale] duration-150 hover:bg-slate-800 active:scale-[0.98] disabled:cursor-wait disabled:opacity-60 ${focusRing}`;
const secondary = `inline-flex h-10 items-center justify-center gap-2 rounded-md border border-slate-300 bg-white px-4 text-sm font-semibold text-slate-900 transition-[border-color,scale] duration-150 hover:border-slate-900 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-60 ${focusRing}`;
const danger = `inline-flex h-10 items-center justify-center gap-2 rounded-md px-3 text-sm font-semibold text-red-700 transition-colors duration-150 hover:bg-red-50 active:scale-[0.98] disabled:cursor-wait disabled:opacity-60 ${focusRing}`;

/**
 * The side panel for one memory entry (the Memory page; a bottom sheet on a phone): what it
 * is, what it is linked to, and the ways to correct it.
 *
 *   - Edit puts its name, type and description into a form; Save sends only what changed.
 *     The person's own entry ("me") keeps its name.
 *   - Delete asks first, in words that say how many relations go with it.
 *   - Each relation reads as a sentence ("Nok - sister of - me"); the other end opens that
 *     entry, and the x removes the relation after a second press ("Remove?", 4 seconds),
 *     like a message's Delete in the chat.
 *
 * Escape cancels an edit or a pending delete, else closes the panel.
 */
export function MemoryInspector({
  node,
  relations,
  knownTypes,
  onSave,
  onDelete,
  onDeleteRelation,
  onOpen,
  onClose,
}: MemoryInspectorProps) {
  const titleId = useId();
  const title = useRef<HTMLHeadingElement | null>(null);
  const [mode, setMode] = useState<"view" | "edit" | "confirm-delete">("view");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const group = groupInfo(groupOf(node.type));

  // Opened (the page mounts one per entry - key={id} - so another entry starts afresh):
  // the screen reader hears its name.
  useEffect(() => {
    title.current?.focus({ preventScroll: true });
  }, []);

  useEffect(() => {
    if (!saved) return;
    const timer = window.setTimeout(() => setSaved(false), SAVED_MS);
    return () => window.clearTimeout(timer);
  }, [saved]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      // The search field's own Escape (closing its list) comes first.
      if ((event.target as HTMLElement | null)?.closest?.("[data-memory-search]")) return;
      event.preventDefault();
      if (mode !== "view") {
        setMode("view");
        setError(null);
      } else {
        onClose();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [mode, onClose]);

  async function remove() {
    setBusy(true);
    setError(null);
    const outcome = await onDelete();
    setBusy(false);
    if (!outcome.ok) setError(outcome.message);
  }

  const count = relations.length;

  return (
    <aside
      aria-labelledby={titleId}
      data-memory-sheet=""
      className="fixed inset-x-0 bottom-0 z-30 flex max-h-[70svh] flex-col border-t border-slate-200 bg-white md:static md:z-auto md:max-h-none md:w-[22rem] md:shrink-0 md:border-l md:border-t-0"
    >
      <div className="flex h-12 shrink-0 items-center justify-between border-b border-slate-200 pl-4 pr-2">
        <span className="flex min-w-0 items-center gap-2 text-xs font-medium text-slate-600">
          <span aria-hidden="true" className="block size-2.5 shrink-0 rounded-full" style={{ background: group.color }} />
          <span className="truncate">{group.label}</span>
        </span>
        <button type="button" onClick={onClose} aria-label="Close details" className={iconButton}>
          <X className="size-5" aria-hidden="true" />
        </button>
      </div>

      <div className={`min-h-0 flex-1 overflow-y-auto px-4 pb-6 pt-4 ${thinScroll}`}>
        <h2
          id={titleId}
          ref={title}
          tabIndex={-1}
          className="break-words text-[17px] font-bold leading-6 tracking-tight text-slate-900 focus:outline-none"
        >
          {displayName(node)}
        </h2>

        {mode === "edit" ? (
          <EditForm
            node={node}
            knownTypes={knownTypes}
            onCancel={() => {
              setMode("view");
              setError(null);
            }}
            onSave={async (changes) => {
              const outcome = await onSave(changes);
              if (outcome.ok) {
                setMode("view");
                setSaved(true);
              }
              return outcome;
            }}
          />
        ) : (
          <>
            <p className="mt-1 text-[13px] text-slate-600">{node.type || "No type"}</p>
            <p className="mt-3 whitespace-pre-wrap break-words text-sm leading-6 text-slate-700">
              {node.description || <span className="text-slate-500">No description.</span>}
            </p>

            {mode === "confirm-delete" ? (
              <div role="group" aria-label="Confirm delete" className="mt-4 rounded-md border border-red-200 bg-red-50 p-3">
                <p className="text-sm leading-6 text-red-800">
                  Delete <strong className="font-semibold">{displayName(node)}</strong>
                  {count > 0 ? ` and its ${count} relation${count === 1 ? "" : "s"}` : ""}? Jarvis will forget{" "}
                  {count > 0 ? "them" : "it"}.
                </p>
                <div className="mt-3 flex flex-wrap gap-2">
                  <button
                    type="button"
                    autoFocus
                    disabled={busy}
                    onClick={() => void remove()}
                    className={`inline-flex h-10 items-center gap-2 rounded-md bg-red-700 px-4 text-sm font-semibold text-white transition-[background-color,scale] duration-150 hover:bg-red-800 active:scale-[0.98] disabled:cursor-wait disabled:opacity-60 ${focusRing}`}
                  >
                    <Trash2 className="size-4" aria-hidden="true" />
                    {busy ? "Deleting…" : "Delete"}
                  </button>
                  <button type="button" disabled={busy} onClick={() => setMode("view")} className={secondary}>
                    Cancel
                  </button>
                </div>
              </div>
            ) : (
              <div className="mt-4 flex flex-wrap items-center gap-2">
                <button type="button" onClick={() => setMode("edit")} className={secondary}>
                  <Pencil className="size-4" aria-hidden="true" />
                  Edit
                </button>
                <button type="button" onClick={() => setMode("confirm-delete")} className={danger}>
                  <Trash2 className="size-4" aria-hidden="true" />
                  Delete
                </button>
                <span role="status" className="ml-1 inline-flex items-center gap-1 text-sm font-medium text-emerald-700">
                  {saved && (
                    <>
                      <Check className="size-4" aria-hidden="true" />
                      Saved
                    </>
                  )}
                </span>
              </div>
            )}
          </>
        )}

        {error && (
          <p role="alert" className="mt-3 text-sm leading-5 text-red-700">
            {error}
          </p>
        )}

        <section aria-labelledby={`${titleId}-relations`} className="mt-6 border-t border-slate-200 pt-4">
          <h3 id={`${titleId}-relations`} className="flex items-baseline gap-2 text-[13px] font-semibold text-slate-800">
            Relations
            <span className="text-xs font-normal tabular-nums text-slate-600">{count}</span>
          </h3>
          {count === 0 ? (
            <p className="mt-2 text-sm leading-5 text-slate-600">Not linked to anything else.</p>
          ) : (
            <ul className="mt-1">
              {relations.map((relation) => (
                <RelationRow
                  key={relation.id}
                  relation={relation}
                  selfId={node.id}
                  onOpen={onOpen}
                  onRemove={() => onDeleteRelation(relation.id)}
                />
              ))}
            </ul>
          )}
        </section>
      </div>
    </aside>
  );
}

function EditForm({
  node,
  knownTypes,
  onSave,
  onCancel,
}: {
  node: MemoryNode;
  knownTypes: string[];
  onSave: (changes: MemoryEntityChanges) => Promise<ActionOutcome>;
  onCancel: () => void;
}) {
  const id = useId();
  const me = isMe(node);
  const [name, setName] = useState(node.name);
  const [type, setType] = useState(node.type);
  const [description, setDescription] = useState(node.description);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const first = useRef<HTMLInputElement | null>(null);

  useEffect(() => first.current?.focus(), []);

  const changes: MemoryEntityChanges = {};
  if (!me && name.trim() !== node.name) changes.name = name.trim();
  if (type.trim() !== node.type) changes.type = type.trim();
  if (description.trim() !== node.description) changes.description = description.trim();
  const changed = Object.keys(changes).length > 0;
  const nameMissing = !me && !name.trim();

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!changed || nameMissing || busy) return;
    setBusy(true);
    setError(null);
    const outcome = await onSave(changes);
    setBusy(false);
    if (!outcome.ok) setError(outcome.message);
  }

  return (
    <form onSubmit={submit} className="mt-3 space-y-4" aria-describedby={error ? `${id}-error` : undefined}>
      <div>
        <label htmlFor={`${id}-name`} className="block text-[13px] font-semibold text-slate-800">
          Name
        </label>
        <input
          id={`${id}-name`}
          ref={me ? undefined : first}
          value={name}
          onChange={(event) => setName(event.target.value)}
          maxLength={MEMORY_LIMITS.name}
          disabled={me}
          aria-describedby={me ? `${id}-me` : undefined}
          aria-invalid={nameMissing || undefined}
          autoComplete="off"
          className={`${field} h-10`}
        />
        {me && (
          <p id={`${id}-me`} className="mt-1.5 text-xs leading-5 text-slate-600">
            This entry is you, so its name stays &ldquo;me&rdquo;.
          </p>
        )}
      </div>

      <div>
        <label htmlFor={`${id}-type`} className="block text-[13px] font-semibold text-slate-800">
          Type
        </label>
        <input
          id={`${id}-type`}
          ref={me ? first : undefined}
          value={type}
          onChange={(event) => setType(event.target.value)}
          maxLength={MEMORY_LIMITS.type}
          list={`${id}-types`}
          placeholder="person, place, project…"
          autoComplete="off"
          className={`${field} h-10`}
        />
        <datalist id={`${id}-types`}>
          {knownTypes.map((known) => (
            <option key={known} value={known} />
          ))}
        </datalist>
      </div>

      <div>
        <div className="flex items-baseline justify-between">
          <label htmlFor={`${id}-description`} className="block text-[13px] font-semibold text-slate-800">
            Description
          </label>
          <span className="text-xs tabular-nums text-slate-600" aria-hidden="true">
            {description.length}/{MEMORY_LIMITS.description}
          </span>
        </div>
        <textarea
          id={`${id}-description`}
          value={description}
          onChange={(event) => setDescription(event.target.value)}
          maxLength={MEMORY_LIMITS.description}
          rows={4}
          className={`${field} resize-y py-2 leading-6 ${thinScroll}`}
        />
        <p className="mt-1.5 text-xs leading-5 text-slate-600">Jarvis finds this entry by its name, type and description.</p>
      </div>

      {error && (
        <p id={`${id}-error`} role="alert" className="text-sm leading-5 text-red-700">
          {error}
        </p>
      )}

      <div className="flex flex-wrap gap-2">
        <button type="submit" disabled={busy || !changed || nameMissing} className={`${primary} disabled:cursor-not-allowed`}>
          {busy ? "Saving…" : "Save"}
        </button>
        <button type="button" onClick={onCancel} disabled={busy} className={secondary}>
          Cancel
        </button>
      </div>
    </form>
  );
}

function RelationRow({
  relation,
  selfId,
  onOpen,
  onRemove,
}: {
  relation: InspectorRelation;
  selfId: string;
  onOpen: (id: string) => void;
  onRemove: () => Promise<ActionOutcome>;
}) {
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!armed) return;
    const timer = window.setTimeout(() => setArmed(false), CONFIRM_MS);
    return () => window.clearTimeout(timer);
  }, [armed]);

  const sentence = `${displayName(relation.source)} ${relationLabel(relation.relationType)} ${displayName(relation.target)}`;

  async function press() {
    if (!armed) {
      setArmed(true);
      return;
    }
    setBusy(true);
    setError(null);
    const outcome = await onRemove();
    // A removed row unmounts; only a failure is still here to say so.
    if (!outcome.ok) {
      setBusy(false);
      setArmed(false);
      setError(outcome.message);
    }
  }

  const end = (node: MemoryNode) =>
    node.id === selfId ? (
      <span className="font-semibold text-slate-900">{displayName(node)}</span>
    ) : (
      <button
        type="button"
        onClick={() => onOpen(node.id)}
        className={`rounded-sm font-semibold text-slate-900 underline decoration-slate-300 underline-offset-2 hover:decoration-slate-900 ${focusRing}`}
      >
        {displayName(node)}
      </button>
    );

  return (
    <li className="border-b border-slate-100 py-2.5 last:border-b-0">
      <div className="flex items-start gap-2">
        <p className="min-w-0 flex-1 break-words text-sm leading-6 text-slate-700">
          {end(relation.source)} <span className="text-slate-600">{relationLabel(relation.relationType)}</span>{" "}
          {end(relation.target)}
          {relation.weight > 1 && (
            <span className="ml-2 whitespace-nowrap text-xs tabular-nums text-slate-600">told {relation.weight}×</span>
          )}
        </p>
        <button
          type="button"
          onClick={() => void press()}
          disabled={busy}
          aria-label={armed ? `Confirm: remove "${sentence}"` : `Remove "${sentence}"`}
          title={armed ? "Press again to remove" : "Remove this relation"}
          className={`inline-flex h-8 min-w-8 shrink-0 items-center justify-center gap-1.5 rounded-md text-xs font-medium transition-[background-color,color] duration-150 disabled:cursor-wait disabled:opacity-50 pointer-coarse:h-10 pointer-coarse:min-w-10 ${
            armed ? "bg-red-50 px-2 text-red-700" : "text-slate-500 hover:bg-slate-100 hover:text-slate-900"
          } ${focusRing}`}
        >
          <X className="size-4 shrink-0" aria-hidden="true" />
          {armed && <span aria-hidden="true">{busy ? "Removing…" : "Remove?"}</span>}
        </button>
      </div>
      {error && (
        <p role="alert" className="mt-1 text-xs leading-5 text-red-700">
          {error}
        </p>
      )}
    </li>
  );
}
