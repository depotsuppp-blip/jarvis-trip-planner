import type { ModelRoute, ToolsData } from "./chatApi";
import type { AgentStatus } from "./types";

/**
 * The sidebar's "Agents & tools", from what the brain reports (GET /api/chat/tools),
 * in three groups that mirror how Jarvis is built:
 *
 *   Executive manager   Jarvis itself: the model it is on - Claude, or the Gemini
 *                       standby and why (out of credit, rate limited).
 *   Specialist agents   Data & Documents, Research, Productivity (Python agents/):
 *                       "Ready" on their primary model, "On fallback" while a
 *                       fallback answers (and why the primary is not),
 *                       "Not connected" when the manager does their work itself.
 *   Tools               Trip Planner, Voice and the MCP tool servers, each saying
 *                       which agent uses it.
 *
 * Row ids: "brain", "trip-planner" and "voice" are the controller's own (it moves
 * the brain and voice rows during a turn); "agent:<name>" and "mcp:<name>" are
 * replaced by every refresh. Pure functions: the controller calls them, tests can too.
 */

export const TOOL_ROW_PREFIX = "mcp:";
export const AGENT_ROW_PREFIX = "agent:";

/** The bundled servers, in the order the sidebar lists them (see mcp_client.py). */
const KNOWN_SERVERS: { name: string; label: string; detail: string }[] = [
  { name: "web_search", label: "Web Search", detail: "Searches the web" },
  { name: "fetch", label: "Web Reader", detail: "Reads web pages and PDFs" },
  { name: "memory", label: "Memory", detail: "Remembers what you tell it" },
  { name: "data", label: "Data Analyst", detail: "Crunches CSV and JSON files" },
  { name: "local_fs", label: "Local Files", detail: "Reads project files and attachments" },
];

/** The specialist agents (Python agents/), in the order the sidebar lists them. */
const KNOWN_AGENTS: { name: string; label: string; detail: string }[] = [
  { name: "data", label: "Data & Documents", detail: "Reads files and crunches data" },
  { name: "research", label: "Research", detail: "Searches and reads the web" },
  { name: "productivity", label: "Productivity", detail: "Drafts schedules and to-dos" },
];

export const TOOL_OFFLINE_DETAIL = "Not running right now";
export const BRAIN_OFFLINE_DETAIL = "Can't reach Jarvis right now";
export const BRAIN_NO_MODEL_DETAIL = "No language model is set up";

const PROVIDERS: Record<string, { label: string; key: string }> = {
  anthropic: { label: "Claude", key: "ANTHROPIC_API_KEY" },
  gemini: { label: "Gemini", key: "GEMINI_API_KEY" },
  openai: { label: "OpenAI", key: "OPENAI_API_KEY" },
  groq: { label: "Groq", key: "GROQ_API_KEY" },
};

// Why a route is resting or failed (ProviderError kinds from the brain), as words.
const FAILURE_WORDS: Record<string, string> = {
  billing: "out of credit",
  quota: "out of quota",
  rate_limit: "rate limited",
  server: "busy",
  auth: "key rejected",
  permission: "no access to the model",
  not_found: "model not found",
  connection: "unreachable",
  bad_request: "request rejected",
};

function capitalize(word: string): string {
  return word ? word[0].toUpperCase() + word.slice(1) : word;
}

/** "my_notes" -> "My Notes" */
function titleCase(name: string): string {
  return name.split("_").filter(Boolean).map(capitalize).join(" ");
}

/**
 * A model id as a person reads it: "claude-sonnet-5" -> "Claude Sonnet 5",
 * "claude-haiku-4-5-20251001" -> "Claude Haiku 4.5", "gemini-flash-lite-latest" ->
 * "Gemini Flash-Lite", "gemini-3.5-flash" -> "Gemini 3.5 Flash", "gpt-4o-mini" ->
 * "GPT-4o mini", "openai/gpt-oss-120b" -> "gpt-oss-120b". Anything else as it is.
 */
export function modelLabel(model: string): string {
  const id = model.trim();
  const claude = /^claude-(sonnet|opus|haiku)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?$/.exec(id);
  if (claude) return `Claude ${capitalize(claude[1])} ${claude[2]}${claude[3] ? `.${claude[3]}` : ""}`;
  if (id.startsWith("gemini-")) {
    const words = id
      .slice("gemini-".length)
      .replace(/-latest$/, "")
      .replace(/flash-lite/, "flash~lite")
      .split("-")
      .filter(Boolean)
      .map((word) => (/^\d/.test(word) ? word : word.split("~").map(capitalize).join("-")));
    return ["Gemini", ...words].join(" ");
  }
  if (/^gpt-\d/.test(id)) return `GPT-${id.slice(4).replace(/-mini$/, " mini")}`;
  return id.includes("/") ? id.slice(id.lastIndexOf("/") + 1) : id;
}

/** A route as a person reads it: the model, and where it runs when that is not obvious from the name. */
export function routeLabel(route: { provider: string; model: string }): string {
  const label = modelLabel(route.model);
  return route.provider === "groq" ? `${label} on Groq` : label;
}

