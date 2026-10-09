import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import { getSessionUserId } from "@/lib/auth";
import { askJarvis, JarvisBrainError, type ChatTurn } from "@/lib/jarvisBrain";
import { prisma } from "@/lib/prisma";
import { checkRateLimit } from "@/lib/rateLimit";

const CHAT_RATE_LIMIT = 30;
const CHAT_RATE_WINDOW_MS = 60_000;
const MAX_MESSAGE_LENGTH = 4000;
// How many earlier turns the brain is shown, and how many the page loads.
const HISTORY_TURNS = 20;
const TITLE_LENGTH = 60;

const ChatBodySchema = z.object({
  message: z.string().trim().min(1).max(MAX_MESSAGE_LENGTH),
  // Omit to continue the user's latest conversation (or start the first one).
  conversationId: z.string().trim().min(1).max(128).optional(),
});

const messageFields = { id: true, role: true, content: true, createdAt: true } as const;

function toRole(role: string): "user" | "jarvis" {
  return role === "user" ? "user" : "jarvis";
}

/**
 * POST /api/chat { message, conversationId? } - the Jarvis hub's text turn.
 * Saves the signed-in user's message, asks the brain (lib/jarvisBrain.ts) for
 * a reply, saves that too, and returns both. The user is always the
 * session's, and a conversationId that isn't theirs is treated as not found.
 *
 * The user's message is written before the brain is called, so a brain
 * failure never loses it: that case answers 502 and the message stays in
 * the history.
 *
 * Responses: 200 { conversationId, userMessage, reply }, 400 bad body, 401
 * signed out, 404 unknown conversation, 429 rate-limited, 502 brain failed,
 * 500 database failed.
 */
export async function POST(request: NextRequest) {
  const userId = await getSessionUserId();
  if (!userId) {
    return NextResponse.json({ error: "Sign in to chat with Jarvis." }, { status: 401 });
  }

  const parsed = ChatBodySchema.safeParse(await request.json().catch(() => null));
  if (!parsed.success) {
    return NextResponse.json(
      { error: `message is required (1-${MAX_MESSAGE_LENGTH} characters).` },
      { status: 400 }
    );
  }
  const { message, conversationId } = parsed.data;

  const rateLimit = checkRateLimit(`chat:${userId}`, CHAT_RATE_LIMIT, CHAT_RATE_WINDOW_MS);
  if (!rateLimit.allowed) {
    return NextResponse.json(
      { error: "Too many messages - please wait a moment." },
      { status: 429, headers: { "Retry-After": String(rateLimit.retryAfterSeconds) } }
    );
  }

  let conversation: { id: string };
  let userMessage;
  let history: ChatTurn[];
  try {
    const existing = conversationId
      ? await prisma.conversation.findFirst({ where: { id: conversationId, userId }, select: { id: true } })
      : await prisma.conversation.findFirst({ where: { userId }, orderBy: { updatedAt: "desc" }, select: { id: true } });

    if (conversationId && !existing) {
      return NextResponse.json({ error: "Conversation not found." }, { status: 404 });
    }

    conversation =
      existing ??
      (await prisma.conversation.create({
        data: { userId, title: message.slice(0, TITLE_LENGTH) },
        select: { id: true },
      }));

    // Read the earlier turns before this one is added, newest first then flipped.
    const earlier = await prisma.message.findMany({
      where: { conversationId: conversation.id },
      orderBy: { createdAt: "desc" },
      take: HISTORY_TURNS,
      select: { role: true, content: true },
    });
    history = earlier.reverse().map((m) => ({ role: toRole(m.role), content: m.content }));

    userMessage = await prisma.message.create({
      data: { conversationId: conversation.id, role: "user", content: message },
      select: messageFields,
    });
  } catch (error) {
    console.error("POST /api/chat failed saving the user's message:", error);
    return NextResponse.json({ error: "Couldn't save your message. Please try again." }, { status: 500 });
  }

  let replyText: string;
  try {
    replyText = (await askJarvis(message, history)).trim();
    if (!replyText) throw new Error("The brain returned an empty reply.");
  } catch (error) {
    console.error(`POST /api/chat: the brain failed for conversation ${conversation.id}:`, error);
    return NextResponse.json(
      { error: error instanceof JarvisBrainError ? error.message : "Jarvis couldn't answer just now.", conversationId: conversation.id, userMessage },
      { status: 502 }
    );
  }

  try {
    const [reply] = await prisma.$transaction([
      prisma.message.create({
        data: { conversationId: conversation.id, role: "jarvis", content: replyText },
        select: messageFields,
      }),
      // Bumps updatedAt so "latest conversation" follows the last activity.
      prisma.conversation.update({ where: { id: conversation.id }, data: { updatedAt: new Date() }, select: { id: true } }),
    ]);
    return NextResponse.json({ conversationId: conversation.id, userMessage, reply });
  } catch (error) {
    console.error("POST /api/chat failed saving the reply:", error);
    return NextResponse.json({ error: "Couldn't save Jarvis's reply. Please try again." }, { status: 500 });
  }
}

/**
 * GET /api/chat - the signed-in user's latest conversation, oldest message
 * first, as { conversationId, messages }. conversationId is null (and
 * messages empty) before the first message. Scoped to the session's user.
 */
export async function GET() {
  const userId = await getSessionUserId();
  if (!userId) {
    return NextResponse.json({ error: "Sign in to see your chat." }, { status: 401 });
  }

  try {
    const conversation = await prisma.conversation.findFirst({
      where: { userId },
      orderBy: { updatedAt: "desc" },
      select: { id: true },
    });
    if (!conversation) return NextResponse.json({ conversationId: null, messages: [] });

    const recent = await prisma.message.findMany({
      where: { conversationId: conversation.id },
      orderBy: { createdAt: "desc" },
      take: 100,
      select: messageFields,
    });
    return NextResponse.json({ conversationId: conversation.id, messages: recent.reverse() });
  } catch (error) {
    console.error("GET /api/chat failed:", error);
    return NextResponse.json({ error: "Couldn't load your chat." }, { status: 500 });
  }
}
