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
import { askJarvis, JarvisBrainCancelled, JarvisBrainError, type ChatTurn } from "@/lib/jarvisBrain";
import { MAX_DOCUMENT_BYTES, storedUserMessage, type Attachment } from "@/lib/jarvis/attachments";
import { speakableAudio, type ReplyAudio } from "@/lib/jarvis/replyAudio";
import { prisma } from "@/lib/prisma";
import { checkRateLimit } from "@/lib/rateLimit";
import { discardUpload, prepareUpload, saveUpload, UploadError, type PreparedUpload } from "@/lib/uploads";

const MAX_MESSAGE_LENGTH = 4000;
// A multipart body is the file plus a few short fields; past this it is refused before it is read into memory.
const MAX_MULTIPART_BYTES = MAX_DOCUMENT_BYTES + 64 * 1024;

const ChatBodySchema = z.object({
  message: z.string().trim().min(1).max(MAX_MESSAGE_LENGTH),
  // Omit to continue the user's latest conversation (or start the first one).
  conversationId: z.string().trim().min(1).max(128).optional(),
  // True starts a NEW session instead of continuing the latest one (the hub's
  // "New session"). Ignored when conversationId names a session.
  newConversation: z.boolean().optional(),
  // The person has voice replies on: also return the reply as audio when it is short enough.
  speak: z.boolean().optional(),
});

// The multipart form's fields, once read: the same as the JSON body, except that the
// message may be empty when a file came with it.
const ChatFormSchema = ChatBodySchema.extend({
  message: z.string().trim().max(MAX_MESSAGE_LENGTH),
});

interface TurnInput {
  message: string;
  conversationId?: string;
  newConversation?: boolean;
  speak?: boolean;
  file: File | null;
}

const BAD_BODY = `message is required (1-${MAX_MESSAGE_LENGTH} characters).`;

/** The turn the request asks for - JSON or multipart - or the response that refuses it. */
async function readTurn(request: NextRequest): Promise<{ turn: TurnInput } | { refused: NextResponse }> {
  const type = request.headers.get("content-type") ?? "";
  if (!type.toLowerCase().startsWith("multipart/form-data")) {
    const parsed = ChatBodySchema.safeParse(await request.json().catch(() => null));
    if (!parsed.success) return { refused: NextResponse.json({ error: BAD_BODY }, { status: 400 }) };
    return { turn: { ...parsed.data, file: null } };
  }

  const length = Number(request.headers.get("content-length"));
  if (Number.isFinite(length) && length > MAX_MULTIPART_BYTES) {
    return { refused: NextResponse.json({ error: "That file is too big to attach." }, { status: 413 }) };
  }
  const form = await request.formData().catch(() => null);
  if (!form) return { refused: NextResponse.json({ error: "That upload could not be read." }, { status: 400 }) };

  const files = form.getAll("file").filter((value): value is File => typeof value !== "string" && value.size > 0);
  if (files.length > 1) {
    return { refused: NextResponse.json({ error: "Attach one file at a time." }, { status: 400 }) };
  }
  const field = (name: string) => {
    const value = form.get(name);
    return typeof value === "string" ? value : undefined;
  };
  const parsed = ChatFormSchema.safeParse({
    message: field("message") ?? "",
    conversationId: field("conversationId")?.trim() || undefined,
    newConversation: field("newConversation") === "true",
    speak: field("speak") === "true",
  });
  const file = files[0] ?? null;
  if (!parsed.success || (!parsed.data.message && !file)) {
    return { refused: NextResponse.json({ error: BAD_BODY }, { status: 400 }) };
  }
  return { turn: { ...parsed.data, file } };
}

/**
 * POST /api/chat - the Jarvis hub's text turn, sent one of two ways:
 *
 *   application/json     { message, conversationId?, newConversation?, speak? }
 *   multipart/form-data  message (may be empty when a file is sent), file?,
 *                        conversationId?, newConversation? ("true"), speak? ("true")
 *
 * Saves the signed-in user's message, asks the brain (lib/jarvisBrain.ts) for
 * a reply, saves that too, and returns both. The user is always the
 * session's, and a conversationId that isn't theirs is treated as not found.
 *
 * An attached `file` (one; PDF, Word .docx or plain text - lib/jarvis/attachments.ts)
 * is checked and saved to the Jarvis project's uploads/ folder (lib/uploads.ts),
 * stored with the message, and handed to the brain as a path, which tells the
 * model where to read it. Failures before anything is saved: 413 too big, 415 not
 * a kind Jarvis can read (or not what its name says), 503 this server has no
 * Jarvis project beside it to save into.
 *
 * The user's message is written before the brain is called, so a brain
 * failure never loses it: that case answers 502 and the message stays in
 * the history.
 *
 * "Stop generating" in the hub aborts this request. The call to the brain is
 * dropped with it (request.signal), and the reply - if it still arrives - is not
 * saved: the person's message stays, unanswered, exactly as they saw it. The
 * brain itself (Python) finishes the turn it started; this only stops waiting.
 *
 * The brain is told who is asking (the session's user id) and which origin
 * this app is served from: its trip tool plans a trip FOR that person
 * (personalized from their Taste Profile) by calling back into this instance
 * (POST /api/trip/create), and the link it returns is built on the same
 * origin. A reply may therefore contain that link - the hub page makes it
 * clickable. The link itself is never a secret: opening it needs the same
 * sign-in as any trip page.
 *
 * With `speak: true` the brain also voices the reply, and the response carries it as
 * `audio: { mime, base64 }` - only when the reply's own text is at most 200 characters
 * (lib/jarvis/replyAudio.ts) and the voice worked. The audio is for this response only:
 * it is not saved with the message, and a reply without it is just text.
 *
 * Responses: 200 { conversationId, userMessage, reply, audio? }, 400 bad body, 401
 * signed out, 404 unknown conversation, 413 / 415 / 503 the file (above), 429
 * rate-limited, 502 brain failed, 500 database (or disk) failed, 499 stopped by the person
 * (the reply is not saved). With a file,
 * `userMessage.content` is the stored form: JSON with the text and the file.
 */
