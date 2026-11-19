import { NextResponse } from "next/server";
import { getSessionUserId } from "@/lib/auth";
import { fetchSystemTopology, JarvisSystemError, SYSTEM_SIGNED_OUT } from "@/lib/jarvisSystem";

/**
 * GET /api/system/topology - what Jarvis can do right now, for the System Topology page
 * (/jarvis/system): Jarvis itself, its agents, its tool servers and its own tools, each with its
 * live status (online, auth_required, offline), its tools and a few starter prompts - from the
 * brain's GET /system/topology (lib/jarvisSystem.ts). The brain is told who is asking (the
 * session's user, never the browser): the owner sees the owner-only parts, nobody else does.
 *
 * Responses: 200 the topology (lib/jarvis/system.ts SystemTopology),
 *            401 { error } signed out,
 *            503 { error, code: "offline" | "unavailable" } the brain is not there or failed.
 * The brain's own error text never reaches the browser: a fixed sentence per code instead.
 */
export async function GET() {
  const userId = await getSessionUserId();
  if (!userId) return NextResponse.json({ error: SYSTEM_SIGNED_OUT }, { status: 401 });

  try {
    const topology = await fetchSystemTopology(userId);
    return NextResponse.json(topology, { headers: { "Cache-Control": "private, no-store" } });
  } catch (error) {
    if (error instanceof JarvisSystemError) {
      return NextResponse.json({ error: error.message, code: error.code }, { status: 503 });
    }
    console.error("GET /api/system/topology failed:", error instanceof Error ? error.name : "unknown");
    return NextResponse.json({ error: "Something went wrong. Please try again.", code: "server" }, { status: 500 });
  }
}
