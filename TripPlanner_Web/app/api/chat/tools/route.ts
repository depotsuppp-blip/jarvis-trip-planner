import { NextResponse } from "next/server";
import { getSessionUserId } from "@/lib/auth";
import {
  getJarvisStatus,
  JarvisBrainError,
  type BrainAgent,
  type BrainManager,
  type BrainToolServer,
} from "@/lib/jarvisBrain";

// The client's copy of this shape is ToolsData in lib/jarvis/chatApi.ts.
interface ToolsResponse {
  /** The brain answered at all. */
  reachable: boolean;
  /** It has a language model to answer with. */
  ready: boolean;
  /** The executive manager's own model (Claude, or the Gemini standby and why). Null when unknown. */
  manager: BrainManager | null;
  /** The specialist agents it delegates to, each with its model routes in order. */
  agents: BrainAgent[];
  /**
   * Its tool servers (web search, memory, ...), connected or not, and - when the brain
   * has a team - which working agent holds each one's tools (null: the manager holds
   * them itself). Empty when the brain is unreachable.
   */
  servers: (BrainToolServer & { agent?: string | null })[];
}

/**
 * GET /api/chat/tools - who Jarvis is made of right now, for the hub's sidebar
 * ("Agents & tools"): the executive manager and the model it is on, the
 * specialist agents (Data & Documents, Research, Productivity) and how each is
 * running, and the tool servers with the agent that uses each. All from the
 * brain's GET /health (lib/jarvisBrain.ts), narrowed and checked there - the
 * brain's own error text stays in its logs.
 *
 * A brain that is offline or still starting is not an error here - it is the
 * status to show - so it answers 200 with `reachable: false`.
 *
 * Responses: 200 { reachable, ready, manager, agents, servers }, 401 signed out.
 */
export async function GET() {
  const userId = await getSessionUserId();
  if (!userId) {
    return NextResponse.json({ error: "Sign in to see Jarvis's tools." }, { status: 401 });
  }

  let body: ToolsResponse;
  try {
    const status = await getJarvisStatus();
    // A server's tools belong to the agent that holds them only while that agent works;
    // otherwise the manager kept them (agents/__init__.py).
    const owner = new Map<string, string>();
    for (const agent of status.agents) {
      if (agent.active) for (const server of agent.servers) owner.set(server, agent.name);
    }
    body = {
      reachable: true,
      ready: status.ready,
      manager: status.manager,
      agents: status.agents,
      // Without a team (switched off, or an older brain) there is no one else to name: no owner at all.
      servers: status.agents.length
        ? status.servers.map((server) => ({ ...server, agent: owner.get(server.name) ?? null }))
        : status.servers,
    };
  } catch (error) {
    if (!(error instanceof JarvisBrainError)) console.error("GET /api/chat/tools failed:", error);
    body = { reachable: false, ready: false, manager: null, agents: [], servers: [] };
  }
  return NextResponse.json(body, { headers: { "Cache-Control": "private, no-store" } });
}
