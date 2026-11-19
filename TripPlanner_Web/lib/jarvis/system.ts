/**
 * The System Topology page (/jarvis/system), browser side: the shape the brain's topology arrives
 * in (api_server.py GET /system/topology -> this app's /api/system/topology), the call that loads
 * it, and the pure work of turning it into a 3D graph - three tiers round Jarvis, every node
 * placed and pinned before the scene draws it. No Three.js here: the scene module imports this,
 * not the other way round.
 *
 *   tier 0   Jarvis, the executive manager, at the centre
 *   tier 1   providers on the inner orbit - agents, MCP tool servers, Jarvis's own tool groups -
 *            in colour families (agents, tool servers, data & memory, core), each given an arc
 *            of the orbit as wide as its tools need
 *   tier 2   capabilities (tools) on the outer orbit, fanned out inside their provider's arc;
 *            a provider with many tools gets a second and third row further out
 *
 * `loadSystemTopology` answers `{ ok: true, data }` or `{ ok: false, status, message }` and never
 * throws; `message` is safe to show as it stands.
 */

export type ProviderStatus = "online" | "auth_required" | "offline";
export type ProviderKind = "orchestrator" | "agent" | "mcp_server" | "core";
export type ProviderCategory = "orchestrator" | "agent" | "mcp" | "data" | "core";
export type AccessMethod = "API" | "MCP" | "Internal";

export interface Capability {
  id: string;
  name: string;
  label: string;
  description: string;
  needs_approval: boolean;
  status: ProviderStatus;
  note: string;
}

export interface SystemRoute {
  role: string;
  vendor: string;
  model: string;
  state: string;
  error?: string;
}

export interface Workflow {
  label: string;
  prompt: string;
}

export interface SystemProvider {
  id: string;
  name: string;
  title: string;
  kind: ProviderKind;
  category: ProviderCategory;
  vendor: string;
  model: string | null;
  access: AccessMethod;
  status: ProviderStatus;
  status_note: string;
  diagnostics: string[];
  missing_scopes: string[];
  owner_only: boolean;
  description: string;
  routes: SystemRoute[];
  uses: string[];
  capabilities: Capability[];
  workflows: Workflow[];
}

export interface SystemTelemetry {
  status: "optimal" | "degraded";
  active_providers: number;
  total_providers: number;
  total_capabilities: number;
}

export interface SystemTopology {
  generated_at: string;
  viewer: { owner: boolean; hidden: number };
  orchestrator: SystemProvider;
  providers: SystemProvider[];
  telemetry: SystemTelemetry;
}

// ---- colour --------------------------------------------------------------------------------

export const OBSIDIAN = "#050508";
/** The core's two blues: the body, and the wireframe and rim. */
export const CORE_COLORS = { body: "#2E7BFF", wire: "#00D2FF" } as const;
export const CATEGORY_COLORS: Record<ProviderCategory, string> = {
  orchestrator: "#00D2FF",
  agent: "#2E90FA",
  mcp: "#8B5CF6",
  data: "#10D981",
  core: "#00D2FF",
};
export const WARNING = "#F59E0B";
export const SLATE = "#64748B";

export const CATEGORY_LABEL: Record<ProviderCategory, string> = {
  orchestrator: "Jarvis",
  agent: "Agents",
  mcp: "MCP servers",
  data: "Data & memory",
  core: "Jarvis core tools",
};

export const STATUS_LABEL: Record<ProviderStatus, string> = {
  online: "Active",
  auth_required: "Auth Required",
  offline: "Offline",
};

export const KIND_LABEL: Record<ProviderKind, string> = {
  orchestrator: "Orchestrator",
  agent: "Agent",
  mcp_server: "MCP server",
  core: "Built-in tools",
};

// ---- the graph ------------------------------------------------------------------------------

export type TopoKind = "core" | "provider" | "capability";

export interface TopoNode {
  id: string;
  kind: TopoKind;
  /** The provider it belongs to: itself for a provider, "jarvis" for the core. */
  providerId: string;
  label: string;
  category: ProviderCategory;
  status: ProviderStatus;
  radius: number;
  /** Where it sits round the core on the orbital plane (radians). */
  angle: number;
  x: number;
  y: number;
  z: number;
  fx: number;
  fy: number;
  fz: number;
}

export type TopoLinkKind = "provider" | "capability" | "uses";

export interface TopoLink {
  id: string;
  source: string | TopoNode;
  target: string | TopoNode;
  kind: TopoLinkKind;
  /** Both ends work: data flows (particles); otherwise the line is muted and dashed. */
  active: boolean;
  category: ProviderCategory;
}

