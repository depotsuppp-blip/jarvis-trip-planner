"use client";

import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { ArrowLeft, ChevronDown, Maximize2, Search, Tags, X } from "lucide-react";
import {
  displayName,
  groupOf,
  loadMemoryGraph,
  RELATION_DASH,
  RELATION_STATUS_LABEL,
  RELATION_STATUSES,
  relationLabel,
  removeMemoryEntity,
  removeMemoryRelation,
  saveMemoryEntity,
  saveMemoryRelationStatus,
  searchNodes,
  TYPE_GROUPS,
  type MemoryEntityChanges,
  type MemoryGraph,
  type RelationStatus,
  type TypeGroupId,
} from "@/lib/jarvis/memory";
import { computeOrbits, endId, seedOrbits, settle, type GraphNode, type SolarGraph } from "@/lib/jarvis/memoryOrbits";
// Types only: a value imported from the canvas module would pull the graph library into server rendering.
import type { Insets, SolarCanvasApi } from "./SolarCanvas";
import { MemoryPanel, OrbDot, type ActionOutcome, type PanelRelation } from "./MemoryPanel";
import { glass, glassButton, glassField, glassIcon } from "./solarStyles";
import { focusRing } from "../styles";

// The graph library touches `window` as it loads: browser only, in a chunk of its own.
const SolarCanvas = dynamic(() => import("./SolarCanvas"), {
  ssr: false,
  loading: () => <CanvasMessage title="Drawing your solar system…" busy />,
});

type Status =
  | { kind: "loading" }
  | { kind: "ready" }
  | { kind: "signed-out" }
  | { kind: "error"; message: string };

const NOTICE_MS = 4000;
const DESKTOP = "(min-width: 768px)";

/** The page's working copy: the canvas library writes positions into these objects. */
function toGraph(data: MemoryGraph): SolarGraph {
  const graph: SolarGraph = { nodes: data.nodes.map((node) => ({ ...node })), links: data.links.map((link) => ({ ...link })) };
  const orbits = computeOrbits(graph.nodes, graph.links);
  // On their orbits before the first frame; drawn centre-out, so the Sun's and planets' names
  // are placed before the moons' (a name is left out where it would cover one already drawn).
  seedOrbits(graph.nodes, orbits);
  graph.nodes.sort((a, b) => (orbits.byId.get(a.id)?.ring ?? 0) - (orbits.byId.get(b.id)?.ring ?? 0));
  return graph;
}

/** What the floating panels cover of the canvas right now, read from the page itself. */
function measureInsets(): Insets {
  const insets: Insets = { top: 0, right: 0, bottom: 0, left: 0 };
  const stage = document.querySelector<HTMLElement>("[data-memory-stage]")?.getBoundingClientRect();
  if (!stage) return insets;
  const bar = document.querySelector<HTMLElement>("[data-memory-topbar]")?.getBoundingClientRect();
  if (bar) insets.top = Math.max(0, bar.bottom - stage.top);
  const panel = document.querySelector<HTMLElement>("[data-memory-panel]")?.getBoundingClientRect();
  if (panel && panel.width > 0 && panel.height > 0) {
    if (window.matchMedia(DESKTOP).matches) insets.right = Math.max(0, stage.right - panel.left);
    else insets.bottom = Math.max(0, stage.bottom - panel.top);
  }
  return insets;
}

/**
 * The Memory page (/jarvis/memory): what Jarvis remembers about the signed-in person, as a
 * solar system they can search, filter, inspect and correct - on deep space, under glass.
 *
 *   top bar   back to Jarvis, the page's name, search (matches light up on the canvas and in
 *             the panel's list; Enter flies to the first), the type filter, Floating labels,
 *             Fit to view
 *   canvas    SolarCanvas (next/dynamic, browser only): the person at the centre as the Sun,
 *             planets one relation away, moons further out, on still orbits
 *   panel     MemoryPanel: graph analytics and every entry, or the open entry's details and
 *             its relations - on the right on a desktop, a bottom sheet on a phone
 *   legend    bottom right (the bottom-left corner is the Next.js dev tools' badge)
 *
 * Every change is the server's first (PATCH / DELETE through /api/memory) and then the
 * graph's: the edited entry repaints in place, a deleted one leaves with its relations, and
 * the orbits close round the gap - no reload.
 */
