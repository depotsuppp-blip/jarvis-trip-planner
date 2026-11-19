"use client";

import { useEffect, useId, useMemo, useRef, useState } from "react";
import { ArrowLeft, Check, ChevronUp, Pencil, Trash2, X } from "lucide-react";
import {
  displayName,
  ENTITY_TYPES,
  groupInfo,
  groupOf,
  isMe,
  MEMORY_LIMITS,
  RELATION_STATUS_LABEL,
  RELATION_STATUSES,
  relationLabel,
  TYPE_GROUPS,
  type MemoryEntityChanges,
  type MemoryNode,
  type RelationStatus,
  type TypeGroupId,
} from "@/lib/jarvis/memory";
import { orbitLabel, orbitWord, type OrbitKind, type Orbits } from "@/lib/jarvis/memoryOrbits";
import { focusRing, focusRingInset } from "../styles";
import { caption, darkScroll, flareButton, flareGhost, glass, glassButton, glassField, glassIcon, starButton } from "./solarStyles";

/** One relation of the open entry, with both ends resolved to entries. */
export interface PanelRelation {
  id: string;
  source: MemoryNode;
  target: MemoryNode;
  relationType: string;
  weight: number;
  status: RelationStatus;
  /** ISO 8601, or null when the brain does not say. */
  updatedAt: string | null;
}

export type ActionOutcome = { ok: true } | { ok: false; message: string };

interface MemoryPanelProps {
  nodes: MemoryNode[];
  orbits: Orbits;
  stats: { links: number; active: number; ended: number };
  /** Bumped when words change in place, so the lists re-sort and re-read them. */
  revision: number;
  query: string;
  /** The search's matches (best first), or null when there is no search. */
  matches: MemoryNode[] | null;
  group: TypeGroupId | "all";
  selected: MemoryNode | null;
  relations: PanelRelation[];
  /** Phone only: the sheet shows its list (true) or just its handle (false). A desktop always shows it. */
  expanded: boolean;
  onExpanded: (expanded: boolean) => void;
  /** Opens an entry: the canvas flies to it. */
  onOpen: (id: string) => void;
  /** Back from an entry to the list. */
  onBack: () => void;
  /** Closes the entry (on a phone the sheet folds down to its handle). */
  onClose: () => void;
  onClearSearch: () => void;
  onSave: (changes: MemoryEntityChanges) => Promise<ActionOutcome>;
  onDelete: () => Promise<ActionOutcome>;
  onDeleteRelation: (id: string) => Promise<ActionOutcome>;
  onRelationStatus: (id: string, status: RelationStatus) => Promise<ActionOutcome>;
}

const CONFIRM_MS = 4000;
const SAVED_MS = 2000;
const DATE = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric" });

/** A small glowing orb in an entry's colour, sized by its orbit (the list, the legend, the details). */
export function OrbDot({ type, kind, sun = false }: { type: string; kind: OrbitKind; sun?: boolean }) {
  const color = groupInfo(groupOf(type)).color;
  const size = kind === "sun" ? 14 : kind === "planet" ? 11 : 8;
  const background = sun
    ? "radial-gradient(circle at 35% 32%, #fffdf4 0%, #ffe6a3 40%, #ffb547 80%, #ff8f3a 100%)"
    : `radial-gradient(circle at 35% 32%, color-mix(in srgb, ${color} 40%, white) 0%, ${color} 55%, color-mix(in srgb, ${color} 50%, #060913) 100%)`;
  const glow = sun ? "rgba(255, 190, 110, 0.55)" : `color-mix(in srgb, ${color} 55%, transparent)`;
  return (
    <span
      aria-hidden="true"
      className="block shrink-0 rounded-full"
      style={{ width: size, height: size, background, boxShadow: `0 0 ${kind === "moon" ? 6 : 9}px ${glow}` }}
    />
  );
}

