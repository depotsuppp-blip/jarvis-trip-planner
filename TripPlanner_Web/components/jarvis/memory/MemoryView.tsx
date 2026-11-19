"use client";

import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { ArrowLeft, Maximize2, Search, X } from "lucide-react";
import {
  displayName,
  groupInfo,
  groupOf,
  loadMemoryGraph,
  removeMemoryEntity,
  removeMemoryRelation,
  saveMemoryEntity,
  searchNodes,
  TYPE_GROUPS,
  type MemoryEntityChanges,
  type MemoryGraph,
  type MemoryNode,
  type TypeGroupId,
} from "@/lib/jarvis/memory";
import type { GraphCanvasApi, GraphLink, GraphNode } from "./MemoryGraphCanvas";
import { MemoryInspector, type ActionOutcome, type InspectorRelation } from "./MemoryInspector";
import { focusRing, focusRingInset, iconButton, thinScroll } from "../styles";

// The graph library touches `window` as it loads: browser only, in a chunk of its own.
const MemoryGraphCanvas = dynamic(() => import("./MemoryGraphCanvas"), {
  ssr: false,
  loading: () => <CanvasMessage title="Drawing the graph…" busy />,
});

type Status =
  | { kind: "loading" }
  | { kind: "ready" }
  | { kind: "signed-out" }
  | { kind: "error"; message: string };

const NOTICE_MS = 4000;
const LIST_LIMIT = 60;

const endId = (end: string | GraphNode): string => (typeof end === "object" ? end.id : end);

function toGraph(data: MemoryGraph): { nodes: GraphNode[]; links: GraphLink[] } {
  return { nodes: data.nodes.map((node) => ({ ...node })), links: data.links.map((link) => ({ ...link })) };
}

/**
 * The Memory page (/jarvis/memory): what Jarvis remembers about the signed-in person, as a
 * graph they can search, filter, inspect and correct.
 *
 *   header      back to the chat, the page's name, how much is in memory
 *   toolbar     search (a combobox: matches light up on the canvas, the list opens one),
 *               type filter (the legend's groups), Fit to view
 *   canvas      MemoryGraphCanvas (next/dynamic, browser only) with the legend over it
 *   inspector   MemoryInspector for the open entry: a side panel, a bottom sheet on a phone
 *
 * Every change is the server's first (PATCH / DELETE through /api/memory) and then the
 * graph's: the edited entry repaints in place, a deleted one leaves with its relations, and
 * the layout settles around the gap - no reload.
 */