export interface TopoGraph {
  nodes: TopoNode[];
  links: TopoLink[];
  /** The orbits' radii: the providers' ring, then each capability row. */
  rings: number[];
}

export const CORE_ID = "jarvis";
export const CORE_RADIUS = 26;
export const CAPABILITY_RADIUS = 4.4;

/** The id the force-graph library swapped for the node object, back as an id. */
export function endId(end: string | TopoNode): string {
  return typeof end === "string" ? end : end.id;
}

// Agents first, then data & memory, then the tool servers, then Jarvis's own tools: one colour
// family after another round the orbit.
const CATEGORY_ORDER: ProviderCategory[] = ["agent", "data", "mcp", "core"];

export function providerRadius(provider: SystemProvider): number {
  return 10.5 + Math.min(4.5, provider.capabilities.length * 0.45);
}

/** The three tiers, placed and pinned: the same topology always lands the same way. */
export function buildGraph(topology: SystemTopology): TopoGraph {
  const providers = [...topology.providers].sort(
    (a, b) => CATEGORY_ORDER.indexOf(a.category) - CATEGORY_ORDER.indexOf(b.category),
  );
  const nodes: TopoNode[] = [];
  const links: TopoLink[] = [];
  const pin = (node: Omit<TopoNode, "fx" | "fy" | "fz">): TopoNode => ({ ...node, fx: node.x, fy: node.y, fz: node.z });

  const core = topology.orchestrator;
  nodes.push(
    pin({
      id: CORE_ID,
      kind: "core",
      providerId: CORE_ID,
      label: "JARVIS",
      category: "orchestrator",
      status: core.status,
      radius: CORE_RADIUS,
      angle: 0,
      x: 0,
      y: 0,
      z: 0,
    }),
  );

  const count = providers.length;
  const inner = Math.min(320, Math.max(150, 96 + count * 9.5));
  const firstRow = inner + 118;
  const rowGap = 46;
  const spacing = 40; // arc length between neighbouring capabilities
  const weights = providers.map((p) => 1 + 0.55 * p.capabilities.length);
  const total = weights.reduce((sum, w) => sum + w, 0) || 1;
  let start = -Math.PI / 2;
  let rows = 1;

  providers.forEach((provider, index) => {
    const sector = (2 * Math.PI * weights[index]) / total;
    const angle = start + sector / 2;
    start += sector;
    // A gentle wave above and below the plane, so the orbit reads as 3D from any side.
    const lift = Math.sin(index * 2.399) * 24;
    nodes.push(
      pin({
        id: provider.id,
        kind: "provider",
        providerId: provider.id,
        label: provider.name,
        category: provider.category,
        status: provider.status,
        radius: providerRadius(provider),
        angle,
        x: Math.cos(angle) * inner,
        y: lift,
        z: Math.sin(angle) * inner,
      }),
    );
    links.push({
      id: `link:${CORE_ID}>${provider.id}`,
      source: CORE_ID,
      target: provider.id,
      kind: "provider",
      active: provider.status === "online" && core.status === "online",
      category: provider.category,
    });

    // Its tools fan out inside its arc, a row at a time.
    const caps = provider.capabilities;
    let placed = 0;
    let row = 0;
    while (placed < caps.length) {
      const radius = firstRow + row * rowGap;
      const usable = sector * 0.8;
      const fits = Math.max(1, Math.floor((usable * radius) / spacing) + 1);
      const here = Math.min(fits, caps.length - placed);
      const step = here > 1 ? Math.min(usable / (here - 1), spacing / radius) : 0;
      for (let k = 0; k < here; k++) {
        const capability = caps[placed + k];
        const a = angle + (k - (here - 1) / 2) * step;
        const y = lift + (k % 2 === 0 ? 1 : -1) * (8 + row * 6) * (here > 1 ? 1 : 0);
        nodes.push(
          pin({
            id: capability.id,
            kind: "capability",
            providerId: provider.id,
            label: capability.label,
            category: provider.category,
            status: capability.status,
            radius: CAPABILITY_RADIUS,
            angle: a,
            x: Math.cos(a) * radius,
            y,
            z: Math.sin(a) * radius,
          }),
        );
        links.push({
          id: `link:${provider.id}>${capability.id}`,
          source: provider.id,
          target: capability.id,
          kind: "capability",
          active: capability.status === "online" && provider.status === "online",
          category: provider.category,
        });
      }
      placed += here;
      row += 1;
    }
    rows = Math.max(rows, row);
  });

  // An agent's tool servers: the agent hands its work to them.
  const byId = new Map(providers.map((p) => [p.id, p]));
  for (const provider of providers) {
    for (const used of provider.uses) {
      const server = byId.get(used);
      if (!server) continue;
      links.push({
        id: `link:${provider.id}>${server.id}`,
        source: provider.id,
        target: server.id,
        kind: "uses",
        active: provider.status === "online" && server.status === "online",
        category: server.category,
      });
    }
  }

  const rings = [inner, ...Array.from({ length: rows }, (_, i) => firstRow + i * rowGap)];
  return { nodes, links, rings };
}

