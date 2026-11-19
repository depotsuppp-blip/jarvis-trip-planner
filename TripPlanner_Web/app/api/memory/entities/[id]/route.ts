import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import { getSessionUserId } from "@/lib/auth";
import { MEMORY_LIMITS } from "@/lib/jarvis/memory";
import {
  deleteMemoryEntity,
  MEMORY_SIGNED_OUT,
  memoryFailure,
  memoryWriteLimit,
  plausibleId,
  updateMemoryEntity,
} from "@/lib/jarvisMemory";

// params is a Promise in this Next.js version, not a plain object.
type RouteParams = { params: Promise<{ id: string }> };

const ChangesSchema = z
  .strictObject({
    name: z.string().trim().min(1).max(MEMORY_LIMITS.name).optional(),
    type: z.string().trim().max(MEMORY_LIMITS.type).optional(),
    description: z.string().trim().max(MEMORY_LIMITS.description).optional(),
  })
  .refine((changes) => Object.values(changes).some((value) => value !== undefined), {
    message: "Nothing to change.",
  });

const NOT_FOUND = { error: "That entry is no longer in Jarvis's memory.", code: "not_found" };

/**
 * PATCH /api/memory/entities/<id> { name?, type?, description? } - corrects one entry of the
 * signed-in person's Jarvis memory (the Memory page's inspector). When its words change, the
 * brain embeds it again, so Jarvis finds it by what it now says; if that fails nothing is saved.
 *
 * Responses: 200 { node: { id, name, type, description }, reembedded }, 400 a bad body
 * (or { code: "me_locked" | "reserved" | "refused" }), 401 signed out, 404 not theirs or gone,
 * 409 { code: "name_taken" }, 429 too many changes, 503 { code } the brain or its memory is not there.
 */
export async function PATCH(request: NextRequest, { params }: RouteParams) {
  const userId = await getSessionUserId();
  if (!userId) return NextResponse.json({ error: MEMORY_SIGNED_OUT }, { status: 401 });

  const { id } = await params;
  if (!plausibleId(id)) return NextResponse.json(NOT_FOUND, { status: 404 });
  const parsed = ChangesSchema.safeParse(await request.json().catch(() => null));
  if (!parsed.success) {
    return NextResponse.json(
      {
        error: `Send a name (1-${MEMORY_LIMITS.name} characters), a type (up to ${MEMORY_LIMITS.type}) or a description (up to ${MEMORY_LIMITS.description}).`,
        code: "invalid",
      },
      { status: 400 },
    );
  }
  const limited = memoryWriteLimit(userId);
  if (limited) return limited;

  try {
    return NextResponse.json(await updateMemoryEntity(userId, id, parsed.data));
  } catch (error) {
    return memoryFailure(error, "PATCH /api/memory/entities");
  }
}

/**
 * DELETE /api/memory/entities/<id> - deletes one entry of the signed-in person's Jarvis memory,
 * with every relation it is in. Jarvis no longer knows it.
 *
 * Responses: 200 { deleted, links: [ids of the relations deleted with it] }, 401 signed out,
 * 404 not theirs or gone, 429 too many changes, 503 { code }.
 */
export async function DELETE(_request: NextRequest, { params }: RouteParams) {
  const userId = await getSessionUserId();
  if (!userId) return NextResponse.json({ error: MEMORY_SIGNED_OUT }, { status: 401 });

  const { id } = await params;
  if (!plausibleId(id)) return NextResponse.json(NOT_FOUND, { status: 404 });
  const limited = memoryWriteLimit(userId);
  if (limited) return limited;

  try {
    return NextResponse.json(await deleteMemoryEntity(userId, id));
  } catch (error) {
    return memoryFailure(error, "DELETE /api/memory/entities");
  }
}
