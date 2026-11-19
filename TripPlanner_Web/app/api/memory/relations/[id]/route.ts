import { NextRequest, NextResponse } from "next/server";
import { getSessionUserId } from "@/lib/auth";
import {
  deleteMemoryRelation,
  MEMORY_SIGNED_OUT,
  memoryFailure,
  memoryWriteLimit,
  plausibleId,
} from "@/lib/jarvisMemory";

// params is a Promise in this Next.js version, not a plain object.
type RouteParams = { params: Promise<{ id: string }> };

/**
 * DELETE /api/memory/relations/<id> - removes one relation ("Nok lives in Chiang Mai") from
 * the signed-in person's Jarvis memory. Both entries stay; delete an entry on its own.
 *
 * Responses: 200 { deleted }, 401 signed out, 404 not theirs or gone, 429 too many changes,
 * 503 { code } the brain or its memory is not there.
 */
export async function DELETE(_request: NextRequest, { params }: RouteParams) {
  const userId = await getSessionUserId();
  if (!userId) return NextResponse.json({ error: MEMORY_SIGNED_OUT }, { status: 401 });

  const { id } = await params;
  if (!plausibleId(id)) {
    return NextResponse.json({ error: "That relation is no longer in Jarvis's memory.", code: "not_found" }, { status: 404 });
  }
  const limited = memoryWriteLimit(userId);
  if (limited) return limited;

  try {
    return NextResponse.json(await deleteMemoryRelation(userId, id));
  } catch (error) {
    return memoryFailure(error, "DELETE /api/memory/relations");
  }
}