/**
 * The Memory page's panel: on the right on a desktop, a bottom sheet on a phone (folded to a
 * handle until it is opened, or until an entry is). Two views:
 *
 *   overview  Graph analytics - total nodes, total connections, the orbits and how many facts
 *             hold - then every entry, the Sun first and the rest grouped by type (planets
 *             before moons), or the search's matches; one press flies the canvas to an entry
 *   details   the open entry: its type and orbit, name and description, Edit (name, type,
 *             description - Save sends only what changed; "me" keeps its name) and Delete (asks
 *             first, saying how many relations go with it); then its relations as sentences,
 *             each with its status - Active, Completed, Archived, saved the moment it changes -
 *             the date it last changed, and a remove (a second press confirms)
 *
 * Escape cancels an edit or a pending delete, else goes back to the list.
 */
export function MemoryPanel(props: MemoryPanelProps) {
  const { nodes, stats, selected, expanded, onExpanded } = props;
  const open = expanded || selected !== null;
  return (
    <aside
      data-memory-panel=""
      aria-label={selected ? `Entry: ${displayName(selected)}` : "Memory overview"}
      className={`absolute inset-x-0 bottom-0 z-30 flex max-h-[72svh] flex-col rounded-t-2xl border-b-0 ${glass} md:inset-x-auto md:bottom-3 md:right-3 md:top-20 md:max-h-none md:w-[22rem] md:rounded-2xl md:border-b`}
    >
      {!selected && (
        <button
          type="button"
          onClick={() => onExpanded(!expanded)}
          aria-expanded={open}
          className={`relative flex h-14 w-full shrink-0 items-center gap-3 px-4 pt-1 text-left md:hidden ${focusRingInset}`}
        >
          <span aria-hidden="true" className="absolute left-1/2 top-1.5 h-1 w-9 -translate-x-1/2 rounded-full bg-white/20" />
          <span className="min-w-0 flex-1 truncate text-sm font-semibold text-[#f1f5fb]">
            {nodes.length} {nodes.length === 1 ? "entry" : "entries"}
            <span className="font-normal text-[#a3b0c6]">
              {" · "}
              {stats.links} connection{stats.links === 1 ? "" : "s"}
            </span>
          </span>
          <span className="text-xs font-medium text-[#c9d3e3]">{expanded ? "Hide" : "All entries"}</span>
          <ChevronUp
            className={`size-4 text-[#c9d3e3] transition-transform duration-200 motion-reduce:transition-none ${expanded ? "rotate-180" : ""}`}
            aria-hidden="true"
          />
        </button>
      )}
      <div className={`${open ? "flex" : "hidden"} min-h-0 flex-1 flex-col md:flex`}>
        {selected ? <Details key={selected.id} {...props} node={selected} /> : <Overview {...props} />}
      </div>
    </aside>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-xl border border-white/[0.08] bg-white/[0.04] px-3 py-2.5">
      <dt className="text-xs font-medium text-[#a3b0c6]">{label}</dt>
      <dd className="mt-0.5 text-2xl font-bold tabular-nums leading-8 tracking-tight text-[#f1f5fb]">{value}</dd>
    </div>
  );
}

