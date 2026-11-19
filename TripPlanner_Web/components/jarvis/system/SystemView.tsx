"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { Activity, ArrowLeft, Maximize2, Search, Tags, X } from "lucide-react";
import {
  buildGraph,
  CATEGORY_COLORS,
  CATEGORY_LABEL,
  CORE_ID,
  FILTERS,
  KIND_LABEL,
  loadSystemTopology,
  searchTopology,
  shapeKey,
  STATUS_LABEL,
  type ProviderKind,
  type SystemProvider,
  type SystemTopology,
  type TopoGraph,
  type TopologyFilter,
} from "@/lib/jarvis/system";
// Types only: a value imported from the scene module would pull Three.js into server rendering.
import type { Insets, NodeInfo, TopologySceneApi } from "./TopologyScene";
import { ProviderDot, SystemPanel, type PanelTab } from "./SystemPanel";
import { glass, glassButton, glassField, glassIcon } from "../memory/solarStyles";
import { focusRing } from "../styles";

const TopologyScene = dynamic(() => import("./TopologyScene"), {
  ssr: false,
  loading: () => <CanvasMessage title="Drawing Jarvis's systems…" busy />,
});

type Status = { kind: "loading" } | { kind: "ready" } | { kind: "signed-out" } | { kind: "error"; message: string };

const DESKTOP = "(min-width: 768px)";
// Live: the topology is read again this often while the page is in view.
const REFRESH_MS = 30_000;

function measureInsets(): Insets {
  const insets: Insets = { top: 0, right: 0, bottom: 0, left: 0 };
  const stage = document.querySelector<HTMLElement>("[data-system-stage]")?.getBoundingClientRect();
  if (!stage) return insets;
  const bar = document.querySelector<HTMLElement>("[data-system-topbar]")?.getBoundingClientRect();
  if (bar) insets.top = Math.max(0, bar.bottom - stage.top);
  const panel = document.querySelector<HTMLElement>("[data-system-panel]")?.getBoundingClientRect();
  if (panel && panel.width > 0 && panel.height > 0) {
    if (window.matchMedia(DESKTOP).matches) insets.right = Math.max(0, stage.right - panel.left);
    else insets.bottom = Math.max(0, stage.bottom - panel.top);
  }
  return insets;
}

/** Statuses only: what can change in place without rebuilding the scene. */
function statusKey(topology: SystemTopology): string {
  return [topology.orchestrator, ...topology.providers]
    .map((p) => `${p.status}:${p.capabilities.map((c) => c.status).join("")}`)
    .join("|");
}

/**
 * The System Topology page (/jarvis/system): Jarvis's infrastructure as a live 3D map - what it
 * can do, which agent or tool server does it, whether that works right now, and what to ask it
 * next - on obsidian black, under glass.
 *
 *   top bar   back to Jarvis, the page's name, search (matches light up; Enter flies to the
 *             first), filter pills (All, Agents, MCP Servers, Capabilities), Floating labels,
 *             Fit to view
 *   health    the System Health widget under the bar, top left (the bottom-left corner is the
 *             Next.js dev tools' badge): Optimal or Degraded, active providers, tools
 *   scene     TopologyScene (next/dynamic, browser only, WebGL): Jarvis at the centre, providers
 *             on the inner orbit, their tools on the outer one
 *   panel     SystemPanel: the overview and every provider, or the open one in three tabs - on
 *             the right on a desktop, a bottom sheet on a phone
 *
 * The data is the brain's own live status (GET /api/system/topology), read again every 30
 * seconds while the page is in view: a status that changes repaints in place; a provider or a
 * tool that appears or goes rebuilds the map.
 */
