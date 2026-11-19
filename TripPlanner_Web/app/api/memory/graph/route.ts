import { NextResponse } from "next/server";
import { getSessionUserId } from "@/lib/auth";
import { fetchMemoryGraph, MEMORY_SIGNED_OUT, memoryFailure } from "@/lib/jarvisMemory";

/**
 * GET /api/memory/graph - the signed-in person's Jarvis memory as a graph, for the Memory
 * page (/jarvis/memory): every entry (person, place, project...) and every relation between
 * them, from the brain's GET /memory/graph (lib/jarvisMemory.ts). Only ever the session's
 * own memory: the brain is told who is asking, never the browser.
 *
 * Responses: 200 { nodes: [{ id, name, type, description }],
 *                  links: [{ id, source, target, relation_type, weight }] }
 *            (empty lists when nothing has been remembered yet),
 *            401 signed out, 503 { error, code } the brain or its memory database is not there.
 */
export async function GET() {
  const userId = await getSessionUserId();
  if (!userId) return NextResponse.json({ error: MEMORY_SIGNED_OUT }, { status: 401 });

  try {
    const graph = await fetchMemoryGraph(userId);
    return NextResponse.json(graph, { headers: { "Cache-Control": "private, no-store" } });
  } catch (error) {
    return memoryFailure(error, "GET /api/memory/graph");
  }
}
