import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import { getSessionUserId } from "@/lib/auth";
import {
  CHAT_RATE_LIMIT,
  CHAT_RATE_WINDOW_MS,
  createConversation,
  findConversation,
  loadHistory,
  saveReply,
  saveUserMessage,
} from "@/lib/chatTurns";
import { askJarvisByVoice, JarvisBrainError, JarvisVoiceError, type ChatTurn } from "@/lib/jarvisBrain";
import { checkRateLimit } from "@/lib/rateLimit";

// A minute of speech is well under 1 MB in any browser's codec; the brain enforces the same ceiling.
const MAX_AUDIO_BYTES = 10 * 1024 * 1024;

const TargetSchema = z.object({
  conversationId: z.string().trim().min(1).max(128).optional(),
  newConversation: z.boolean().optional(),
});

/**
 * POST /api/chat/voice - the hub's spoken turn. multipart/form-data:
 *
 *   audio             the recording (whatever MediaRecorder produced: WebM, MP4, Ogg...)
 *   conversationId?   continue this session; omit to continue the latest one
 *   newConversation?  "true" starts a NEW session instead (ignored with conversationId)
 *
 * The brain (Python, POST /voice) transcribes the audio and answers the words
 * exactly as it would a typed message. This route then saves BOTH sides of the
 * turn - the transcript as the person's message, the answer as Jarvis's - so a
 * spoken turn reads in the history like any other, and returns what the typed
 * route returns plus the words that were heard.
 *
 * Unlike a typed turn, the person's message does not exist until the audio has
 * been transcribed, so it is saved afterwards. The one failure that must not
 * lose it - the words were heard and the brain then had no answer - saves the
 * transcript before answering 502, as the typed route does.
 *
 * Responses: 200 { conversationId, userMessage, reply, transcript, engine },
 * 400 not a recording, 401 signed out, 404 unknown conversation, 413 too big,
 * 415 not audio, 422 nothing was heard, 429 rate-limited, 502 transcription or
 * the brain failed, 500 database failed.
 *
 * `engine` says who transcribed it: "openai" (the Whisper API) or "local"
 * (faster-whisper on the brain's own machine, when it has no OpenAI key).
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

  const form = await request.formData().catch(() => null);
  const audio = form?.get("audio");
  if (!form || !audio || typeof audio === "string" || audio.size === 0) {
    return NextResponse.json({ error: "audio is required: send the recording as the 'audio' field." }, { status: 400 });
  }
  if (audio.size > MAX_AUDIO_BYTES) {
    return NextResponse.json({ error: "That recording is too long." }, { status: 413 });
  }
  // An empty type is allowed (some browsers omit it); a type that names something else is not.
  if (audio.type && !/^(audio|video)\//i.test(audio.type)) {
    return NextResponse.json({ error: "That file is not audio." }, { status: 415 });
  }

  const rawId = form.get("conversationId");
  const target = TargetSchema.safeParse({
    conversationId: typeof rawId === "string" && rawId.trim() ? rawId : undefined,
    newConversation: form.get("newConversation") === "true",
  });
  if (!target.success) {
    return NextResponse.json({ error: "conversationId is not valid." }, { status: 400 });
  }
  const { conversationId, newConversation } = target.data;

  // Looked up before the audio goes anywhere, so a stale session id costs no transcription.
  let existing: { id: string } | null;
  let history: ChatTurn[] = [];
  try {
    existing = await findConversation(userId, { conversationId, newConversation });
    if (conversationId && !existing) {
      return NextResponse.json({ error: "Conversation not found." }, { status: 404 });
    }
    if (existing) history = await loadHistory(existing.id);
  } catch (error) {
    console.error("POST /api/chat/voice failed loading the conversation:", error);
    return NextResponse.json({ error: "Couldn't load your conversation. Please try again." }, { status: 500 });
  }

  let heard: Awaited<ReturnType<typeof askJarvisByVoice>>;
  try {
    heard = await askJarvisByVoice(audio, history, { userId, appUrl: request.nextUrl.origin });
  } catch (error) {
    const failure = error instanceof JarvisVoiceError ? error : null;
    const message = error instanceof JarvisBrainError ? error.message : "Jarvis couldn't answer just now.";
    console.error("POST /api/chat/voice: the brain failed:", error);

    if (failure?.noSpeech) {
      return NextResponse.json({ error: message }, { status: 422 });
    }
    if (failure?.transcript) {
      // The words were heard: keep them, like a typed message that the brain then failed to answer.
      try {
        const conversation = existing ?? (await createConversation(userId, failure.transcript));
        const userMessage = await saveUserMessage(conversation.id, failure.transcript);
        return NextResponse.json({ error: message, conversationId: conversation.id, userMessage }, { status: 502 });
      } catch (saveError) {
        console.error("POST /api/chat/voice failed saving the heard words:", saveError);
      }
    }
    return NextResponse.json({ error: message }, { status: 502 });
  }

  try {
    const conversation = existing ?? (await createConversation(userId, heard.transcript));
    const userMessage = await saveUserMessage(conversation.id, heard.transcript);
    const reply = await saveReply(conversation.id, heard.reply);
    return NextResponse.json({
      conversationId: conversation.id,
      userMessage,
      reply,
      transcript: heard.transcript,
      engine: heard.engine,
    });
  } catch (error) {
    console.error("POST /api/chat/voice failed saving the turn:", error);
    return NextResponse.json({ error: "Couldn't save the conversation. Please try again." }, { status: 500 });
  }
}