export function SystemView() {
  const [status, setStatus] = useState<Status>({ kind: "loading" });
  const [topology, setTopology] = useState<SystemTopology | null>(null);
  const [graph, setGraph] = useState<TopoGraph>({ nodes: [], links: [], rings: [] });
  const [revision, setRevision] = useState(0);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [tab, setTab] = useState<PanelTab>("overview");
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<TopologyFilter>("all");
  const [labels, setLabels] = useState(true);
  const [sheetExpanded, setSheetExpanded] = useState(false);
  const scene = useRef<TopologySceneApi | null>(null);
  const shape = useRef("");
  const statuses = useRef("");
  /** A new reading: same shape repaints in place, a new shape is a new map. */
  const take = useCallback((data: SystemTopology) => {
    const nextShape = shapeKey(data);
    const nextStatuses = statusKey(data);
    setTopology(data);
    if (nextShape !== shape.current) {
      shape.current = nextShape;
      statuses.current = nextStatuses;
      setGraph(buildGraph(data));
      return;
    }
    if (nextStatuses === statuses.current) return;
    statuses.current = nextStatuses;
    const fresh = buildGraph(data);
    setGraph((current) => {
      // A link that starts or stops working changes its line and its particles: a new map.
      if (fresh.links.some((link, index) => current.links[index]?.active !== link.active)) return fresh;
      // Otherwise in place: the scene keeps every object and just repaints the statuses.
      const byId = new Map(fresh.nodes.map((node) => [node.id, node]));
      for (const node of current.nodes) node.status = byId.get(node.id)?.status ?? node.status;
      return current;
    });
    setRevision((value) => value + 1);
  }, []);

  const load = useCallback(
    async (quiet = false, signal?: AbortSignal) => {
      if (!quiet) setStatus({ kind: "loading" });
      const result = await loadSystemTopology(signal);
      if (signal?.aborted) return;
      if (result.ok) {
        take(result.data);
        setStatus({ kind: "ready" });
      } else if (result.status === 401) {
        setStatus({ kind: "signed-out" });
      } else if (!quiet) {
        setStatus({ kind: "error", message: result.message });
      }
    },
    [take],
  );

  useEffect(() => {
    const controller = new AbortController();
    // The first load, once the page is in the browser.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load(false, controller.signal);
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void load(true, controller.signal);
    }, REFRESH_MS);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [load]);

  const providerById = useMemo(() => {
    const map = new Map<string, SystemProvider>();
    if (!topology) return map;
    map.set(CORE_ID, topology.orchestrator);
    for (const provider of topology.providers) map.set(provider.id, provider);
    return map;
  }, [topology]);

  const kinds = useMemo(() => {
    const map = new Map<string, ProviderKind>();
    for (const [id, provider] of providerById) map.set(id, provider.kind);
    return map;
  }, [providerById]);

  const info = useMemo(() => {
    const map = new Map<string, NodeInfo>();
    if (!topology) return map;
    const o = topology.orchestrator;
    map.set(CORE_ID, {
      title: "Jarvis",
      detail: [o.vendor, o.model].filter(Boolean).join(" ") || "Orchestrator",
      status: o.status,
      color: CATEGORY_COLORS.orchestrator,
      note: o.diagnostics[0],
    });
    for (const p of topology.providers) {
      map.set(p.id, {
        title: p.name,
        detail: `${KIND_LABEL[p.kind]} · ${p.vendor}${p.model ? ` ${p.model}` : ""}`,
        status: p.status,
        color: CATEGORY_COLORS[p.category],
        note: p.status === "online" ? `${p.capabilities.length} tool${p.capabilities.length === 1 ? "" : "s"}` : p.diagnostics[0],
      });
      for (const c of p.capabilities) {
        map.set(c.id, {
          title: c.label,
          detail: `Tool of ${p.name}`,
          status: c.status,
          color: CATEGORY_COLORS[p.category],
          note: c.note || c.description,
        });
      }
    }
    return map;
  }, [topology]);

  const hits = useMemo(() => (topology && query.trim() ? searchTopology(topology, query) : null), [topology, query]);
  const matchIds = useMemo(() => (hits ? new Set(hits.map((hit) => hit.id)) : null), [hits]);

  // The open node's provider: itself, the one a tool belongs to, or Jarvis for the core.
  const openProvider = useMemo(() => {
    if (!selectedId) return null;
    const node = graph.nodes.find((item) => item.id === selectedId);
    return node ? (providerById.get(node.providerId) ?? null) : null;
  }, [selectedId, graph, providerById]);

  const onReady = useCallback((api: TopologySceneApi) => {
    scene.current = api;
  }, []);

  const reveal = useCallback((id: string) => {
    window.setTimeout(() => scene.current?.focus(id), 0);
  }, []);

  /** Opens a node from the panel, the search or the scene: the camera flies to it. */
  const open = useCallback(
    (id: string) => {
      const node = graph.nodes.find((item) => item.id === id);
      if (!node) return;
      const kind = kinds.get(node.providerId);
      // Under a filter that hides it, it would open off-screen: show everything again.
      if (filter !== "all" && filter !== "capabilities" && kind && kind !== "orchestrator") {
        const allowed = filter === "agents" ? kind === "agent" : kind === "mcp_server";
        if (!allowed) setFilter("all");
      }
      setSelectedId(id);
      setTab(node.kind === "capability" ? "capabilities" : "overview");
      reveal(id);
    },
    [graph, kinds, filter, reveal],
  );

  const pick = useCallback(
    (id: string | null) => {
      if (id) open(id);
      else setSelectedId(null);
    },
    [open],
  );

  const changeQuery = useCallback((text: string) => {
    setQuery(text);
    if (text.trim()) {
      setSelectedId(null);
      setSheetExpanded(true);
    }
  }, []);

  const ready = status.kind === "ready" && topology !== null;
  const telemetry = topology?.telemetry;
  const optimal = telemetry?.status === "optimal";

  return (
    <div
      data-system-stage=""
      className="relative h-dvh w-full overflow-hidden bg-[#050508] text-[#f1f5fb] [color-scheme:dark] selection:bg-rose-600 selection:text-white"
    >
      {ready && (
        <div
          role="img"
          aria-label={`Jarvis's system topology: ${topology.telemetry.total_providers} providers, ${topology.telemetry.active_providers} of them active, and ${topology.telemetry.total_capabilities} tools. The panel lists every provider and its tools.`}
          className="absolute inset-0"
        >
          <TopologyScene
            graph={graph}
            revision={revision}
            selectedId={selectedId}
            matchIds={matchIds}
            filter={filter}
            kinds={kinds}
            info={info}
            labels={labels}
            insets={measureInsets}
            onSelect={pick}
            onReady={onReady}
          />
        </div>
      )}

      {status.kind === "loading" && <CanvasMessage title="Reading Jarvis's systems…" busy />}
      {status.kind === "signed-out" && (
        <CanvasMessage title="Sign in to see Jarvis's systems.">
          <Link
            href="/login?callbackUrl=%2Fjarvis%2Fsystem"
            className={`mt-4 inline-flex h-10 items-center rounded-lg bg-[#f1f5fb] px-4 text-sm font-semibold text-[#0b1122] hover:bg-white ${focusRing}`}
          >
            Sign in
          </Link>
        </CanvasMessage>
      )}
      {status.kind === "error" && (
        <CanvasMessage title="Couldn't read Jarvis's systems." tone="error" detail={status.message}>
          <button type="button" onClick={() => void load()} className={`mt-4 ${glassButton}`}>
            Try again
          </button>
        </CanvasMessage>
      )}

      <div className="pointer-events-none absolute inset-x-2 top-2 z-30 flex flex-col items-start gap-2 md:inset-x-3 md:top-3">
        <header
          data-system-topbar=""
          className={`pointer-events-auto flex w-full flex-wrap items-center gap-2 rounded-2xl p-2 md:flex-nowrap ${glass}`}
        >
          <Link href="/jarvis" aria-label="Back to Jarvis" title="Back to Jarvis" className={glassIcon}>
            <ArrowLeft className="size-5" aria-hidden="true" />
          </Link>
          <div className="min-w-0 flex-1 md:flex-none md:pr-2">
            <h1 className="truncate text-[15px] font-semibold leading-5 tracking-tight text-[#f1f5fb]">System Topology</h1>
            <p className="hidden truncate text-xs leading-4 text-[#a3b0c6] sm:block">What Jarvis can do, and who does it</p>
          </div>

          {ready && (
            <>
              <div role="search" className="order-last w-full min-w-0 md:order-none md:w-auto md:max-w-xs md:flex-1 lg:max-w-sm">
                <SearchField
                  query={query}
                  hits={hits}
                  onQuery={changeQuery}
                  onPick={(id) => {
                    setQuery("");
                    open(id);
                  }}
                />
              </div>
              <div
                role="group"
                aria-label="Show"
                className="order-last -mx-1 flex w-[calc(100%+0.5rem)] min-w-0 gap-1.5 overflow-x-auto px-1 pb-0.5 [scrollbar-width:none] md:order-none md:mx-0 md:w-auto md:overflow-visible md:p-0"
              >
                {FILTERS.map((item) => {
                  const pressed = filter === item.id;
                  return (
                    <button
                      key={item.id}
                      type="button"
                      aria-pressed={pressed}
                      onClick={() => setFilter(item.id)}
                      className={`inline-flex h-9 shrink-0 items-center rounded-full border px-3.5 text-[13px] font-semibold transition-[background-color,border-color,color,scale] duration-150 active:scale-[0.98] ${
                        pressed
                          ? "border-[#7cc4ff]/50 bg-[#2e90fa]/20 text-[#f1f5fb]"
                          : "border-white/[0.14] bg-white/[0.04] text-[#c9d3e3] hover:border-white/25 hover:bg-white/[0.09]"
                      } ${focusRing}`}
                    >
                      {item.label}
                    </button>
                  );
                })}
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
                  <span className="sr-only xl:not-sr-only">Floating Labels</span>
                </button>
                <button type="button" onClick={() => scene.current?.fit()} title="Fit to view" className={glassButton}>
                  <Maximize2 className="size-4" aria-hidden="true" />
                  <span className="sr-only xl:not-sr-only">Fit to View</span>
                </button>
              </div>
            </>
          )}
        </header>

        {ready && telemetry && (
          <section
            data-system-health=""
            aria-label="System health"
            className={`pointer-events-auto flex items-center gap-3 rounded-xl px-3 py-2 md:min-w-56 md:flex-col md:items-stretch md:gap-2 md:py-3 ${glass}`}
          >
            <div className="flex items-center gap-2">
              <span aria-hidden="true" className="relative grid size-2.5 place-items-center">
                <span
                  className={`absolute inline-flex size-full rounded-full opacity-60 motion-safe:animate-ping ${optimal ? "bg-[#10b981]" : "bg-[#f59e0b]"}`}
                />
                <span className={`relative inline-flex size-2.5 rounded-full ${optimal ? "bg-[#10b981]" : "bg-[#f59e0b]"}`} />
              </span>
              <span className="hidden text-[11px] font-semibold uppercase tracking-[0.08em] text-[#a3b0c6] md:inline">System health</span>
              <span className={`text-sm font-bold ${optimal ? "text-[#34d399]" : "text-[#fbbf24]"} md:ml-auto`}>{optimal ? "Optimal" : "Degraded"}</span>
            </div>
            <dl className="flex items-center gap-3 text-xs md:grid md:grid-cols-2 md:gap-2">
              <div className="flex items-baseline gap-1 md:block">
                <dt className="text-[#a3b0c6] md:text-[11px]">
                  <span className="md:hidden">Active</span>
                  <span className="hidden md:inline">Active providers</span>
                </dt>
                <dd className="font-bold tabular-nums text-[#f1f5fb] md:text-lg md:leading-6">
                  {telemetry.active_providers}/{telemetry.total_providers}
                </dd>
              </div>
              <div className="flex items-baseline gap-1 md:block">
                <dt className="text-[#a3b0c6] md:text-[11px]">Tools</dt>
                <dd className="font-bold tabular-nums text-[#f1f5fb] md:text-lg md:leading-6">{telemetry.total_capabilities}</dd>
              </div>
            </dl>
            <Legend />
          </section>
        )}
      </div>

      {ready && (
        <SystemPanel
          topology={topology}
          provider={openProvider}
          selectedId={selectedId}
          tab={tab}
          query={query}
          hits={hits}
          expanded={sheetExpanded}
          onTab={setTab}
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
        />
      )}

      <p role="status" className="sr-only">
        {ready && telemetry
          ? `System ${optimal ? "optimal" : "degraded"}: ${telemetry.active_providers} of ${telemetry.total_providers} providers active.`
          : ""}
      </p>
    </div>
  );
}

