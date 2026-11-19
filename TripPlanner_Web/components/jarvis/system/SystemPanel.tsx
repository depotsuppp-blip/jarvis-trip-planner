"use client";

import { useId, useRef } from "react";
import Link from "next/link";
import { ArrowLeft, ArrowUpRight, ChevronUp, Lock, ShieldAlert, X } from "lucide-react";
import {
  CATEGORY_COLORS,
  CORE_ID,
  KIND_LABEL,
  mixHex,
  OBSIDIAN,
  promptHref,
  SLATE,
  STATUS_LABEL,
  WARNING,
  type Capability,
  type ProviderKind,
  type ProviderStatus,
  type SearchHit,
  type SystemProvider,
  type SystemTopology,
} from "@/lib/jarvis/system";
import { caption, darkScroll, glass, glassIcon } from "../memory/solarStyles";
import { focusRing, focusRingInset } from "../styles";

/**
 * The System Topology page's inspector: on the right on a desktop, a bottom sheet on a phone.
 *
 *   nothing open   the system overview (status, active providers, tools, Jarvis's own model) and
 *                  every provider grouped by kind with its live status badge; while searching,
 *                  the matches instead
 *   a node open    its provider in three tabs - Overview (what it is, provider, model, access
 *                  method, status and why), Capabilities (its tools; a click flies the scene to
 *                  one) and Quick Actions (starter prompts that open the chat with the words in
 *                  the message field, ready to send)
 */

export type PanelTab = "overview" | "capabilities" | "actions";

const TABS: { id: PanelTab; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "capabilities", label: "Capabilities" },
  { id: "actions", label: "Quick Actions" },
];

const GROUPS: { kind: ProviderKind; label: string }[] = [
  { kind: "agent", label: "Agents" },
  { kind: "mcp_server", label: "MCP servers" },
  { kind: "core", label: "Jarvis core tools" },
];

export interface SystemPanelProps {
  topology: SystemTopology;
  /** The open provider (Jarvis itself for the core), or null for the overview. */
  provider: SystemProvider | null;
  /** The open node's id: the provider's, the core's, or one of its tools'. */
  selectedId: string | null;
  tab: PanelTab;
  query: string;
  hits: SearchHit[] | null;
  expanded: boolean;
  onTab: (tab: PanelTab) => void;
  onExpanded: (expanded: boolean) => void;
  onOpen: (id: string) => void;
  onBack: () => void;
  onClose: () => void;
  onClearSearch: () => void;
}

export function SystemPanel(props: SystemPanelProps) {
  const { topology, provider, expanded, onExpanded } = props;
  const open = expanded || provider !== null;
  const tools = topology.telemetry.total_capabilities;
  return (
    <aside
      data-system-panel=""
      aria-label={provider ? `${provider.name} details` : "System overview"}
      className={`absolute inset-x-0 bottom-0 z-30 flex max-h-[72svh] flex-col rounded-t-2xl border-b-0 ${glass} md:inset-x-auto md:bottom-3 md:right-3 md:top-20 md:max-h-none md:w-[23rem] md:rounded-2xl md:border-b`}
    >
      {!provider && (
        <button
          type="button"
          onClick={() => onExpanded(!expanded)}
          aria-expanded={open}
          className={`relative flex h-14 w-full shrink-0 items-center gap-3 px-4 pt-1 text-left md:hidden ${focusRingInset}`}
        >
          <span aria-hidden="true" className="absolute left-1/2 top-1.5 h-1 w-9 -translate-x-1/2 rounded-full bg-white/20" />
          <span className="min-w-0 flex-1 truncate text-sm font-semibold text-[#f1f5fb]">
            {topology.telemetry.total_providers} providers
            <span className="font-normal text-[#a3b0c6]">
              {" · "}
              {tools} tool{tools === 1 ? "" : "s"}
            </span>
          </span>
          <span className="text-xs font-medium text-[#c9d3e3]">{expanded ? "Hide" : "All providers"}</span>
          <ChevronUp
            className={`size-4 text-[#c9d3e3] transition-transform duration-200 motion-reduce:transition-none ${expanded ? "rotate-180" : ""}`}
            aria-hidden="true"
          />
        </button>
      )}
      <div className={`${open ? "flex" : "hidden"} min-h-0 flex-1 flex-col md:flex`}>
        {provider ? <Details key={provider.id} {...props} provider={provider} /> : <Overview {...props} />}
      </div>
    </aside>
  );
}

