import { NextResponse } from "next/server";
import { getSessionUserId } from "@/lib/auth";
import { getJarvisStatus, JarvisBrainError, type BrainToolServer } from "@/lib/jarvisBrain";

// The client's copy of this shape is ToolsData in lib/jarvis/chatApi.ts.
interface ToolsResponse {
  /** The brain answered at all. */
  reachable: boolean;
  /** It has a language model to answer with. */
  ready: boolean;
  /** Its tool servers (web search, memory, ...), connected or not. Empty when it is unreachable. */
  servers: BrainToolServer[];
}

/**
 * GET /api/chat/tools - which agents and tools Jarvis has right now, for the
 * hub's sidebar ("Agents & tools"): the brain's tool servers and whether each
 * is connected, from the brain's GET /health (lib/jarvisBrain.ts).
 *
 * A brain that is offline or still starting is not an error here - it is the
 * status to show - so it answers 200 with `reachable: false`. The brain's own
 * error text stays in its logs.
 *
 * Responses: 200 { reachable, ready, servers }, 401 signed out.
 */
export async function GET() {
  const userId = await getSessionUserId();
  if (!userId) {
    return NextResponse.json({ error: "Sign in to see Jarvis's tools." }, { status: 401 });
  }

  let body: ToolsResponse;
  try {
    const status = await getJarvisStatus();
    body = { reachable: true, ready: status.ready, servers: status.servers };
  } catch (error) {
    if (!(error instanceof JarvisBrainError)) console.error("GET /api/chat/tools failed:", error);
    body = { reachable: false, ready: false, servers: [] };
  }
  return NextResponse.json(body, { headers: { "Cache-Control": "private, no-store" } });
}
