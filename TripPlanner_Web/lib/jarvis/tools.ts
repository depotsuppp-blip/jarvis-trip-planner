import type { ToolsData } from "./chatApi";
import type { AgentStatus } from "./types";

/**
 * The sidebar's tool rows, from what the brain reports (GET /api/chat/tools).
 *
 * Each of the brain's MCP tool servers is one row: "Ready" (Go Green) while it
 * is connected, "Offline" when it is configured but not running. The servers
 * that ship with Jarvis get a name and a line a person understands; one the
 * owner added shows under its own config name with its tool count.
 *
 * The rows' ids start with "mcp:", so a refresh replaces exactly them and never
 * touches the rows the controller moves itself (Jarvis brain, Voice) or the
 * Trip Planner row. Pure functions: the controller calls them, tests can too.
 */

export const TOOL_ROW_PREFIX = "mcp:";

/** The bundled servers, in the order the sidebar lists them (see mcp_client.py). */
const KNOWN_SERVERS: { name: string; label: string; detail: string }[] = [
  { name: "web_search", label: "Web Search", detail: "Searches the web" },
  { name: "fetch", label: "Web Reader", detail: "Reads web pages and PDFs" },
  { name: "memory", label: "Memory", detail: "Remembers what you tell it" },
  { name: "data", label: "Data Analyst", detail: "Crunches CSV and JSON files" },
  { name: "local_fs", label: "Local Files", detail: "Reads project files and attachments" },
];

export const TOOL_OFFLINE_DETAIL = "Not running right now";
export const BRAIN_OFFLINE_DETAIL = "Can't reach Jarvis right now";
export const BRAIN_NO_MODEL_DETAIL = "No language model is set up";

function rank(name: string): number {
  const index = KNOWN_SERVERS.findIndex((server) => server.name === name);
  return index === -1 ? KNOWN_SERVERS.length : index;
}

/** "my_notes" -> "My Notes" */
function titleCase(name: string): string {
  return name
    .split("_")
    .filter(Boolean)
    .map((word) => word[0].toUpperCase() + word.slice(1))
    .join(" ");
}

export function toolRows(servers: ToolsData["servers"]): AgentStatus[] {
  return [...servers]
    .sort((a, b) => rank(a.name) - rank(b.name) || a.name.localeCompare(b.name))
    .map((server): AgentStatus => {
      const known = KNOWN_SERVERS.find((candidate) => candidate.name === server.name);
      const count = `${server.tools} tool${server.tools === 1 ? "" : "s"}`;
      return {
        id: `${TOOL_ROW_PREFIX}${server.name}`,
        name: known?.label ?? (titleCase(server.name) || server.name),
        kind: "tool",
        state: server.connected ? "ready" : "offline",
        detail: server.connected ? (known?.detail ?? count) : TOOL_OFFLINE_DETAIL,
      };
    });
}

/**
 * The brain row after a status check. A turn in progress ("Working") is left
 * alone - the turn will settle it - and otherwise the check is the freshest
 * news: reachable with a model is "Ready", anything else "Offline" with why.
 */
export function brainFromStatus(brain: AgentStatus, status: ToolsData): AgentStatus {
  if (brain.state === "working") return brain;
  if (status.reachable && status.ready) return { ...brain, state: "ready", detail: undefined };
  return {
    ...brain,
    state: "offline",
    detail: status.reachable ? BRAIN_NO_MODEL_DETAIL : BRAIN_OFFLINE_DETAIL,
  };
}

/** The whole list after a status check: the controller's own rows in place, the tool rows replaced. */
export function withStatus(agents: AgentStatus[], status: ToolsData): AgentStatus[] {
  const own = agents
    .filter((agent) => !agent.id.startsWith(TOOL_ROW_PREFIX))
    .map((agent) => (agent.id === "brain" ? brainFromStatus(agent, status) : agent));
  return [...own, ...toolRows(status.servers)];
}