export function MemoryView() {
  const [status, setStatus] = useState<Status>({ kind: "loading" });
  const [graph, setGraph] = useState<{ nodes: GraphNode[]; links: GraphLink[] }>({ nodes: [], links: [] });
  const [revision, setRevision] = useState(0);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [group, setGroup] = useState<TypeGroupId | "all">("all");
  const [notice, setNotice] = useState<string | null>(null);
  const canvas = useRef<GraphCanvasApi | null>(null);
  const filterId = useId();

  const load = useCallback(async () => {
    setStatus({ kind: "loading" });
    const result = await loadMemoryGraph();
    if (result.ok) {
      setGraph(toGraph(result.data));
      setStatus({ kind: "ready" });
    } else if (result.status === 401) {
      setStatus({ kind: "signed-out" });
    } else {
      setStatus({ kind: "error", message: result.message });
    }
  }, []);

  useEffect(() => {
    // The first load, once the page is in the browser.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
  }, [load]);

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(null), NOTICE_MS);
    return () => window.clearTimeout(timer);
  }, [notice]);

  const nodeById = useMemo(() => new Map(graph.nodes.map((node) => [node.id, node])), [graph]);
  // revision: names change in place, and the search must see them.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const matches = useMemo(() => (query.trim() ? searchNodes(graph.nodes, query) : null), [graph, query, revision]);
  const matchIds = useMemo(() => (matches ? new Set(matches.map((node) => node.id)) : null), [matches]);
  const groupCounts = useMemo(() => {
    const counts = new Map<TypeGroupId, number>();
    for (const node of graph.nodes) counts.set(groupOf(node.type), (counts.get(groupOf(node.type)) ?? 0) + 1);
    return counts;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graph, revision]);
  const knownTypes = useMemo(
    () => [...new Set(graph.nodes.map((node) => node.type).filter(Boolean))].sort(),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [graph, revision],
  );

  const selected = selectedId ? (nodeById.get(selectedId) ?? null) : null;
  const relations: InspectorRelation[] = useMemo(() => {
    if (!selectedId) return [];
    return graph.links
      .filter((link) => endId(link.source) === selectedId || endId(link.target) === selectedId)
      .flatMap((link) => {
        const source = nodeById.get(endId(link.source));
        const target = nodeById.get(endId(link.target));
        return source && target
          ? [{ id: link.id, source, target, relationType: link.relation_type, weight: link.weight }]
          : [];
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graph, nodeById, selectedId, revision]);

  const onReady = useCallback((api: GraphCanvasApi) => {
    canvas.current = api;
  }, []);

  /**
   * Glides the canvas to an entry, once the panel for it is on screen. On a phone the panel is a
   * bottom sheet over the canvas, so the entry is centred in what the sheet leaves visible.
   */
  const reveal = useCallback((id: string) => {
    window.setTimeout(() => {
      const sheet = document.querySelector<HTMLElement>("[data-memory-sheet]");
      const covered = sheet && getComputedStyle(sheet).position === "fixed" ? sheet.getBoundingClientRect().height : 0;
      canvas.current?.focus(id, covered);
    }, 0);
  }, []);

  /** Opens an entry from a list (search, a relation): the canvas glides to it. */
  const open = useCallback(
    (id: string) => {
      const node = nodeById.get(id);
      if (node && group !== "all" && groupOf(node.type) !== group) setGroup("all");
      setSelectedId(id);
      // After the filter (if any) has let it back onto the canvas and the panel is drawn.
      reveal(id);
    },
    [nodeById, group, reveal],
  );

  /** A dot tapped on the canvas: on a desktop it stays where it is; on a phone it moves clear of the sheet. */
  const pick = useCallback(
    (id: string | null) => {
      setSelectedId(id);
      if (id && !window.matchMedia("(min-width: 768px)").matches) reveal(id);
    },
    [reveal],
  );

  // ---- changes: the server first, then the graph -------------------------------------------

  const forget = useCallback((nodeId: string, linkIds: string[] = []) => {
    const gone = new Set(linkIds);
    setGraph((current) => ({
      nodes: current.nodes.filter((node) => node.id !== nodeId),
      links: current.links.filter(
        (link) => !gone.has(link.id) && endId(link.source) !== nodeId && endId(link.target) !== nodeId,
      ),
    }));
    setSelectedId((current) => (current === nodeId ? null : current));
  }, []);

  const save = useCallback(
    async (node: GraphNode, changes: MemoryEntityChanges): Promise<ActionOutcome> => {
      const result = await saveMemoryEntity(node.id, changes);
      if (!result.ok) {
        if (result.status === 404) forget(node.id);
        return { ok: false, message: result.message };
      }
      // In place: the canvas keeps the node where it is and just paints the new words.
      Object.assign(node, result.data.node);
      setRevision((value) => value + 1);
      setNotice(`Saved “${displayName(node)}”.`);
      return { ok: true };
    },
    [forget],
  );

  const deleteEntity = useCallback(
    async (node: GraphNode): Promise<ActionOutcome> => {
      const result = await removeMemoryEntity(node.id);
      if (!result.ok && result.status !== 404) return { ok: false, message: result.message };
      const links = result.ok ? result.data.links.length : 0;
      forget(node.id, result.ok ? result.data.links : []);
      setNotice(
        `Deleted “${displayName(node)}”${links ? ` and ${links} relation${links === 1 ? "" : "s"}` : ""}.`,
      );
      return { ok: true };
    },
    [forget],
  );

  const deleteRelation = useCallback(
    async (id: string): Promise<ActionOutcome> => {
      const link = graph.links.find((item) => item.id === id);
      const result = await removeMemoryRelation(id);
      if (!result.ok && result.status !== 404) return { ok: false, message: result.message };
      setGraph((current) => ({ nodes: current.nodes, links: current.links.filter((item) => item.id !== id) }));
      if (link) {
        const source = nodeById.get(endId(link.source));
        const target = nodeById.get(endId(link.target));
        if (source && target) {
          setNotice(`Removed “${displayName(source)} ${link.relation_type.replace(/_/g, " ")} ${displayName(target)}”.`);
        }
      }
      return { ok: true };
    },
    [graph, nodeById],
  );

  const ready = status.kind === "ready";
  const empty = ready && graph.nodes.length === 0;
  const presentGroups = TYPE_GROUPS.filter((item) => groupCounts.has(item.id));

  return (
    // As in the hub: the outer wrapper only carries the selection colours (white on Deep Signal Rose).
    <div className="selection:bg-rose-600 selection:text-white">
    <div className="flex h-svh flex-col overflow-hidden bg-white text-slate-900">
      <header className="flex h-14 shrink-0 items-center gap-2 border-b border-slate-200 pl-2 pr-4 md:pl-3">
        <Link href="/jarvis" aria-label="Back to chat" title="Back to chat" className={iconButton}>
          <ArrowLeft className="size-5" aria-hidden="true" />
        </Link>
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-[15px] font-semibold leading-5 tracking-tight text-slate-900">Memory</h1>
          <p className="hidden truncate text-xs leading-4 text-slate-600 sm:block">What Jarvis remembers about you</p>
        </div>
        {ready && !empty && (
          <p className="shrink-0 text-xs tabular-nums text-slate-600">
            {graph.nodes.length} {graph.nodes.length === 1 ? "entry" : "entries"}
            <span aria-hidden="true"> · </span>
            <span className="sr-only">, </span>
            {graph.links.length} relation{graph.links.length === 1 ? "" : "s"}
          </p>
        )}
      </header>

      {ready && !empty && (
        <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-slate-200 px-3 py-2 md:px-4">
          <SearchBox
            nodes={graph.nodes}
            query={query}
            matches={matches}
            revision={revision}
            onQuery={setQuery}
            onPick={(id) => {
              setQuery("");
              open(id);
            }}
          />
          <label htmlFor={filterId} className="sr-only">
            Type
          </label>
          <select
            id={filterId}
            value={group}
            onChange={(event) => setGroup(event.target.value as TypeGroupId | "all")}
            className={`h-10 min-w-0 rounded-md border border-slate-300 bg-white pl-3 pr-8 text-sm font-medium text-slate-900 hover:border-slate-400 ${focusRing}`}
          >
            <option value="all">All types ({graph.nodes.length})</option>
            {presentGroups.map((item) => (
              <option key={item.id} value={item.id}>
                {item.label} ({groupCounts.get(item.id)})
              </option>
            ))}
          </select>
          <button
            type="button"
            onClick={() => canvas.current?.fit()}
            className={`inline-flex h-10 shrink-0 items-center gap-2 rounded-md border border-slate-300 bg-white px-3 text-sm font-semibold text-slate-900 transition-[border-color,scale] duration-150 hover:border-slate-900 active:scale-[0.98] ${focusRing}`}
          >
            <Maximize2 className="size-4" aria-hidden="true" />
            Fit to view
          </button>
        </div>
      )}

      <div className="relative flex min-h-0 flex-1">
        <div className="relative min-w-0 flex-1">
          {status.kind === "loading" && <CanvasMessage title="Loading memory…" busy />}
          {status.kind === "signed-out" && (
            <CanvasMessage title="Sign in to see what Jarvis remembers about you.">
              <Link
                href="/login?callbackUrl=%2Fjarvis%2Fmemory"
                className={`mt-4 inline-flex h-10 items-center rounded-md bg-slate-900 px-4 text-sm font-semibold text-white hover:bg-slate-800 ${focusRing}`}
              >
                Sign in
              </Link>
            </CanvasMessage>
          )}
          {status.kind === "error" && (
            <CanvasMessage title="Couldn't open Jarvis's memory." tone="error" detail={status.message}>
              <button
                type="button"
                onClick={() => void load()}
                className={`mt-4 inline-flex h-10 items-center rounded-md border border-slate-300 bg-white px-4 text-sm font-semibold text-slate-900 hover:border-slate-900 ${focusRing}`}
              >
                Try again
              </button>
            </CanvasMessage>
          )}
          {empty && (
            <CanvasMessage
              title="Nothing remembered yet"
              detail="When you tell Jarvis something lasting - “My sister Nok lives in Chiang Mai” - it shows up here as a graph you can check and correct."
            >
              <Link
                href="/jarvis"
                className={`mt-4 inline-flex h-10 items-center rounded-md border border-slate-300 bg-white px-4 text-sm font-semibold text-slate-900 hover:border-slate-900 ${focusRing}`}
              >
                Open the chat
              </Link>
            </CanvasMessage>
          )}

          {ready && !empty && (
            <>
              <div
                role="img"
                aria-label={`Memory graph: ${graph.nodes.length} entries and ${graph.links.length} relations. Use the search to find and open an entry.`}
                className="absolute inset-0"
              >
                <MemoryGraphCanvas
                  graph={graph}
                  revision={revision}
                  selectedId={selectedId}
                  matchIds={matchIds}
                  group={group}
                  onSelect={pick}
                  onReady={onReady}
                />
              </div>

              <ul
                aria-label="Legend"
                className="pointer-events-none absolute bottom-3 left-3 flex max-w-[calc(100%-1.5rem)] flex-wrap gap-x-3 gap-y-1 rounded-md border border-slate-200 bg-white/95 px-3 py-2"
              >
                {presentGroups.map((item) => (
                  <li
                    key={item.id}
                    className={`flex items-center gap-1.5 text-xs font-medium ${
                      group === "all" || group === item.id ? "text-slate-700" : "text-slate-400"
                    }`}
                  >
                    <span aria-hidden="true" className="block size-2.5 rounded-full" style={{ background: item.color }} />
                    {item.label}
                    <span className="tabular-nums text-slate-600">{groupCounts.get(item.id)}</span>
                  </li>
                ))}
              </ul>
            </>
          )}

          {notice && (
            <p
              aria-hidden="true"
              className="pointer-events-none absolute left-1/2 top-3 max-w-[calc(100%-2rem)] -translate-x-1/2 truncate rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white"
            >
              {notice}
            </p>
          )}
          <p role="status" className="sr-only">
            {notice ?? ""}
          </p>
        </div>

        {selected && (
          <MemoryInspector
            key={selected.id}
            node={selected}
            relations={relations}
            knownTypes={knownTypes}
            onSave={(changes) => save(selected, changes)}
            onDelete={() => deleteEntity(selected)}
            onDeleteRelation={deleteRelation}
            onOpen={open}
            onClose={() => setSelectedId(null)}
          />
        )}
      </div>
    </div>
    </div>
  );
}

function CanvasMessage({
  title,
  detail,
  tone = "plain",
  busy = false,
  children,
}: {
  title: string;
  detail?: string;
  tone?: "plain" | "error";
  busy?: boolean;
  children?: React.ReactNode;
}) {
  return (
    <div
      role={busy ? "status" : undefined}
      className="absolute inset-0 grid place-items-center px-6 text-center"
    >
      <div className="max-w-sm">
        <p
          className={`text-[17px] font-bold tracking-tight ${tone === "error" ? "text-red-700" : "text-slate-900"} ${
            busy ? "motion-safe:animate-pulse" : ""
          }`}
        >
          {title}
        </p>
        {detail && <p className="mt-2 text-sm leading-6 text-slate-600">{detail}</p>}
        {children}
      </div>
    </div>
  );
}

/**
 * The search: a combobox over every entry. Typing lights up the matches on the canvas
 * (the rest fade) and lists them; Arrow keys move through the list, Enter opens the
 * highlighted one (or the first), Escape closes the list and then clears the words. With
 * nothing typed, Arrow Down lists every entry - the page's keyboard way into the graph.
 */
function SearchBox({
  nodes,
  query,
  matches,
  revision,
  onQuery,
  onPick,
}: {
  nodes: MemoryNode[];
  query: string;
  matches: MemoryNode[] | null;
  revision: number;
  onQuery: (query: string) => void;
  onPick: (id: string) => void;
}) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const list = useRef<HTMLUListElement | null>(null);

  const all = useMemo(
    () => [...nodes].sort((a, b) => a.name.localeCompare(b.name)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [nodes, revision],
  );
  const options = (matches ?? all).slice(0, LIST_LIMIT);
  const total = (matches ?? all).length;
  const expanded = open && (query.trim() !== "" || active >= 0 || options.length > 0);

  useEffect(() => {
    // New words, new list: nothing is highlighted until an arrow key says so.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setActive(-1);
  }, [query]);

  useEffect(() => {
    if (active < 0) return;
    list.current?.querySelector(`#${CSS.escape(`${id}-option-${active}`)}`)?.scrollIntoView({ block: "nearest" });
  }, [active, id]);

  function onKeyDown(event: React.KeyboardEvent<HTMLInputElement>) {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      setOpen(true);
      if (!options.length) return;
      const step = event.key === "ArrowDown" ? 1 : -1;
      setActive((current) => (current < 0 ? (step > 0 ? 0 : options.length - 1) : (current + step + options.length) % options.length));
    } else if (event.key === "Enter") {
      const pick = options[active >= 0 ? active : 0];
      if (pick && (query.trim() || active >= 0)) {
        event.preventDefault();
        setOpen(false);
        onPick(pick.id);
      }
    } else if (event.key === "Escape") {
      if (open) {
        event.preventDefault();
        setOpen(false);
        setActive(-1);
      } else if (query) {
        event.preventDefault();
        onQuery("");
      }
    }
  }

  const showList = open && (query.trim() !== "" || active >= 0);

  return (
    <div data-memory-search="" className="relative min-w-0 flex-1 basis-56 md:max-w-sm">
      <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-slate-500" aria-hidden="true" />
      <input
        type="text"
        role="combobox"
        aria-expanded={showList && expanded}
        aria-controls={`${id}-list`}
        aria-autocomplete="list"
        aria-activedescendant={showList && active >= 0 ? `${id}-option-${active}` : undefined}
        aria-label="Search memory"
        placeholder="Search memory"
        value={query}
        onChange={(event) => {
          onQuery(event.target.value);
          setOpen(true);
        }}
        onKeyDown={onKeyDown}
        onFocus={() => query.trim() && setOpen(true)}
        onBlur={() => setOpen(false)}
        autoComplete="off"
        spellCheck={false}
        className={`h-10 w-full rounded-md border border-slate-300 bg-white pl-9 pr-9 text-[15px] text-slate-900 placeholder:text-slate-500 hover:border-slate-400 focus-visible:border-slate-900 ${focusRing}`}
      />
      {query && (
        <button
          type="button"
          onClick={() => onQuery("")}
          aria-label="Clear search"
          className={`absolute right-1 top-1/2 grid size-8 -translate-y-1/2 place-items-center rounded-md text-slate-500 hover:bg-slate-100 hover:text-slate-900 ${focusRing}`}
        >
          <X className="size-4" aria-hidden="true" />
        </button>
      )}

      {showList && (
        <div className="absolute left-0 right-0 top-full z-20 mt-1 overflow-hidden rounded-md border border-slate-200 bg-white">
          <p className="border-b border-slate-100 px-3 py-2 text-xs text-slate-600">
            {matches === null
              ? `${total} ${total === 1 ? "entry" : "entries"}`
              : total === 0
                ? `No entries match “${query.trim()}”.`
                : `${total} match${total === 1 ? "" : "es"}`}
            {total > LIST_LIMIT && ` - showing ${LIST_LIMIT}, keep typing to narrow it down`}
          </p>
          {options.length > 0 && (
            <ul ref={list} id={`${id}-list`} role="listbox" aria-label="Memory entries" className={`max-h-72 overflow-y-auto py-1 ${thinScroll}`}>
              {options.map((node, index) => (
                <li
                  key={node.id}
                  id={`${id}-option-${index}`}
                  role="option"
                  aria-selected={index === active}
                  // Keeps the focus in the field, so the list does not close before the click lands.
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => {
                    setOpen(false);
                    onPick(node.id);
                  }}
                  className={`flex min-h-10 cursor-pointer items-center gap-2 px-3 text-sm ${
                    index === active ? "bg-slate-100 text-slate-900" : "text-slate-700 hover:bg-slate-50"
                  } ${focusRingInset}`}
                >
                  <span
                    aria-hidden="true"
                    className="block size-2.5 shrink-0 rounded-full"
                    style={{ background: groupInfo(groupOf(node.type)).color }}
                  />
                  <span className="min-w-0 flex-1 truncate font-medium">{displayName(node)}</span>
                  <span className="shrink-0 truncate text-xs text-slate-600">{node.type}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      <p role="status" className="sr-only">
        {matches === null ? "" : total === 0 ? "No matches" : `${total} match${total === 1 ? "" : "es"}`}
      </p>
    </div>
  );
}