// ---- small pieces -------------------------------------------------------------------------------

/** A provider's orb as a dot: its family's colour, amber-ringed when it needs a sign-in, slate when offline. */
export function ProviderDot({ color, status, size = 11 }: { color: string; status: ProviderStatus; size?: number }) {
  const tone = status === "offline" ? mixHex(color, SLATE, 0.75) : color;
  return (
    <span aria-hidden="true" className="relative block shrink-0" style={{ width: size, height: size }}>
      <span
        className="block size-full rounded-full"
        style={{
          background: `radial-gradient(circle at 35% 32%, ${mixHex(tone, "#ffffff", 0.45)} 0%, ${tone} 55%, ${mixHex(tone, OBSIDIAN, 0.5)} 100%)`,
          opacity: status === "offline" ? 0.45 : 1,
          boxShadow: status === "offline" ? undefined : `0 0 9px color-mix(in srgb, ${tone} 55%, transparent)`,
        }}
      />
      {status === "auth_required" && <span className="absolute -inset-[3px] rounded-full border-[1.5px]" style={{ borderColor: WARNING }} />}
    </span>
  );
}

const BADGE: Record<ProviderStatus, string> = {
  online: "border-[#10b981]/40 bg-[#10b981]/12 text-[#34d399]",
  auth_required: "border-[#f59e0b]/45 bg-[#f59e0b]/12 text-[#fbbf24]",
  offline: "border-white/15 bg-white/[0.05] text-[#a3b0c6]",
};

export function StatusBadge({ status, title }: { status: ProviderStatus; title?: string }) {
  return (
    <span
      title={title}
      className={`inline-flex h-6 shrink-0 items-center gap-1 rounded-full border px-2 text-[11px] font-semibold tabular-nums ${BADGE[status]}`}
    >
      {status === "auth_required" && <ShieldAlert className="size-3" aria-hidden="true" />}
      {STATUS_LABEL[status]}
    </span>
  );
}

function colorOf(provider: SystemProvider): string {
  return CATEGORY_COLORS[provider.category];
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="min-w-0 rounded-xl border border-white/[0.08] bg-white/[0.04] px-3 py-2.5">
      <dt className="truncate text-xs font-medium text-[#a3b0c6]">{label}</dt>
      <dd className="mt-0.5 truncate text-xl font-bold tabular-nums leading-7 tracking-tight" style={{ color: tone ?? "#f1f5fb" }}>
        {value}
      </dd>
    </div>
  );
}

