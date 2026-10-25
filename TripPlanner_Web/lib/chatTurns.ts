import { prisma } from "@/lib/prisma";
import type { ChatTurn } from "@/lib/jarvisBrain";
import { parseStoredUserMessage } from "@/lib/jarvis/attachments";

/**
 * What the two ways of taking a chat turn have in common - typed
 * (`POST /api/chat`) and spoken (`POST /api/chat/voice`): which session a turn
 * belongs to, which earlier turns the brain is shown, and how a message is
 * saved. The routes differ only in WHEN they know the words (typed: at once;
 * spoken: after transcription), so each composes these in its own order.
 */

/** One bucket per person for both kinds of turn: speaking is not a way round the typing limit. */
export const CHAT_RATE_LIMIT = 30;
export const CHAT_RATE_WINDOW_MS = 60_000;

/** How many earlier turns the brain is shown, and how many the page loads. */
export const HISTORY_TURNS = 20;
/** A new session is titled with the start of its first message. */
export const TITLE_LENGTH = 60;

export const messageFields = { id: true, role: true, content: true, createdAt: true } as const;

function toRole(role: string): "user" | "jarvis" {
  return role === "user" ? "user" : "jarvis";
}

export interface TurnTarget {
  /** Continue this session (must be the person's own). */
  conversationId?: string;
  /** Start a NEW session instead of continuing the latest one. Ignored when conversationId names one. */
  newConversation?: boolean;
}

/**
 * The person's session this turn continues, or null when there is none yet (a
 * new session, or a first message ever). A named session that is not theirs is
 * also null - the caller tells that apart by `target.conversationId` being set,
 * and answers 404, exactly as for one that does not exist.
 */
export async function findConversation(userId: string, target: TurnTarget): Promise<{ id: string } | null> {
  if (target.conversationId) {
    return prisma.conversation.findFirst({ where: { id: target.conversationId, userId }, select: { id: true } });
  }
  if (target.newConversation) return null;
  return prisma.conversation.findFirst({ where: { userId }, orderBy: { updatedAt: "desc" }, select: { id: true } });
}

export function createConversation(userId: string, firstMessage: string): Promise<{ id: string }> {
  return prisma.conversation.create({
    data: { userId, title: firstMessage.slice(0, TITLE_LENGTH) },
    select: { id: true },
  });
}

/**
 * The last HISTORY_TURNS turns of a session, oldest first - read before the turn being taken is added.
 * A message that carried a file goes to the brain as its text plus the file (lib/jarvis/attachments.ts),
 * so "and what does page 3 say?" in a later turn still knows where the file is.
 */
export async function loadHistory(conversationId: string): Promise<ChatTurn[]> {
  const earlier = await prisma.message.findMany({
    where: { conversationId },
    orderBy: { createdAt: "desc" },
    take: HISTORY_TURNS,
    select: { role: true, content: true },
  });
  return earlier.reverse().map((m): ChatTurn => {
    const role = toRole(m.role);
    if (role !== "user") return { role, content: m.content };
    const { text, attachments } = parseStoredUserMessage(m.content);
    return attachments.length > 0 ? { role, content: text, attachments } : { role, content: m.content };
  });
}

/** `content` is the stored form: the text, or text plus attachments (storedUserMessage in lib/jarvis/attachments.ts). */
export function saveUserMessage(conversationId: string, content: string) {
  return prisma.message.create({ data: { conversationId, role: "user", content }, select: messageFields });
}

/** Saves Jarvis's reply and bumps the session's updatedAt, so "latest session" follows the last activity. */
export async function saveReply(conversationId: string, content: string) {
  const [reply] = await prisma.$transaction([
    prisma.message.create({ data: { conversationId, role: "jarvis", content }, select: messageFields }),
    prisma.conversation.update({ where: { id: conversationId }, data: { updatedAt: new Date() }, select: { id: true } }),
  ]);
  return reply;
}