export function MemoryView() {
  const [status, setStatus] = useState<Status>({ kind: "loading" });
  const [graph, setGraph] = useState<SolarGraph>({ nodes: [], links: [] });
  const [revision, setRevision] = useState(0);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [group, setGroup] = useState<TypeGroupId | "all">("all");
  const [labels, setLabels] = useState(true);
  // Phone: whether the bottom sheet shows its list (an open entry always shows).
  const [sheetExpanded, setSheetExpanded] = useState(false);
  // null until the page knows the screen: the legend starts open on a desktop, folded on a phone.
  const [legendOpen, setLegendOpen] = useState<boolean | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const canvas = useRef<SolarCanvasApi | null>(null);
  const filterId = useId();
  const legendId = useId();

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
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLegendOpen(window.matchMedia(DESKTOP).matches);
  }, []);

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(null), NOTICE_MS);
    return () => window.clearTimeout(timer);
  }, [notice]);

  const orbits = useMemo(() => computeOrbits(graph.nodes, graph.links), [graph]);
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
  const stats = useMemo(() => {
    const active = graph.links.filter((link) => link.status === "active").length;
    return { links: graph.links.length, active, ended: graph.links.length - active };
    // revision: a status changed in place.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graph, revision]);

  const selected = selectedId ? (nodeById.get(selectedId) ?? null) : null;
  const relations: PanelRelation[] = useMemo(() => {
    if (!selectedId) return [];
    return graph.links
      .filter((link) => endId(link.source) === selectedId || endId(link.target) === selectedId)
      .flatMap((link) => {
        const source = nodeById.get(endId(link.source));
        const target = nodeById.get(endId(link.target));
        return source && target
          ? [{
              id: link.id,
              source,
              target,
              relationType: link.relation_type,
              weight: link.weight,
              status: link.status ?? "active",
              updatedAt: link.updated_at ?? null,
            }]
          : [];
      })
      // What holds now first, then what ended; the latest first within each.
      .sort(
        (a, b) =>
          RELATION_STATUSES.indexOf(a.status) - RELATION_STATUSES.indexOf(b.status) ||
          (b.updatedAt ?? "").localeCompare(a.updatedAt ?? ""),
      );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graph, nodeById, selectedId, revision]);

  const onReady = useCallback((api: SolarCanvasApi) => {
    canvas.current = api;
  }, []);

  /** Flies the canvas to an entry once its panel is on screen, centred in what the panels leave clear. */
  const reveal = useCallback((id: string) => {
    window.setTimeout(() => canvas.current?.focus(id), 0);
  }, []);

  /** Opens an entry from a list (the panel, the search, a relation): the canvas flies to it. */
  const open = useCallback(
    (id: string) => {
      const node = nodeById.get(id);
      if (node && group !== "all" && node.id !== orbits.centerId && groupOf(node.type) !== group) setGroup("all");
      setSelectedId(id);
      reveal(id);
    },
    [nodeById, group, orbits.centerId, reveal],
  );

  /** An orb tapped on the canvas: on a desktop it stays where it is; on a phone it moves clear of the sheet. */
  const pick = useCallback(
    (id: string | null) => {
      setSelectedId(id);
      if (id && !window.matchMedia(DESKTOP).matches) reveal(id);
    },
    [reveal],
  );

  const changeQuery = useCallback((text: string) => {
    setQuery(text);
    if (text.trim()) {
      // A new search shows its matches in the panel's list.
      setSelectedId(null);
      setSheetExpanded(true);
    }
  }, []);

  // ---- changes: the server first, then the graph -------------------------------------------

  const forget = useCallback((nodeId: string, linkIds: string[] = []) => {
    const gone = new Set(linkIds);
    setGraph((current) =>
      settle(
        current.nodes.filter((node) => node.id !== nodeId),
        current.links.filter((link) => !gone.has(link.id) && endId(link.source) !== nodeId && endId(link.target) !== nodeId),
      ),
    );
    setSelectedId((current) => (current === nodeId ? null : current));
  }, []);

  const save = useCallback(
    async (node: GraphNode, changes: MemoryEntityChanges): Promise<ActionOutcome> => {
      const result = await saveMemoryEntity(node.id, changes);
      if (!result.ok) {
        if (result.status === 404) forget(node.id);
        return { ok: false, message: result.message };
      }
      // In place: the canvas keeps the orb where it is and just paints the new words (or colour).
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

  const setRelationStatus = useCallback(
    async (id: string, status: RelationStatus): Promise<ActionOutcome> => {
      const link = graph.links.find((item) => item.id === id);
      const result = await saveMemoryRelationStatus(id, status);
      if (!result.ok) {
        if (result.status === 404) {
          setGraph((current) => settle(current.nodes, current.links.filter((item) => item.id !== id)));
        }
        return { ok: false, message: result.message };
      }
      if (link) {
        // In place, like an entry's words: the line redraws in its new style, the orbits stay.
        link.status = result.data.link.status;
        link.updated_at = result.data.link.updated_at;
        setRevision((value) => value + 1);
        const source = nodeById.get(endId(link.source));
        const target = nodeById.get(endId(link.target));
        if (source && target) {
          setNotice(
            `Marked “${displayName(source)} ${relationLabel(link.relation_type)} ${displayName(target)}” ${RELATION_STATUS_LABEL[status].toLowerCase()}.`,
          );
        }
      }
      return { ok: true };
    },
    [graph, nodeById],
  );

  const deleteRelation = useCallback(
    async (id: string): Promise<ActionOutcome> => {
      const link = graph.links.find((item) => item.id === id);
      const result = await removeMemoryRelation(id);
      if (!result.ok && result.status !== 404) return { ok: false, message: result.message };
      setGraph((current) => settle(current.nodes, current.links.filter((item) => item.id !== id)));
      if (link) {
        const source = nodeById.get(endId(link.source));
        const target = nodeById.get(endId(link.target));
        if (source && target) {
          setNotice(`Removed “${displayName(source)} ${relationLabel(link.relation_type)} ${displayName(target)}”.`);
        }
      }
      return { ok: true };
    },
    [graph, nodeById],
  );

  const ready = status.kind === "ready";
  const empty = ready && graph.nodes.length === 0;
  const live = ready && !empty;
  const presentGroups = TYPE_GROUPS.filter((item) => groupCounts.has(item.id));
  // On a phone the open sheet covers the legend's corner.
  const sheetOpen = sheetExpanded || selected !== null;

  return (
    <div
      data-memory-stage=""
      className="relative h-dvh w-full overflow-hidden bg-[#060913] text-[#f1f5fb] [color-scheme:dark] selection:bg-rose-600 selection:text-white"
    >
      {live && (
        <div
          role="img"
          aria-label={`Memory as a solar system: ${graph.nodes.length} entries and ${graph.links.length} connections. Use the search or the list of entries to find and open one.`}
          className="absolute inset-0"
        >
          <SolarCanvas
            graph={graph}
            orbits={orbits}
            revision={revision}
            selectedId={selectedId}
            matchIds={matchIds}
            group={group}
            labels={labels}
            insets={measureInsets}
            onSelect={pick}
            onReady={onReady}
          />
        </div>
      )}

      {status.kind === "loading" && <CanvasMessage title="Loading memory…" busy />}
      {status.kind === "signed-out" && (
        <CanvasMessage title="Sign in to see what Jarvis remembers about you.">
          <Link
            href="/login?callbackUrl=%2Fjarvis%2Fmemory"
            className={`mt-4 inline-flex h-10 items-center rounded-lg bg-[#f1f5fb] px-4 text-sm font-semibold text-[#0b1122] hover:bg-white ${focusRing}`}
          >
            Sign in
          </Link>
        </CanvasMessage>
      )}
      {status.kind === "error" && (
        <CanvasMessage title="Couldn't open Jarvis's memory." tone="error" detail={status.message}>
          <button type="button" onClick={() => void load()} className={`mt-4 ${glassButton}`}>
            Try again
          </button>
        </CanvasMessage>
      )}
      {empty && (
        <CanvasMessage
          title="Nothing remembered yet"
          detail="When you tell Jarvis something lasting - “My sister Nok lives in Chiang Mai” - it shows up here, in orbit round you, where you can check and correct it."
        >
          <Link href="/jarvis" className={`mt-4 ${glassButton}`}>
            Open the chat
          </Link>
        </CanvasMessage>
      )}

      <header
        data-memory-topbar=""
        className={`absolute inset-x-2 top-2 z-30 flex flex-wrap items-center gap-2 rounded-2xl p-2 md:inset-x-3 md:top-3 md:flex-nowrap ${glass}`}
      >
        <Link href="/jarvis" aria-label="Back to Jarvis" title="Back to Jarvis" className={glassIcon}>
          <ArrowLeft className="size-5" aria-hidden="true" />
        </Link>
        <div className="min-w-0 flex-1 md:flex-none md:pr-2">
          <h1 className="truncate text-[15px] font-semibold leading-5 tracking-tight text-[#f1f5fb]">Memory</h1>
          <p className="hidden truncate text-xs leading-4 text-[#a3b0c6] sm:block">What Jarvis remembers, in orbit round you</p>
        </div>

        {live && (
          <>
            <div role="search" className="order-last flex w-full min-w-0 gap-2 md:order-none md:w-auto md:max-w-xl md:flex-1">
              <SearchField
                query={query}
                matches={matches}
                onQuery={changeQuery}
                onPick={(id) => {
                  setQuery("");
                  open(id);
                }}
                onList={() => {
                  setSheetExpanded(true);
                  window.setTimeout(
                    () => document.querySelector<HTMLButtonElement>("[data-memory-panel] li button")?.focus(),
                    0,
                  );
                }}
              />
              <label htmlFor={filterId} className="sr-only">
                Type
              </label>
              <select
                id={filterId}
                value={group}
                onChange={(event) => setGroup(event.target.value as TypeGroupId | "all")}
                className={`h-10 w-[44%] min-w-0 shrink-0 pr-8 text-sm font-medium md:w-auto ${glassField}`}
              >
                <option value="all">All types ({graph.nodes.length})</option>
                {presentGroups.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.label} ({groupCounts.get(item.id)})
                  </option>
                ))}
              </select>
            </div>
            <div className="flex shrink-0 items-center gap-1.5 md:ml-auto md:gap-2">
              <button
                type="button"
                aria-pressed={labels}
                onClick={() => setLabels((value) => !value)}
                title={labels ? "Floating labels: on" : "Floating labels: off"}
                className={`inline-flex h-10 shrink-0 items-center justify-center gap-2 rounded-lg border px-3 text-sm font-semibold transition-[background-color,border-color,color,scale] duration-150 active:scale-[0.98] ${
                  labels
                    ? "border-white/25 bg-white/[0.12] text-[#f1f5fb] hover:bg-white/[0.16]"
                    : "border-white/[0.14] bg-white/[0.04] text-[#c9d3e3] hover:border-white/25 hover:bg-white/[0.09]"
                } ${focusRing}`}
              >
                <Tags className="size-4" aria-hidden="true" />
                <span className="sr-only lg:not-sr-only">Floating labels</span>
              </button>
              <button type="button" onClick={() => canvas.current?.fit()} title="Fit to view" className={glassButton}>
                <Maximize2 className="size-4" aria-hidden="true" />
                <span className="sr-only lg:not-sr-only">Fit to view</span>
              </button>
            </div>
          </>
        )}
      </header>

      {live && (
        <>
          <MemoryPanel
            nodes={graph.nodes}
            orbits={orbits}
            stats={stats}
            revision={revision}
            query={query}
            matches={matches}
            group={group}
            selected={selected}
            relations={relations}
            expanded={sheetExpanded}
            onExpanded={setSheetExpanded}
            onOpen={open}
            onBack={() => {
              setSelectedId(null);
              setSheetExpanded(true);
            }}
            onClose={() => {
              setSelectedId(null);
              setSheetExpanded(false);
            }}
            onClearSearch={() => setQuery("")}
            onSave={(changes) => (selected ? save(selected, changes) : Promise.resolve({ ok: true }))}
            onDelete={() => (selected ? deleteEntity(selected) : Promise.resolve({ ok: true }))}
            onDeleteRelation={deleteRelation}
            onRelationStatus={setRelationStatus}
          />

          {/* Bottom right, clear of the panel: the bottom-left corner is the Next.js dev tools' badge. */}
          <div
            role="group"
            aria-labelledby={legendId}
            className={`absolute bottom-16 right-2 z-20 max-w-[calc(100%-1rem)] rounded-xl ${glass} md:bottom-3 md:right-[23.5rem] ${
              legendOpen === null ? "w-32 md:w-60" : legendOpen ? "w-60" : "w-32"
            } ${sheetOpen ? "hidden md:block" : ""}`}
          >
            <button
              id={legendId}
              type="button"
              onClick={() => setLegendOpen((value) => !(value ?? window.matchMedia(DESKTOP).matches))}
              aria-expanded={legendOpen ?? undefined}
              className={`flex h-9 w-full items-center justify-between gap-2 rounded-xl px-3 text-xs font-semibold text-[#c9d3e3] hover:text-white ${focusRing}`}
            >
              Legend
              <ChevronDown
                className={`size-4 transition-transform duration-200 motion-reduce:transition-none ${legendOpen ? "" : "rotate-180"}`}
                aria-hidden="true"
              />
            </button>
            <div className={`px-3 pb-3 ${legendOpen === null ? "hidden md:block" : legendOpen ? "block" : "hidden"}`}>
              <ul aria-label="Entry types" className="grid grid-cols-2 gap-x-3 gap-y-1">
                {presentGroups.map((item) => (
                  <li
                    key={item.id}
                    className={`flex min-w-0 items-center gap-1.5 text-xs font-medium ${
                      group === "all" || group === item.id ? "text-[#e6ebf5]" : "text-[#8b98ae]"
                    }`}
                  >
                    <OrbDot type={item.id} kind="moon" />
                    <span className="truncate">{item.label}</span>
                    <span className="tabular-nums text-[#a3b0c6]">{groupCounts.get(item.id)}</span>
                  </li>
                ))}
              </ul>
              <ul aria-label="Orbits" className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-white/10 pt-2">
                <li className="flex items-center gap-1.5 text-xs font-medium text-[#e6ebf5]">
                  <OrbDot type="person" kind="sun" sun />
                  You
                </li>
                <li className="flex items-center gap-1.5 text-xs font-medium text-[#e6ebf5]">
                  <OrbDot type="person" kind="planet" />
                  1 hop
                </li>
                <li className="flex items-center gap-1.5 text-xs font-medium text-[#e6ebf5]">
                  <OrbDot type="person" kind="moon" />
                  2+ hops
                </li>
              </ul>
              <ul aria-label="Relations" className="mt-2 grid grid-cols-1 gap-1 border-t border-white/10 pt-2">
                {RELATION_STATUSES.map((relationStatus) => (
                  <li key={relationStatus} className="flex items-center gap-2 text-xs font-medium text-[#e6ebf5]">
                    <LineSample status={relationStatus} />
                    {RELATION_STATUS_LABEL[relationStatus]}
                    <span className="font-normal text-[#a3b0c6]">
                      {relationStatus === "active" ? "holds now" : relationStatus === "completed" ? "done" : "no longer true"}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          </div>
        </>
      )}

      {notice && (
        <p
          aria-hidden="true"
          className={`pointer-events-none absolute left-1/2 top-[7.5rem] z-30 max-w-[calc(100%-2rem)] -translate-x-1/2 truncate rounded-xl px-3 py-2 text-sm font-medium text-[#f1f5fb] md:left-[calc(50%-11.75rem)] md:top-20 md:max-w-[calc(100%-26rem)] ${glass}`}
        >
          {notice}
        </p>
      )}
      <p role="status" className="sr-only">
        {notice ?? ""}
      </p>
    </div>
  );
}

/** A 24px stretch of a relation's line as the canvas draws that status (RELATION_DASH). */
function LineSample({ status }: { status: RelationStatus }) {
  const dash = RELATION_DASH[status];
  const holds = status === "active";
  return (
    <svg aria-hidden="true" width="24" height="8" viewBox="0 0 24 8" className="shrink-0">
      {holds && <line x1="0" y1="4" x2="24" y2="4" stroke="rgba(150, 180, 255, 0.35)" strokeWidth="5" strokeLinecap="round" />}
      <line
        x1="0"
        y1="4"
        x2="24"
        y2="4"
        stroke={holds ? "rgba(214, 226, 255, 0.95)" : "#a3b0c6"}
        strokeWidth={holds ? 1.5 : 1.25}
        strokeDasharray={dash ? dash.join(" ") : undefined}
      />
    </svg>
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
    <div role={busy ? "status" : undefined} className="absolute inset-0 z-10 grid place-items-center px-4 pt-24 text-center">
      <div className={`max-w-sm rounded-2xl px-6 py-5 ${glass}`}>
        <p
          className={`text-[17px] font-bold tracking-tight ${tone === "error" ? "text-[#ff8095]" : "text-[#f1f5fb]"} ${
            busy ? "motion-safe:animate-pulse" : ""
          }`}
        >
          {title}
        </p>
        {detail && <p className="mt-2 text-sm leading-6 text-[#c9d3e3]">{detail}</p>}
        {children}
      </div>
    </div>
  );
}

/**
 * The search: typing lights up the matches on the canvas (the rest fade) and lists them in the
 * panel; Enter flies to the best match and opens it; Arrow Down moves into the list; Escape
 * clears the words.
 */
function SearchField({
  query,
  matches,
  onQuery,
  onPick,
  onList,
}: {
  query: string;
  matches: { id: string }[] | null;
  onQuery: (query: string) => void;
  onPick: (id: string) => void;
  onList: () => void;
}) {
  function onKeyDown(event: React.KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Enter") {
      const first = matches?.[0];
      if (first) {
        event.preventDefault();
        onPick(first.id);
      }
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      onList();
    } else if (event.key === "Escape" && query) {
      event.preventDefault();
      onQuery("");
    }
  }

  return (
    <div data-memory-search="" className="relative min-w-0 flex-1">
      <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-[#a3b0c6]" aria-hidden="true" />
      <input
        type="text"
        aria-label="Search memory"
        placeholder="Search memory"
        value={query}
        onChange={(event) => onQuery(event.target.value)}
        onKeyDown={onKeyDown}
        autoComplete="off"
        spellCheck={false}
        enterKeyHint="search"
        className={`h-10 w-full pl-9 pr-9 ${glassField}`}
      />
      {query && (
        <button
          type="button"
          onClick={() => onQuery("")}
          aria-label="Clear search"
          className={`absolute right-1 top-1/2 grid size-8 -translate-y-1/2 place-items-center rounded-md text-[#a3b0c6] hover:bg-white/[0.08] hover:text-white ${focusRing}`}
        >
          <X className="size-4" aria-hidden="true" />
        </button>
      )}
      <p role="status" className="sr-only">
        {matches === null ? "" : matches.length === 0 ? "No matches" : `${matches.length} match${matches.length === 1 ? "" : "es"}`}
      </p>
    </div>
  );
}