export async function POST(request: NextRequest) {
  const userId = await getSessionUserId();
  if (!userId) {
    return NextResponse.json({ error: "Sign in to chat with Jarvis." }, { status: 401 });
  }

  const rateLimit = checkRateLimit(`chat:${userId}`, CHAT_RATE_LIMIT, CHAT_RATE_WINDOW_MS);
  if (!rateLimit.allowed) {
    return NextResponse.json(
      { error: "Too many messages - please wait a moment." },
      { status: 429, headers: { "Retry-After": String(rateLimit.retryAfterSeconds) } }
    );
  }

  const read = await readTurn(request);
  if ("refused" in read) return read.refused;
  const { message, conversationId, newConversation, speak, file } = read.turn;

  // The file is checked before anything is looked up or written, so a wrong one costs nothing.
  let upload: PreparedUpload | null = null;
  if (file) {
    try {
      upload = await prepareUpload(file);
    } catch (error) {
      if (error instanceof UploadError) return NextResponse.json({ error: error.message }, { status: error.status });
      console.error("POST /api/chat failed reading the attached file:", error);
      return NextResponse.json({ error: "That upload could not be read." }, { status: 400 });
    }
  }

  let conversation: { id: string };
  let userMessage;
  let history: ChatTurn[];
  const attachments: Attachment[] = [];
  try {
    const existing = await findConversation(userId, { conversationId, newConversation });

    if (conversationId && !existing) {
      return NextResponse.json({ error: "Conversation not found." }, { status: 404 });
    }

    // Saved once the session is known to be theirs, and before the message that points at it.
    if (upload) {
      try {
        attachments.push(await saveUpload(upload));
      } catch (error) {
        const failure = error instanceof UploadError ? error : new UploadError("Couldn't save the file. Please try again.", 500);
        return NextResponse.json({ error: failure.message }, { status: failure.status });
      }
    }

    // A file sent with no words titles its session with its name.
    conversation = existing ?? (await createConversation(userId, message || attachments[0]?.name || "Attachment"));

    // Read the earlier turns before this one is added.
    history = await loadHistory(conversation.id);

    userMessage = await saveUserMessage(conversation.id, storedUserMessage(message, attachments));
  } catch (error) {
    console.error("POST /api/chat failed saving the user's message:", error);
    // A file whose message was never saved is pointed at by nothing: take it back off the disk.
    await Promise.all(attachments.map(discardUpload));
    return NextResponse.json({ error: "Couldn't save your message. Please try again." }, { status: 500 });
  }

  let replyText: string;
  let audio: ReplyAudio | null = null;
  try {
    const answer = await askJarvis(message, history, {
      userId,
      appUrl: request.nextUrl.origin,
      speak,
      attachments,
      signal: request.signal,
    });
    replyText = answer.reply.trim();
    if (!replyText) throw new Error("The brain returned an empty reply.");
    audio = speakableAudio(replyText, answer.audio);
  } catch (error) {
    if (error instanceof JarvisBrainCancelled || request.signal.aborted) return stopped(conversation.id);
    console.error(`POST /api/chat: the brain failed for conversation ${conversation.id}:`, error);
    return NextResponse.json(
      { error: error instanceof JarvisBrainError ? error.message : "Jarvis couldn't answer just now.", conversationId: conversation.id, userMessage },
      { status: 502 }
    );
  }

  // Stopped while the answer was on its way in: the person never sees it, so it is not kept either.
  if (request.signal.aborted) return stopped(conversation.id);

  try {
    const reply = await saveReply(conversation.id, replyText);
    return NextResponse.json({ conversationId: conversation.id, userMessage, reply, ...(audio ? { audio } : {}) });
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
/**
 * The answer to a request the person stopped. Nobody reads it (the page dropped the
 * request), so it only has to be honest: 499, the code proxies use for "client closed".
 */
function stopped(conversationId: string) {
  console.info(`POST /api/chat: stopped by the person (conversation ${conversationId}); the reply is not saved.`);
  return NextResponse.json({ error: "Stopped.", conversationId }, { status: 499 });
}

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
