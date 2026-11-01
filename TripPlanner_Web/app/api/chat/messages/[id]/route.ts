import { NextRequest, NextResponse } from "next/server";
import { getSessionUserId } from "@/lib/auth";
import { parseStoredUserMessage } from "@/lib/jarvis/attachments";
import { prisma } from "@/lib/prisma";
import { discardUpload } from "@/lib/uploads";

// params is a Promise in this Next.js version, not a plain object.
type RouteParams = { params: Promise<{ id: string }> };

/**
 * DELETE /api/chat/messages/<id> - deletes one message of the signed-in person's own
 * Jarvis sessions (the hub's Delete on a message bubble).
 *
 * It is gone for good: from the session as the page loads it, and from the history
 * the brain is shown on the next turn (lib/chatTurns.ts loadHistory reads the saved
 * messages). A file the person sent with it is taken off the disk too (lib/uploads.ts):
 * nothing points at it any more. The session itself stays, with its title, even when
 * its last message goes.
 *
 * Someone else's message is a 404, exactly like one that does not exist.
 *
 * Responses: 200 { deleted: <id> }, 401 signed out, 404 not found, 500 database failed.
 */
export async function DELETE(_request: NextRequest, { params }: RouteParams) {
  const userId = await getSessionUserId();
  if (!userId) {
    return NextResponse.json({ error: "Sign in to manage your chat." }, { status: 401 });
  }

  const { id } = await params;
  const notFound = NextResponse.json({ error: "Message not found." }, { status: 404 });
  if (!id || id.length > 128) return notFound;

  const mine = { id, conversation: { userId } };
  let deleted: { id: string; role: string; content: string } | null;
  try {
    deleted = await prisma.message.findFirst({ where: mine, select: { id: true, role: true, content: true } });
    if (!deleted) return notFound;
    // Scoped again in the delete itself: a message deleted meanwhile (another tab) is a 404, not an error.
    const { count } = await prisma.message.deleteMany({ where: mine });
    if (count === 0) return notFound;
  } catch (error) {
    console.error("DELETE /api/chat/messages failed:", error);
    return NextResponse.json({ error: "Couldn't delete that message. Please try again." }, { status: 500 });
  }

  if (deleted.role === "user") await discardFiles(deleted.content);
  return NextResponse.json({ deleted: deleted.id });
}

/**
 * Takes the files a deleted message carried off the disk - each only when no other message
 * still names it. Typed words can look exactly like the stored form of a message with a
 * file (lib/jarvis/attachments.ts), so a path in a message proves nothing about whose file
 * it is; the message that really sent it still names it, and keeps it. Best effort: the
 * message is already deleted, and a file left behind is only disk space.
 */
async function discardFiles(content: string): Promise<void> {
  for (const attachment of parseStoredUserMessage(content).attachments) {
    try {
      const stillNamed = await prisma.message.count({ where: { content: { contains: attachment.path } } });
      if (stillNamed === 0) await discardUpload(attachment);
    } catch (error) {
      console.error("DELETE /api/chat/messages: could not tidy up an attached file:", error);
    }
  }
}
