import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import { getSessionUserId } from "@/lib/auth";
import { RELATION_STATUSES } from "@/lib/jarvis/memory";
import {
  deleteMemoryRelation,
  MEMORY_SIGNED_OUT,
  memoryFailure,
  memoryWriteLimit,
  plausibleId,
  updateMemoryRelation,
} from "@/lib/jarvisMemory";

// params is a Promise in this Next.js version, not a plain object.
type RouteParams = { params: Promise<{ id: string }> };

const StatusSchema = z.strictObject({ status: z.enum(RELATION_STATUSES) });

const GONE = { error: "That relation is no longer in Jarvis's memory.", code: "not_found" };

/**
 * PATCH /api/memory/relations/<id> { status } - marks one relation of the signed-in person's
 * Jarvis memory active (holds now), completed (done) or archived (no longer true), from the
 * Memory page's inspector. The fact stays in memory either way; its date moves to now, and
 * Jarvis's recall shows it with that status.
 *
 * Responses: 200 { link: { id, source, target, relation_type, weight, status, updated_at } },
 * 400 a bad body, 401 signed out, 404 not theirs or gone, 429 too many changes, 503 { code }.
 */
export async function PATCH(request: NextRequest, { params }: RouteParams) {
  const userId = await getSessionUserId();
  if (!userId) return NextResponse.json({ error: MEMORY_SIGNED_OUT }, { status: 401 });

  const { id } = await params;
  if (!plausibleId(id)) return NextResponse.json(GONE, { status: 404 });
  const parsed = StatusSchema.safeParse(await request.json().catch(() => null));
  if (!parsed.success) {
    return NextResponse.json(
      { error: `Send a status: ${RELATION_STATUSES.join(", ")}.`, code: "invalid" },
      { status: 400 },
    );
  }
  const limited = memoryWriteLimit(userId);
  if (limited) return limited;

  try {
    return NextResponse.json(await updateMemoryRelation(userId, id, parsed.data.status));
  } catch (error) {
    return memoryFailure(error, "PATCH /api/memory/relations");
  }
}

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
  if (!plausibleId(id)) return NextResponse.json(GONE, { status: 404 });
  const limited = memoryWriteLimit(userId);
  if (limited) return limited;

  try {
    return NextResponse.json(await deleteMemoryRelation(userId, id));
  } catch (error) {
    return memoryFailure(error, "DELETE /api/memory/relations");
  }
}