/** The colour families and statuses, folded into the health widget on a desktop. */
function Legend() {
  const families = ["agent", "mcp", "data", "core"] as const;
  return (
    <div className="hidden border-t border-white/10 pt-2 md:block">
      <ul aria-label="Colours" className="grid grid-cols-2 gap-x-3 gap-y-1">
        {families.map((family) => (
          <li key={family} className="flex items-center gap-1.5 text-[11px] font-medium text-[#e6ebf5]">
            <ProviderDot color={CATEGORY_COLORS[family]} status="online" size={8} />
            {CATEGORY_LABEL[family]}
          </li>
        ))}
      </ul>
      <ul aria-label="Statuses" className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1">
        <li className="flex items-center gap-1.5 text-[11px] font-medium text-[#e6ebf5]">
          <ProviderDot color={CATEGORY_COLORS.agent} status="auth_required" size={8} />
          {STATUS_LABEL.auth_required}
        </li>
        <li className="flex items-center gap-1.5 text-[11px] font-medium text-[#e6ebf5]">
          <ProviderDot color={CATEGORY_COLORS.agent} status="offline" size={8} />
          {STATUS_LABEL.offline}
        </li>
      </ul>
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
    <div role={busy ? "status" : undefined} className="absolute inset-0 z-10 grid place-items-center px-4 pt-24 text-center">
      <div className={`max-w-sm rounded-2xl px-6 py-5 ${glass}`}>
        <p
          className={`text-[17px] font-bold tracking-tight ${tone === "error" ? "text-[#ff8095]" : "text-[#f1f5fb]"} ${
            busy ? "motion-safe:animate-pulse" : ""
          }`}
        >
          {busy && <Activity className="mr-2 inline size-4 align-[-2px]" aria-hidden="true" />}
          {title}
        </p>
        {detail && <p className="mt-2 text-sm leading-6 text-[#c9d3e3]">{detail}</p>}
        {children}
      </div>
    </div>
  );
}

/** Search: matches light up in the scene and list in the panel; Enter flies to the first; Escape clears. */
function SearchField({
  query,
  hits,
  onQuery,
  onPick,
}: {
  query: string;
  hits: { id: string }[] | null;
  onQuery: (query: string) => void;
  onPick: (id: string) => void;
}) {
  function onKeyDown(event: React.KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Enter") {
      const first = hits?.[0];
      if (first) {
        event.preventDefault();
        onPick(first.id);
      }
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      document.querySelector<HTMLButtonElement>("[data-system-panel] li button")?.focus();
    } else if (event.key === "Escape" && query) {
      event.preventDefault();
      onQuery("");
    }
  }

  return (
    <div className="relative min-w-0">
      <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-[#a3b0c6]" aria-hidden="true" />
      <input
        type="text"
        aria-label="Search agents, servers and tools"
        placeholder="Search agents, servers, tools"
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
        {hits === null ? "" : hits.length === 0 ? "No matches" : `${hits.length} match${hits.length === 1 ? "" : "es"}`}
      </p>
    </div>
  );
}