function Overview({ nodes, orbits, stats, revision, query, matches, group, onOpen, onClearSearch }: MemoryPanelProps) {
  const id = useId();
  const center = orbits.centerId ? (nodes.find((node) => node.id === orbits.centerId) ?? null) : null;

  // The Sun first, then each type's entries - planets before moons, then by name.
  const sections = useMemo(() => {
    const byRing = (a: MemoryNode, b: MemoryNode) =>
      (orbits.byId.get(a.id)?.ring ?? 9) - (orbits.byId.get(b.id)?.ring ?? 9) || a.name.localeCompare(b.name);
    return TYPE_GROUPS.filter((item) => group === "all" || item.id === group)
      .map((item) => ({
        ...item,
        nodes: nodes.filter((node) => node.id !== orbits.centerId && groupOf(node.type) === item.id).sort(byRing),
      }))
      .filter((section) => section.nodes.length > 0);
    // revision: names and types change in place.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodes, orbits, group, revision]);

  const listed = matches ? matches.length : sections.reduce((sum, section) => sum + section.nodes.length, center ? 1 : 0);

  return (
    <div className={`min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 pb-5 pt-4 ${darkScroll}`}>
      <section aria-labelledby={`${id}-stats`}>
        <h2 id={`${id}-stats`} className={caption}>
          Graph analytics
        </h2>
        <dl className="mt-2 grid grid-cols-2 gap-2">
          <Stat label="Total nodes" value={nodes.length} />
          <Stat label="Total connections" value={stats.links} />
        </dl>
        <dl className="mt-3 grid grid-cols-[1fr_auto] gap-x-4 gap-y-1.5 text-[13px]">
          <dt className="text-[#a3b0c6]">Planets · 1 hop</dt>
          <dd className="text-right tabular-nums text-[#f1f5fb]">{orbits.counts.planets}</dd>
          <dt className="text-[#a3b0c6]">Moons · 2+ hops</dt>
          <dd className="text-right tabular-nums text-[#f1f5fb]">{orbits.counts.moons}</dd>
          {orbits.counts.drifting > 0 && (
            <>
              <dt className="text-[#a3b0c6]">Outer orbit · not linked</dt>
              <dd className="text-right tabular-nums text-[#f1f5fb]">{orbits.counts.drifting}</dd>
            </>
          )}
          <dt className="text-[#a3b0c6]">Facts that hold</dt>
          <dd className="text-right tabular-nums text-[#f1f5fb]">{stats.active}</dd>
          <dt className="text-[#a3b0c6]">Facts that ended</dt>
          <dd className="text-right tabular-nums text-[#f1f5fb]">{stats.ended}</dd>
        </dl>
      </section>

      <section aria-labelledby={`${id}-list`} className="mt-5 border-t border-white/10 pt-4">
        <div className="flex items-baseline justify-between gap-2">
          <h2 id={`${id}-list`} className={caption}>
            {matches ? "Matches" : "All entries"}
          </h2>
          <span className="text-xs tabular-nums text-[#a3b0c6]">{listed}</span>
        </div>
        {matches && (
          <p className="mt-1.5 text-xs leading-5 text-[#c9d3e3]">
            For &ldquo;{query.trim()}&rdquo;{matches.length > 0 ? " · Enter flies to the first" : ""} ·{" "}
            <button
              type="button"
              onClick={onClearSearch}
              className={`rounded-sm font-semibold text-[#f1f5fb] underline decoration-white/30 underline-offset-2 hover:decoration-white ${focusRing}`}
            >
              Clear
            </button>
          </p>
        )}
        {!matches && group !== "all" && (
          <p className="mt-1.5 text-xs leading-5 text-[#c9d3e3]">Only {groupInfo(group).label} entries (the type filter).</p>
        )}

        {matches ? (
          matches.length === 0 ? (
            <p className="mt-3 text-sm leading-6 text-[#c9d3e3]">No entries match.</p>
          ) : (
            <ul className="-mx-2 mt-2">
              {matches.map((node) => (
                <EntryRow key={node.id} node={node} orbits={orbits} onOpen={onOpen} />
              ))}
            </ul>
          )
        ) : (
          <>
            {center && (
              <ul className="-mx-2 mt-2" aria-label={orbits.centerIsMe ? "The Sun" : "Centre"}>
                <EntryRow node={center} orbits={orbits} onOpen={onOpen} />
              </ul>
            )}
            {sections.map((section) => (
              <div key={section.id} className="mt-3">
                <h3 className="flex items-center gap-2 px-0.5 text-xs font-semibold text-[#c9d3e3]">
                  <span aria-hidden="true" className="block size-2 rounded-full" style={{ background: section.color }} />
                  {section.label}
                  <span className="font-normal tabular-nums text-[#a3b0c6]">{section.nodes.length}</span>
                </h3>
                <ul className="-mx-2 mt-1">
                  {section.nodes.map((node) => (
                    <EntryRow key={node.id} node={node} orbits={orbits} onOpen={onOpen} />
                  ))}
                </ul>
              </div>
            ))}
          </>
        )}
      </section>
    </div>
  );
}

function EntryRow({ node, orbits, onOpen }: { node: MemoryNode; orbits: Orbits; onOpen: (id: string) => void }) {
  const info = orbits.byId.get(node.id);
  const count = orbits.degree.get(node.id) ?? 0;
  return (
    <li>
      <button
        type="button"
        onClick={() => onOpen(node.id)}
        className={`flex min-h-11 w-full items-center gap-3 rounded-lg px-2 py-1.5 text-left transition-colors duration-150 hover:bg-white/[0.06] ${focusRingInset}`}
      >
        <span className="grid size-4 shrink-0 place-items-center">
          <OrbDot type={node.type} kind={info?.kind ?? "moon"} sun={info?.kind === "sun" && isMe(node)} />
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm font-medium text-[#f1f5fb]">{displayName(node)}</span>
          <span className="block truncate text-xs text-[#a3b0c6]">
            {orbitWord(info, orbits.centerIsMe)} · {count} relation{count === 1 ? "" : "s"}
          </span>
        </span>
      </button>
    </li>
  );
}

function Details({
  node,
  orbits,
  relations,
  onSave,
  onDelete,
  onDeleteRelation,
  onRelationStatus,
  onOpen,
  onBack,
  onClose,
}: MemoryPanelProps & { node: MemoryNode }) {
  const titleId = useId();
  const title = useRef<HTMLHeadingElement | null>(null);
  const [mode, setMode] = useState<"view" | "edit" | "confirm-delete">("view");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const info = orbits.byId.get(node.id);
  const group = groupInfo(groupOf(node.type));

  // Opened (the panel mounts one per entry - key={id} - so another entry starts afresh):
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
      // The search field's own Escape (clearing its words) comes first.
      if ((event.target as HTMLElement | null)?.closest?.("[data-memory-search]")) return;
      event.preventDefault();
      if (mode !== "view") {
        setMode("view");
        setError(null);
      } else {
        onBack();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [mode, onBack]);

  async function remove() {
    setBusy(true);
    setError(null);
    const outcome = await onDelete();
    setBusy(false);
    if (!outcome.ok) setError(outcome.message);
  }

  const count = relations.length;

  return (
    <>
      <div className="flex h-12 shrink-0 items-center justify-between border-b border-white/10 pl-2 pr-1.5">
        <button
          type="button"
          onClick={onBack}
          className={`inline-flex h-9 items-center gap-1.5 rounded-lg px-2 text-[13px] font-medium text-[#c9d3e3] transition-colors duration-150 hover:bg-white/[0.06] hover:text-white ${focusRing}`}
        >
          <ArrowLeft className="size-4" aria-hidden="true" />
          All entries
        </button>
        <button type="button" onClick={onClose} aria-label="Close details" className={glassIcon}>
          <X className="size-5" aria-hidden="true" />
        </button>
      </div>

      <div className={`min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 pb-6 pt-4 ${darkScroll}`}>
        <p className="flex min-w-0 items-center gap-2 text-xs font-medium text-[#c9d3e3]">
          <OrbDot type={node.type} kind={info?.kind ?? "moon"} sun={info?.kind === "sun" && isMe(node)} />
          <span className="truncate">
            {group.label}
            {node.type && node.type !== group.id ? ` · ${node.type}` : ""}
          </span>
        </p>
        <h2
          id={titleId}
          ref={title}
          tabIndex={-1}
          className="mt-2 break-words text-[19px] font-bold leading-7 tracking-tight text-[#f1f5fb] focus:outline-none"
        >
          {displayName(node)}
        </h2>
        <p className="mt-0.5 text-[13px] text-[#a3b0c6]">{orbitLabel(info, orbits.centerIsMe)}</p>

        {mode === "edit" ? (
          <EditForm
            node={node}
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
            <p className="mt-3 whitespace-pre-wrap break-words text-sm leading-6 text-[#c9d3e3]">
              {node.description || <span className="text-[#a3b0c6]">No description.</span>}
            </p>

            {mode === "confirm-delete" ? (
              <div role="group" aria-label="Confirm delete" className="mt-4 rounded-xl border border-[#ff5c7a]/30 bg-[#ff5c7a]/10 p-3">
                <p className="text-sm leading-6 text-[#ffd1d9]">
                  Delete <strong className="font-semibold text-white">{displayName(node)}</strong>
                  {count > 0 ? ` and its ${count} relation${count === 1 ? "" : "s"}` : ""}? Jarvis will forget{" "}
                  {count > 0 ? "them" : "it"}.
                </p>
                <div className="mt-3 flex flex-wrap gap-2">
                  <button type="button" autoFocus disabled={busy} onClick={() => void remove()} className={flareButton}>
                    <Trash2 className="size-4" aria-hidden="true" />
                    {busy ? "Deleting…" : "Delete"}
                  </button>
                  <button type="button" disabled={busy} onClick={() => setMode("view")} className={glassButton}>
                    Cancel
                  </button>
                </div>
              </div>
            ) : (
              <div className="mt-4 flex flex-wrap items-center gap-2">
                <button type="button" onClick={() => setMode("edit")} className={glassButton}>
                  <Pencil className="size-4" aria-hidden="true" />
                  Edit
                </button>
                <button type="button" onClick={() => setMode("confirm-delete")} className={flareGhost}>
                  <Trash2 className="size-4" aria-hidden="true" />
                  Delete
                </button>
                <span role="status" className="ml-1 inline-flex items-center gap-1 text-sm font-medium text-[#4ade80]">
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
          <p role="alert" className="mt-3 text-sm leading-5 text-[#ff8095]">
            {error}
          </p>
        )}

        <section aria-labelledby={`${titleId}-relations`} className="mt-6 border-t border-white/10 pt-4">
          <h3 id={`${titleId}-relations`} className={`flex items-baseline gap-2 ${caption}`}>
            Relations
            <span className="font-normal normal-case tracking-normal tabular-nums">{count}</span>
          </h3>
          {count === 0 ? (
            <p className="mt-2 text-sm leading-5 text-[#c9d3e3]">Not linked to anything else.</p>
          ) : (
            <ul className="mt-1">
              {relations.map((relation) => (
                <RelationRow
                  key={relation.id}
                  relation={relation}
                  selfId={node.id}
                  onOpen={onOpen}
                  onRemove={() => onDeleteRelation(relation.id)}
                  onStatus={(status) => onRelationStatus(relation.id, status)}
                />
              ))}
            </ul>
          )}
        </section>
      </div>
    </>
  );
}

function EditForm({
  node,
  onSave,
  onCancel,
}: {
  node: MemoryNode;
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
  const typeField = useRef<HTMLSelectElement | null>(null);

  // The first field that can change: the name, or for "me" (whose name stays) the type.
  useEffect(() => (first.current ?? typeField.current)?.focus(), []);

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

  const label = "block text-[13px] font-semibold text-[#e6ebf5]";
  const hint = "mt-1.5 text-xs leading-5 text-[#a3b0c6]";

  return (
    <form onSubmit={submit} className="mt-4 space-y-4" aria-describedby={error ? `${id}-error` : undefined}>
      <div>
        <label htmlFor={`${id}-name`} className={label}>
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
          className={`mt-1.5 h-10 w-full ${glassField}`}
        />
        {me && (
          <p id={`${id}-me`} className={hint}>
            This entry is you, so its name stays &ldquo;me&rdquo;.
          </p>
        )}
      </div>

      <div>
        <label htmlFor={`${id}-type`} className={label}>
          Type
        </label>
        <select
          id={`${id}-type`}
          ref={me ? typeField : undefined}
          value={type}
          onChange={(event) => setType(event.target.value)}
          className={`mt-1.5 h-10 w-full pr-8 ${glassField}`}
        >
          {/* An entry saved before types were fixed keeps its old one until another is picked. */}
          {!(ENTITY_TYPES as readonly string[]).includes(node.type) && <option value={node.type}>{node.type || "no type"}</option>}
          {ENTITY_TYPES.map((kind) => (
            <option key={kind} value={kind}>
              {kind}
            </option>
          ))}
        </select>
      </div>

      <div>
        <div className="flex items-baseline justify-between">
          <label htmlFor={`${id}-description`} className={label}>
            Description
          </label>
          <span className="text-xs tabular-nums text-[#a3b0c6]" aria-hidden="true">
            {description.length}/{MEMORY_LIMITS.description}
          </span>
        </div>
        <textarea
          id={`${id}-description`}
          value={description}
          onChange={(event) => setDescription(event.target.value)}
          maxLength={MEMORY_LIMITS.description}
          rows={4}
          className={`mt-1.5 w-full resize-y py-2 leading-6 ${glassField} ${darkScroll}`}
        />
        <p className={hint}>Jarvis finds this entry by its name, type and description.</p>
      </div>

      {error && (
        <p id={`${id}-error`} role="alert" className="text-sm leading-5 text-[#ff8095]">
          {error}
        </p>
      )}

      <div className="flex flex-wrap gap-2">
        <button type="submit" disabled={busy || !changed || nameMissing} className={starButton}>
          {busy ? "Saving…" : "Save"}
        </button>
        <button type="button" onClick={onCancel} disabled={busy} className={glassButton}>
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
  onStatus,
}: {
  relation: PanelRelation;
  selfId: string;
  onOpen: (id: string) => void;
  onRemove: () => Promise<ActionOutcome>;
  onStatus: (status: RelationStatus) => Promise<ActionOutcome>;
}) {
  const statusId = useId();
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ended = relation.status !== "active";
  const updated = relation.updatedAt ? new Date(relation.updatedAt) : null;

  async function changeStatus(status: RelationStatus) {
    if (status === relation.status) return;
    setSaving(true);
    setError(null);
    const outcome = await onStatus(status);
    setSaving(false);
    if (!outcome.ok) setError(outcome.message);
  }

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
      <span className="font-semibold text-[#f1f5fb]">{displayName(node)}</span>
    ) : (
      <button
        type="button"
        onClick={() => onOpen(node.id)}
        className={`rounded-sm font-semibold text-[#f1f5fb] underline decoration-white/30 underline-offset-2 hover:decoration-white ${focusRing}`}
      >
        {displayName(node)}
      </button>
    );

  return (
    <li className="border-b border-white/[0.06] py-2.5 last:border-b-0">
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <p className={`break-words text-sm leading-6 ${ended ? "text-[#a3b0c6]" : "text-[#c9d3e3]"}`}>
            {end(relation.source)} <span>{relationLabel(relation.relationType)}</span> {end(relation.target)}
            {relation.weight > 1 && (
              <span className="ml-2 whitespace-nowrap text-xs tabular-nums text-[#a3b0c6]">told {relation.weight}×</span>
            )}
          </p>
          <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1">
            <label htmlFor={statusId} className="sr-only">
              Status of &ldquo;{sentence}&rdquo;
            </label>
            <select
              id={statusId}
              value={relation.status}
              disabled={saving || busy}
              onChange={(event) => void changeStatus(event.target.value as RelationStatus)}
              className={`h-8 rounded-lg border border-white/[0.14] bg-[#060913]/70 pl-2 pr-7 text-xs font-medium text-[#f1f5fb] hover:border-white/25 disabled:cursor-wait disabled:opacity-60 pointer-coarse:h-10 ${focusRing}`}
            >
              {RELATION_STATUSES.map((status) => (
                <option key={status} value={status}>
                  {RELATION_STATUS_LABEL[status]}
                </option>
              ))}
            </select>
            {updated && !Number.isNaN(updated.getTime()) && (
              <time dateTime={relation.updatedAt ?? undefined} className="text-xs tabular-nums text-[#a3b0c6]">
                {saving ? "Saving…" : `Updated ${DATE.format(updated)}`}
              </time>
            )}
          </div>
        </div>
        <button
          type="button"
          onClick={() => void press()}
          disabled={busy}
          aria-label={armed ? `Confirm: remove "${sentence}"` : `Remove "${sentence}"`}
          title={armed ? "Press again to remove" : "Remove this relation"}
          className={`inline-flex h-8 min-w-8 shrink-0 items-center justify-center gap-1.5 rounded-lg text-xs font-medium transition-[background-color,color] duration-150 disabled:cursor-wait disabled:opacity-50 pointer-coarse:h-10 pointer-coarse:min-w-10 ${
            armed ? "bg-[#ff5c7a]/15 px-2 text-[#ff8095]" : "text-[#a3b0c6] hover:bg-white/[0.08] hover:text-white"
          } ${focusRing}`}
        >
          <X className="size-4 shrink-0" aria-hidden="true" />
          {armed && <span aria-hidden="true">{busy ? "Removing…" : "Remove?"}</span>}
        </button>
      </div>
      {error && (
        <p role="alert" className="mt-1 text-xs leading-5 text-[#ff8095]">
          {error}
        </p>
      )}
    </li>
  );
}
