import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import { getSessionUserId } from "@/lib/auth";
import {
  CHAT_RATE_LIMIT,
  CHAT_RATE_WINDOW_MS,
  createConversation,
  findConversation,
  loadHistory,
  messageFields,
  saveReply,
  saveUserMessage,
} from "@/lib/chatTurns";
import { askJarvis, JarvisBrainError, type ChatTurn } from "@/lib/jarvisBrain";
import { prisma } from "@/lib/prisma";
import { checkRateLimit } from "@/lib/rateLimit";

const MAX_MESSAGE_LENGTH = 4000;

const ChatBodySchema = z.object({
  message: z.string().trim().min(1).max(MAX_MESSAGE_LENGTH),
  // Omit to continue the user's latest conversation (or start the first one).
  conversationId: z.string().trim().min(1).max(128).optional(),
  // True starts a NEW session instead of continuing the latest one (the hub's
  // "New session"). Ignored when conversationId names a session.
  newConversation: z.boolean().optional(),
});

/**
 * POST /api/chat { message, conversationId?, newConversation? } - the Jarvis hub's text turn.
 * Saves the signed-in user's message, asks the brain (lib/jarvisBrain.ts) for
 * a reply, saves that too, and returns both. The user is always the
 * session's, and a conversationId that isn't theirs is treated as not found.
 *
 * The user's message is written before the brain is called, so a brain
 * failure never loses it: that case answers 502 and the message stays in
 * the history.
 *
 * The brain is told who is asking (the session's user id) and which origin
 * this app is served from: its trip tool plans a trip FOR that person
 * (personalized from their Taste Profile) by calling back into this instance
 * (POST /api/trip/create), and the link it returns is built on the same
 * origin. A reply may therefore contain that link - the hub page makes it
 * clickable. The link itself is never a secret: opening it needs the same
 * sign-in as any trip page.
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
  const { message, conversationId, newConversation } = parsed.data;

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
    const existing = await findConversation(userId, { conversationId, newConversation });

    if (conversationId && !existing) {
      return NextResponse.json({ error: "Conversation not found." }, { status: 404 });
    }

    conversation = existing ?? (await createConversation(userId, message));

    // Read the earlier turns before this one is added.
    history = await loadHistory(conversation.id);

    userMessage = await saveUserMessage(conversation.id, message);
  } catch (error) {
    console.error("POST /api/chat failed saving the user's message:", error);
    return NextResponse.json({ error: "Couldn't save your message. Please try again." }, { status: 500 });
  }

  let replyText: string;
  try {
    replyText = (await askJarvis(message, history, { userId, appUrl: request.nextUrl.origin })).trim();
    if (!replyText) throw new Error("The brain returned an empty reply.");
  } catch (error) {
    console.error(`POST /api/chat: the brain failed for conversation ${conversation.id}:`, error);
    return NextResponse.json(
      { error: error instanceof JarvisBrainError ? error.message : "Jarvis couldn't answer just now.", conversationId: conversation.id, userMessage },
      { status: 502 }
    );
  }

  try {
    const reply = await saveReply(conversation.id, replyText);
    return NextResponse.json({ conversationId: conversation.id, userMessage, reply });
  } catch (error) {
    console.error("POST /api/chat failed saving the reply:", error);
    return NextResponse.json({ error: "Couldn't save Jarvis's reply. Please try again." }, { status: 500 });
  }
}

/**
 * GET /api/chat[?conversationId=...] - one of the signed-in user's sessions
 * (the latest unless one is named), oldest message first, as
 * { conversationId, messages }. conversationId is null (and messages empty)
 * before the first message. Scoped to the session's user: naming someone
 * else's session is a 404, exactly like naming one that does not exist.
 *
 * A message's `content` is returned as stored. For a reply from a tool it may
 * be a JSON object instead of prose; the hub parses it (lib/jarvis/messageContent.ts).
 */
export async function GET(request: NextRequest) {
  const userId = await getSessionUserId();
  if (!userId) {
    return NextResponse.json({ error: "Sign in to see your chat." }, { status: 401 });
  }

  try {
    const wanted = request.nextUrl.searchParams.get("conversationId")?.trim();
    const conversation = await prisma.conversation.findFirst({
      where: wanted ? { id: wanted, userId } : { userId },
      orderBy: { updatedAt: "desc" },
      select: { id: true },
    });
    if (wanted && !conversation) return NextResponse.json({ error: "Conversation not found." }, { status: 404 });
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