/** Every node's neighbours (for lighting up what an open node touches). */
export function neighbourMap(graph: TopoGraph): Map<string, Set<string>> {
  const map = new Map<string, Set<string>>();
  for (const link of graph.links) {
    const s = endId(link.source);
    const t = endId(link.target);
    if (!map.has(s)) map.set(s, new Set());
    if (!map.has(t)) map.set(t, new Set());
    map.get(s)!.add(t);
    map.get(t)!.add(s);
  }
  return map;
}

/** The shape of the graph: same ids, same graph (statuses can then change in place). */
export function shapeKey(topology: SystemTopology): string {
  return [
    topology.orchestrator.id,
    ...topology.providers.map((p) => `${p.id}[${p.capabilities.map((c) => c.id).join(",")}]<${p.uses.join(",")}>`),
  ].join("|");
}

// ---- finding things ---------------------------------------------------------------------------

export type TopologyFilter = "all" | "agents" | "mcp" | "capabilities";

export const FILTERS: { id: TopologyFilter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "agents", label: "Agents" },
  { id: "mcp", label: "MCP Servers" },
  { id: "capabilities", label: "Capabilities" },
];

/** Whether a provider (or a capability of it) is shown under a filter; Jarvis always is. */
export function shownUnder(filter: TopologyFilter, kind: ProviderKind): boolean {
  if (filter === "agents") return kind === "agent" || kind === "orchestrator";
  if (filter === "mcp") return kind === "mcp_server" || kind === "orchestrator";
  return true;
}

export interface SearchHit {
  id: string;
  label: string;
  providerId: string;
  kind: TopoKind;
}

/** Providers and capabilities whose words contain every word of the query. */
export function searchTopology(topology: SystemTopology, query: string): SearchHit[] {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return [];
  const has = (...parts: (string | null | undefined)[]) => {
    const text = parts.filter(Boolean).join(" ").toLowerCase();
    return words.every((word) => text.includes(word));
  };
  const hits: SearchHit[] = [];
  const all = [topology.orchestrator, ...topology.providers];
  for (const p of all) {
    if (has(p.name, p.title, p.vendor, p.model, p.description, KIND_LABEL[p.kind], CATEGORY_LABEL[p.category])) {
      hits.push({ id: p.kind === "orchestrator" ? CORE_ID : p.id, label: p.name, providerId: p.id, kind: p.kind === "orchestrator" ? "core" : "provider" });
    }
  }
  for (const p of topology.providers) {
    for (const c of p.capabilities) {
      if (has(c.label, c.name, c.description)) hits.push({ id: c.id, label: c.label, providerId: p.id, kind: "capability" });
    }
  }
  return hits;
}

/** The chat, opened with `prompt` waiting in the message field (components/jarvis/Composer.tsx). */
export function promptHref(prompt: string): string {
  return `/jarvis?prompt=${encodeURIComponent(prompt)}`;
}

/** "#RRGGBB" a share of the way to another colour. */
export function mixHex(from: string, to: string, amount: number): string {
  const a = parseInt(from.slice(1), 16);
  const b = parseInt(to.slice(1), 16);
  const channel = (shift: number) => Math.round(((a >> shift) & 255) * (1 - amount) + ((b >> shift) & 255) * amount);
  return `#${((1 << 24) | (channel(16) << 16) | (channel(8) << 8) | channel(0)).toString(16).slice(1)}`;
}

// ---- loading ---------------------------------------------------------------------------------

export type SystemResult<T> = { ok: true; data: T } | { ok: false; status: number; message: string };

export async function loadSystemTopology(signal?: AbortSignal): Promise<SystemResult<SystemTopology>> {
  let response: Response;
  try {
    response = await fetch("/api/system/topology", { cache: "no-store", signal });
  } catch {
    return { ok: false, status: 0, message: "Couldn't reach the server. Check your connection and try again." };
  }
  const body: unknown = await response.json().catch(() => null);
  if (response.ok && body && typeof body === "object" && "providers" in body) return { ok: true, data: body as SystemTopology };
  const error = body && typeof body === "object" && "error" in body ? (body as { error: unknown }).error : null;
  return {
    ok: false,
    status: response.status,
    message: typeof error === "string" && error ? error : "Something went wrong. Please try again.",
  };
}