/** Why a route is not answering: "out of quota", "needs GROQ_API_KEY", ... */
export function routeTrouble(route: Pick<ModelRoute, "provider" | "state" | "lastError">): string {
  if (route.state === "not configured") return `needs ${PROVIDERS[route.provider]?.key ?? "an API key"}`;
  return (route.lastError && FAILURE_WORDS[route.lastError]) || (route.state === "resting" ? "resting" : "failed");
}

function rank(list: { name: string }[], name: string): number {
  const index = list.findIndex((item) => item.name === name);
  return index === -1 ? list.length : index;
}

/** The executive manager's row after a status check. A turn in progress keeps its "Working". */
export function brainFromStatus(brain: AgentStatus, status: ToolsData): AgentStatus {
  const manager = status.reachable && status.ready ? status.manager : null;
  let model: string | undefined;
  let standby = false;
  if (manager) {
    const active = routeLabel(manager);
    standby = manager.onStandby;
    model = standby
      ? `${active} · ${PROVIDERS[manager.primary.provider]?.label ?? routeLabel(manager.primary)} ${routeTrouble(manager.primary)}`
      : active;
  }
  if (brain.state === "working") return { ...brain, model };
  if (status.reachable && status.ready) return { ...brain, state: standby ? "degraded" : "ready", detail: undefined, model };
  return {
    ...brain,
    state: "offline",
    detail: status.reachable ? BRAIN_NO_MODEL_DETAIL : BRAIN_OFFLINE_DETAIL,
    model: undefined,
  };
}

/** One row per specialist agent. */
export function agentRows(agents: NonNullable<ToolsData["agents"]>): AgentStatus[] {
  return [...agents]
    .sort((a, b) => rank(KNOWN_AGENTS, a.name) - rank(KNOWN_AGENTS, b.name) || a.name.localeCompare(b.name))
    .map((agent): AgentStatus => {
      const known = KNOWN_AGENTS.find((candidate) => candidate.name === agent.name);
      const base = {
        id: `${AGENT_ROW_PREFIX}${agent.name}`,
        name: known?.label ?? agent.title.replace(/^Head of /, ""),
        kind: "agent" as const,
        detail: known?.detail,
        routes: agent.routes.map(routeLabel).join(" → "),
      };
      if (!agent.active) {
        const configured = agent.routes.some((route) => route.state !== "not configured");
        const keys = [...new Set(agent.routes.map((route) => PROVIDERS[route.provider]?.key).filter(Boolean))];
        return {
          ...base,
          state: "unavailable",
          model: configured ? "Its tools aren't running; Jarvis does this itself" : `Needs ${keys.join(" or ")}`,
        };
      }
      const current = agent.routes.find((route) => route.state === "ready");
      const primary = agent.routes[0];
      if (!current) {
        return { ...base, state: "offline", model: `All its models are resting · ${routeLabel(primary)} ${routeTrouble(primary)}` };
      }
      if (current === primary) return { ...base, state: "ready", model: routeLabel(current) };
      // Name the first route that is not answering and why: usually the primary.
      const down = agent.routes.find((route) => route.state !== "ready") ?? primary;
      return { ...base, state: "degraded", model: `${routeLabel(current)} · ${routeLabel(down)} ${routeTrouble(down)}` };
    });
}

/** One row per tool server, saying which agent uses it. */
export function toolRows(servers: ToolsData["servers"], agents: ToolsData["agents"] = []): AgentStatus[] {
  return [...servers]
    .sort((a, b) => rank(KNOWN_SERVERS, a.name) - rank(KNOWN_SERVERS, b.name) || a.name.localeCompare(b.name))
    .map((server): AgentStatus => {
      const known = KNOWN_SERVERS.find((candidate) => candidate.name === server.name);
      const count = `${server.tools} tool${server.tools === 1 ? "" : "s"}`;
      const owner = server.agent ? agents?.find((agent) => agent.name === server.agent) : undefined;
      const ownerLabel = owner ? (KNOWN_AGENTS.find((a) => a.name === owner.name)?.label ?? owner.title) : null;
      return {
        id: `${TOOL_ROW_PREFIX}${server.name}`,
        name: known?.label ?? (titleCase(server.name) || server.name),
        kind: "tool",
        state: server.connected ? "ready" : "offline",
        detail: server.connected ? (known?.detail ?? count) : TOOL_OFFLINE_DETAIL,
        // Only from a brain that reports who holds what (one with the agent team).
        ...(server.agent !== undefined ? { model: `Used by ${ownerLabel ?? "Jarvis"}` } : {}),
      };
    });
}

/** The whole list after a status check: the controller's own rows in place, agent and tool rows replaced. */
export function withStatus(agents: AgentStatus[], status: ToolsData): AgentStatus[] {
  const own = agents
    .filter((agent) => !agent.id.startsWith(TOOL_ROW_PREFIX) && !agent.id.startsWith(AGENT_ROW_PREFIX))
    .map((agent) => (agent.id === "brain" ? brainFromStatus(agent, status) : agent));
  return [...own, ...agentRows(status.agents ?? []), ...toolRows(status.servers, status.agents ?? [])];
}