function ProviderRow({ provider, onOpen }: { provider: SystemProvider; onOpen: (id: string) => void }) {
  const id = provider.kind === "orchestrator" ? CORE_ID : provider.id;
  return (
    <li>
      <button
        type="button"
        onClick={() => onOpen(id)}
        className={`flex min-h-12 w-full items-center gap-3 rounded-lg px-2.5 py-2 text-left transition-colors duration-150 hover:bg-white/[0.06] ${focusRing}`}
      >
        <ProviderDot color={colorOf(provider)} status={provider.status} />
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm font-semibold text-[#f1f5fb]">{provider.name}</span>
          <span className="block truncate text-xs text-[#a3b0c6]">
            {[provider.vendor, provider.model, provider.access].filter(Boolean).join(" · ")}
          </span>
        </span>
        <StatusBadge status={provider.status} title={provider.status_note} />
      </button>
    </li>
  );
}

// ---- nothing open: the overview -----------------------------------------------------------------

function Overview({ topology, query, hits, onOpen, onClearSearch }: SystemPanelProps) {
  const { telemetry, orchestrator, providers, viewer } = topology;
  const optimal = telemetry.status === "optimal";
  const attention = providers.filter((p) => p.status !== "online");
  return (
    <div className={`min-h-0 flex-1 overflow-y-auto px-3 pb-4 pt-2 md:pt-4 ${darkScroll}`}>
      {hits !== null ? (
        <section aria-label="Search results">
          <div className="flex items-center justify-between gap-2 px-1">
            <h2 className={caption}>
              {hits.length} match{hits.length === 1 ? "" : "es"} for “{query.trim()}”
            </h2>
            <button type="button" onClick={onClearSearch} className={`rounded-md px-2 py-1 text-xs font-semibold text-[#c9d3e3] hover:text-white ${focusRing}`}>
              Clear
            </button>
          </div>
          {hits.length === 0 ? (
            <p className="mt-3 px-1 text-sm leading-6 text-[#c9d3e3]">Nothing matches. Try a tool name like “search”, or a provider like “Google”.</p>
          ) : (
            <ul className="mt-2 space-y-0.5">
              {hits.map((hit) => {
                const owner = hit.providerId === orchestrator.id ? orchestrator : providers.find((p) => p.id === hit.providerId);
                const capability = hit.kind === "capability" ? owner?.capabilities.find((c) => c.id === hit.id) : undefined;
                const status = capability?.status ?? owner?.status ?? "online";
                return (
                  <li key={hit.id}>
                    <button
                      type="button"
                      onClick={() => onOpen(hit.id)}
                      className={`flex min-h-11 w-full items-center gap-3 rounded-lg px-2.5 py-2 text-left hover:bg-white/[0.06] ${focusRing}`}
                    >
                      <ProviderDot color={owner ? colorOf(owner) : CATEGORY_COLORS.orchestrator} status={status} size={hit.kind === "capability" ? 8 : 11} />
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-sm font-semibold text-[#f1f5fb]">{hit.label}</span>
                        <span className="block truncate text-xs text-[#a3b0c6]">
                          {hit.kind === "capability" ? `Tool of ${owner?.name ?? "Jarvis"}` : owner ? KIND_LABEL[owner.kind] : ""}
                        </span>
                      </span>
                      <StatusBadge status={status} />
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </section>
      ) : (
        <>
          <section aria-labelledby="system-overview">
            <h2 id="system-overview" className={`px-1 ${caption}`}>
              System overview
            </h2>
            <dl className="mt-2 grid grid-cols-[1.4fr_1fr_1fr] gap-2">
              <Stat label="Status" value={optimal ? "Optimal" : "Degraded"} tone={optimal ? "#34d399" : "#fbbf24"} />
              <Stat label="Active" value={`${telemetry.active_providers}/${telemetry.total_providers}`} />
              <Stat label="Tools" value={String(telemetry.total_capabilities)} />
            </dl>
            {attention.length > 0 && (
              <p className="mt-2 px-1 text-xs leading-5 text-[#c9d3e3]">
                Needs attention: {attention.map((p) => p.name).join(", ")}.
              </p>
            )}
            <ul className="mt-3">
              <ProviderRow provider={orchestrator} onOpen={onOpen} />
            </ul>
          </section>
          {GROUPS.map(({ kind, label }) => {
            const rows = providers.filter((p) => p.kind === kind);
            if (!rows.length) return null;
            return (
              <section key={kind} aria-label={label} className="mt-4 border-t border-white/[0.08] pt-3">
                <h2 className={`flex items-center justify-between px-1 ${caption}`}>
                  {label}
                  <span className="tabular-nums">{rows.length}</span>
                </h2>
                <ul className="mt-1.5 space-y-0.5">
                  {rows.map((provider) => (
                    <ProviderRow key={provider.id} provider={provider} onOpen={onOpen} />
                  ))}
                </ul>
              </section>
            );
          })}
          {viewer.hidden > 0 && (
            <p className="mt-4 flex items-start gap-2 border-t border-white/[0.08] px-1 pt-3 text-xs leading-5 text-[#a3b0c6]">
              <Lock className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
              {viewer.hidden} more {viewer.hidden === 1 ? "service is" : "services are"} for Jarvis&apos;s owner only and not shown.
            </p>
          )}
        </>
      )}
    </div>
  );
}

// ---- a node open: three tabs ---------------------------------------------------------------------

function Details(props: SystemPanelProps & { provider: SystemProvider }) {
  const { provider, tab, onTab, onBack, onClose } = props;
  const base = useId();
  const tabs = useRef<(HTMLButtonElement | null)[]>([]);
  // An agent's count includes the tools it reaches through its tool servers (listed in the tab).
  const through = provider.uses.reduce(
    (sum, id) => sum + (props.topology.providers.find((p) => p.id === id)?.capabilities.length ?? 0),
    0,
  );
  const counts: Record<PanelTab, number | null> = {
    overview: null,
    capabilities: provider.kind === "orchestrator" ? props.topology.providers.length : provider.capabilities.length + through,
    actions: provider.workflows.length,
  };

  function onKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    const index = TABS.findIndex((item) => item.id === tab);
    const next =
      event.key === "ArrowRight" ? (index + 1) % TABS.length
      : event.key === "ArrowLeft" ? (index + TABS.length - 1) % TABS.length
      : event.key === "Home" ? 0
      : event.key === "End" ? TABS.length - 1
      : -1;
    if (next < 0) return;
    event.preventDefault();
    onTab(TABS[next].id);
    tabs.current[next]?.focus();
  }

  return (
    <>
      <div className="flex shrink-0 items-start gap-2 px-2 pb-2 pt-2">
        <button type="button" onClick={onBack} aria-label="Back to all providers" title="Back to all providers" className={glassIcon}>
          <ArrowLeft className="size-5" aria-hidden="true" />
        </button>
        <div className="min-w-0 flex-1 pt-0.5">
          <div className="flex items-center gap-2">
            <ProviderDot color={colorOf(provider)} status={provider.status} size={12} />
            <h2 className="truncate text-[17px] font-bold leading-6 tracking-tight text-[#f1f5fb]">{provider.name}</h2>
          </div>
          <p className="mt-0.5 truncate text-xs text-[#a3b0c6]">{provider.title || KIND_LABEL[provider.kind]}</p>
        </div>
        <div className="flex items-center gap-1 pt-1.5">
          <StatusBadge status={provider.status} title={provider.status_note} />
          <button type="button" onClick={onClose} aria-label="Close details" title="Close" className={`${glassIcon} md:hidden`}>
            <X className="size-5" aria-hidden="true" />
          </button>
        </div>
      </div>

      <div role="tablist" aria-label={`${provider.name} sections`} onKeyDown={onKeyDown} className="mx-3 flex shrink-0 gap-1 rounded-lg border border-white/[0.08] bg-black/20 p-1">
        {TABS.map((item, index) => {
          const selected = item.id === tab;
          return (
            <button
              key={item.id}
              ref={(element) => {
                tabs.current[index] = element;
              }}
              id={`${base}-tab-${item.id}`}
              type="button"
              role="tab"
              aria-selected={selected}
              aria-controls={`${base}-panel-${item.id}`}
              tabIndex={selected ? 0 : -1}
              onClick={() => onTab(item.id)}
              className={`flex h-9 min-w-0 flex-1 items-center justify-center gap-1.5 rounded-md px-1.5 text-[13px] font-semibold transition-colors duration-150 ${
                selected ? "bg-white/[0.12] text-[#f1f5fb]" : "text-[#a3b0c6] hover:bg-white/[0.06] hover:text-[#f1f5fb]"
              } ${focusRingInset}`}
            >
              <span className="truncate">{item.label}</span>
              {counts[item.id] !== null && <span className="text-[11px] font-medium tabular-nums text-[#a3b0c6]">{counts[item.id]}</span>}
            </button>
          );
        })}
      </div>

      <div
        id={`${base}-panel-${tab}`}
        role="tabpanel"
        aria-labelledby={`${base}-tab-${tab}`}
        tabIndex={0}
        className={`min-h-0 flex-1 overflow-y-auto px-3 pb-4 pt-3 outline-none ${darkScroll}`}
      >
        {tab === "overview" && <OverviewTab {...props} />}
        {tab === "capabilities" && <CapabilitiesTab {...props} />}
        {tab === "actions" && <ActionsTab provider={provider} />}
      </div>
    </>
  );
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-[11px] font-medium text-[#a3b0c6]">{label}</dt>
      <dd className="mt-0.5 truncate text-sm font-semibold text-[#f1f5fb]">{children}</dd>
    </div>
  );
}

function OverviewTab({ provider, topology, onOpen }: SystemPanelProps & { provider: SystemProvider }) {
  const used = provider.uses
    .map((id) => topology.providers.find((p) => p.id === id))
    .filter((p): p is SystemProvider => p !== undefined);
  const tone = provider.status === "online" ? "#34d399" : provider.status === "auth_required" ? "#fbbf24" : "#c9d3e3";
  return (
    <div className="space-y-4">
      {provider.description && <p className="text-sm leading-6 text-[#c9d3e3]">{provider.description}</p>}
      <dl className="grid grid-cols-2 gap-x-3 gap-y-2.5 rounded-xl border border-white/[0.08] bg-white/[0.03] px-3 py-3">
        <Fact label="Provider">{provider.vendor || "—"}</Fact>
        <Fact label="Access method">{provider.access}</Fact>
        <Fact label="Model">{provider.model ?? "No model"}</Fact>
        <Fact label="Type">{KIND_LABEL[provider.kind]}</Fact>
      </dl>

      <section aria-label="Status and diagnostics">
        <h3 className={caption}>Status</h3>
        <p className="mt-1.5 text-sm font-semibold" style={{ color: tone }}>
          {STATUS_LABEL[provider.status]}
          {provider.status_note && provider.status_note !== "Ready" && <span className="font-medium text-[#c9d3e3]"> · {provider.status_note}</span>}
        </p>
        {provider.diagnostics.length > 0 ? (
          <ul className="mt-1.5 space-y-1.5">
            {provider.diagnostics.map((line) => (
              <li key={line} className="text-[13px] leading-5 text-[#c9d3e3]">
                {line}
              </li>
            ))}
          </ul>
        ) : (
          provider.status === "online" && <p className="mt-1 text-[13px] leading-5 text-[#a3b0c6]">Working normally.</p>
        )}
        {provider.missing_scopes.length > 0 && (
          <div className="mt-2.5 rounded-lg border border-[#f59e0b]/35 bg-[#f59e0b]/[0.08] px-3 py-2.5">
            <p className="text-xs font-semibold text-[#fbbf24]">Missing OAuth scope{provider.missing_scopes.length === 1 ? "" : "s"}</p>
            <ul className="mt-1 space-y-1">
              {provider.missing_scopes.map((scope) => (
                <li key={scope} className="break-all font-mono text-[12px] leading-5 text-[#f1f5fb]">
                  {scope.replace("https://www.googleapis.com/auth/", "")}
                </li>
              ))}
            </ul>
          </div>
        )}
      </section>

      {provider.routes.length > 0 && (
        <section aria-label="Models">
          <h3 className={caption}>Models, in the order tried</h3>
          <ol className="mt-1.5 space-y-1">
            {provider.routes.map((route, index) => (
              <li key={`${route.role}-${route.model}-${index}`} className="flex items-baseline gap-2 text-[13px] leading-5">
                <span className="w-20 shrink-0 text-[#a3b0c6]">{route.role}</span>
                <span className="min-w-0 flex-1 truncate font-medium text-[#f1f5fb]">
                  {route.vendor} {route.model}
                </span>
                {route.state && (
                  <span className={`shrink-0 text-xs ${route.state === "ready" ? "text-[#34d399]" : "text-[#fbbf24]"}`}>
                    {route.state}
                    {route.error ? ` · ${route.error}` : ""}
                  </span>
                )}
              </li>
            ))}
          </ol>
        </section>
      )}

      {used.length > 0 && (
        <section aria-label="Works through">
          <h3 className={caption}>Works through</h3>
          <ul className="mt-1 space-y-0.5">
            {used.map((server) => (
              <ProviderRow key={server.id} provider={server} onOpen={onOpen} />
            ))}
          </ul>
        </section>
      )}

      {provider.owner_only && (
        <p className="flex items-start gap-2 text-xs leading-5 text-[#a3b0c6]">
          <Lock className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
          Owner only: nobody else signed in to the web app is offered this.
        </p>
      )}
    </div>
  );
}

function CapabilityRow({ capability, current, onOpen }: { capability: Capability; current: boolean; onOpen: (id: string) => void }) {
  return (
    <li>
      <button
        type="button"
        onClick={() => onOpen(capability.id)}
        aria-current={current ? "true" : undefined}
        className={`flex w-full items-start gap-3 rounded-lg px-2.5 py-2 text-left transition-colors duration-150 ${
          current ? "bg-white/[0.1]" : "hover:bg-white/[0.06]"
        } ${focusRing}`}
      >
        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-2">
            <span className="truncate text-sm font-semibold text-[#f1f5fb]">{capability.label}</span>
            {capability.needs_approval && (
              <span className="shrink-0 rounded border border-white/15 px-1 text-[10px] font-semibold uppercase tracking-wide text-[#c9d3e3]">Asks first</span>
            )}
          </span>
          <span className="block truncate font-mono text-[11px] text-[#8b98ae]">{capability.name}</span>
          {capability.description && <span className="mt-0.5 line-clamp-2 block text-xs leading-5 text-[#c9d3e3]">{capability.description}</span>}
          {capability.note && <span className="mt-0.5 block text-xs leading-5 text-[#a3b0c6]">{capability.note}</span>}
        </span>
        <StatusBadge status={capability.status} />
      </button>
    </li>
  );
}

function CapabilitiesTab({ provider, topology, selectedId, onOpen }: SystemPanelProps & { provider: SystemProvider }) {
  if (provider.kind === "orchestrator") {
    return (
      <div>
        <p className="px-1 text-[13px] leading-5 text-[#c9d3e3]">Jarvis does its work through these. Open one to see its tools.</p>
        <ul className="mt-2 space-y-0.5">
          {topology.providers.map((p) => (
            <ProviderRow key={p.id} provider={p} onOpen={onOpen} />
          ))}
        </ul>
      </div>
    );
  }
  const through = provider.uses
    .map((id) => topology.providers.find((p) => p.id === id))
    .filter((p): p is SystemProvider => p !== undefined && p.capabilities.length > 0);
  return (
    <div className="space-y-4">
      {provider.capabilities.length > 0 ? (
        <ul className="space-y-0.5">
          {provider.capabilities.map((capability) => (
            <CapabilityRow key={capability.id} capability={capability} current={capability.id === selectedId} onOpen={onOpen} />
          ))}
        </ul>
      ) : (
        through.length === 0 && <p className="px-1 text-sm leading-6 text-[#c9d3e3]">No tools are listed for this right now.</p>
      )}
      {through.map((server) => (
        <section key={server.id} aria-label={`Through ${server.name}`}>
          <h3 className={`px-1 ${caption}`}>Through {server.name}</h3>
          <ul className="mt-1 space-y-0.5">
            {server.capabilities.map((capability) => (
              <CapabilityRow key={capability.id} capability={capability} current={capability.id === selectedId} onOpen={onOpen} />
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

function ActionsTab({ provider }: { provider: SystemProvider }) {
  if (!provider.workflows.length) return <p className="px-1 text-sm leading-6 text-[#c9d3e3]">No starter prompts for this one.</p>;
  return (
    <div>
      <p className="px-1 text-[13px] leading-5 text-[#c9d3e3]">Opens the chat with the request in the message field, ready to send.</p>
      {provider.status !== "online" && (
        <p className="mt-2 rounded-lg border border-white/[0.1] bg-white/[0.04] px-3 py-2 text-xs leading-5 text-[#c9d3e3]">
          {provider.name} is {STATUS_LABEL[provider.status].toLowerCase()} right now, so Jarvis will tell you what&apos;s missing instead.
        </p>
      )}
      <ul className="mt-3 space-y-2">
        {provider.workflows.map((flow) => (
          <li key={flow.label}>
            <Link
              href={promptHref(flow.prompt)}
              className={`group flex w-full items-start gap-3 rounded-xl border border-white/[0.12] bg-white/[0.04] px-3 py-2.5 text-left transition-[background-color,border-color,scale] duration-150 hover:border-white/25 hover:bg-white/[0.09] active:scale-[0.99] ${focusRing}`}
            >
              <span className="min-w-0 flex-1">
                <span className="block text-sm font-semibold text-[#f1f5fb]">{flow.label}</span>
                <span className="mt-0.5 line-clamp-2 block text-xs leading-5 text-[#c9d3e3]">“{flow.prompt}”</span>
              </span>
              <ArrowUpRight className="mt-0.5 size-4 shrink-0 text-[#a3b0c6] transition-colors group-hover:text-white" aria-hidden="true" />
            </Link>
          </li>
        ))}
      </ul>
      <p className="mt-3 px-1 text-[11px] leading-4 text-[#8b98ae]">Nothing is sent until you press Send.</p>
    </div>
  );
}
