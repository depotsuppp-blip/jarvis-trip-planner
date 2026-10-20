import { NextResponse } from "next/server";
import { getSessionUserId } from "@/lib/auth";
import { prisma } from "@/lib/prisma";

// The sidebar lists this many, newest first; older ones are still in the database.
const SESSION_LIMIT = 50;

/**
 * GET /api/chat/sessions - the signed-in user's chat sessions for the hub's
 * sidebar, most recently used first, as
 * { sessions: [{ id, title, updatedAt }] }. `title` is the start of the
 * session's first message (set when it was created) and can be null. Message
 * bodies are not included: open one with GET /api/chat?conversationId=<id>.
 *
 * Scoped to the session's user - there is no way to list anyone else's.
 *
 * Responses: 200 { sessions }, 401 signed out, 500 the read failed.
 */
export async function GET() {
  const userId = await getSessionUserId();
  if (!userId) {
    return NextResponse.json({ error: "Sign in to see your sessions." }, { status: 401 });
  }

  try {
    const sessions = await prisma.conversation.findMany({
      where: { userId },
      orderBy: { updatedAt: "desc" },
      take: SESSION_LIMIT,
      select: { id: true, title: true, updatedAt: true },
    });
    return NextResponse.json({ sessions }, { headers: { "Cache-Control": "private, no-store" } });
  } catch (error) {
    console.error("GET /api/chat/sessions failed:", error);
    return NextResponse.json({ error: "Couldn't load your sessions." }, { status: 500 });
  }
}
