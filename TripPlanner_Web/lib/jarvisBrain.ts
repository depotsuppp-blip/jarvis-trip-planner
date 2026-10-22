import { parseReplyAudio, type ReplyAudio } from "@/lib/jarvis/replyAudio";

/**
 * The one place the chat route asks "what does Jarvis say?": a POST to the
 * Python brain. The brain's side of the contract:
 *
 *   POST {JARVIS_BRAIN_URL}   default http://127.0.0.1:8000/chat
 *   body  { prompt: string,
 *           history: [{ role: "user" | "jarvis", content }],
 *           user_id?: string, app_url?: string, speak?: true }
 *   reply { reply: string | object, audio?: { mime, base64 } | null }
 *
 *   speak = the person has voice replies on: the brain also voices the reply
 *           (Edge-TTS) when its plain text is at most 200 characters, and sends
 *           the MP3 as `audio`. It is null otherwise - too long, or the voice
 *           failed - and the reply is then just text. The audio is checked here
 *           (parseReplyAudio) and handed to the page; it is never stored.
 *
 *   reply is normally a string: a plain sentence, or - when a tool showed the
 *   person a widget - a JSON string (see lib/jarvis/messageContent.ts for the
 *   shapes). A brain that sends the widget as an OBJECT instead is accepted too
 *   and stored as the same JSON string, so the page reads both the same way.
 *
 *   history = earlier turns, oldest first, not including `prompt`.
 *   user_id = the signed-in person asking (User.id). Jarvis's trip tool acts on
 *             their behalf - it plans the trip for them and personalizes it from
 *             their Taste Profile - so it needs to know who they are.
 *   app_url = this web app's own origin (e.g. http://localhost:3000). The trip
 *             tool calls back into THIS instance (it is the one that has the
 *             route and the database the person is looking at) and builds the
 *             link it hands back from the same origin.
 *
 * Any failure throws a JarvisBrainError whose message is safe to show the
 * user; the route keeps their message saved and answers 502.
 *
 * The voice door (askJarvisByVoice) is the same brain behind speech-to-text:
 *
 *   POST {JARVIS_VOICE_URL}   default: JARVIS_BRAIN_URL with its /chat swapped for /voice
 *   body  multipart/form-data: audio (the recording), history (a JSON list of
 *         the same turns as above), user_id?, app_url?, speak? ("true")
 *   reply { transcript: string, reply: string | object, engine: "openai" | "local",
 *           audio?: { mime, base64 } | null }
 *   422 { detail }                 nothing was heard in the recording
 *   502 { detail, transcript? }    transcription failed (no transcript), or the
 *                                  words were heard but the brain had no answer
 *
 * `engine` says who did the listening: "openai" is the Whisper API (the brain has
 * an OPENAI_API_KEY and the recording left the machine); "local" is faster-whisper
 * on the brain's own machine.
 */

export type ChatTurn = { role: "user" | "jarvis"; content: string };

/** Who is asking, where this app lives, and whether to voice the reply - see the contract above. */
export type ChatContext = { userId?: string; appUrl?: string; speak?: boolean };

/** What the brain said, and its voice if it was asked for one and could give it. */
export interface BrainAnswer {
  reply: string;
  audio: ReplyAudio | null;
}

const BRAIN_URL = process.env.JARVIS_BRAIN_URL ?? "http://127.0.0.1:8000/chat";
const VOICE_URL = process.env.JARVIS_VOICE_URL ?? BRAIN_URL.replace(/\/chat\/?$/, "/voice");
// Tool calls and the reasoning tier can take a while - and "plan a trip" is a
// tool call that itself runs a whole generation (two LLM calls, Places, Routes:
// ~15-30s) between the model's turns. Past this the turn fails.
const BRAIN_TIMEOUT_MS = 120_000;

export class JarvisBrainError extends Error {}

export async function askJarvis(
  message: string,
  history: ChatTurn[],
  context: ChatContext = {}
): Promise<BrainAnswer> {
  let response: Response;
  try {
    response = await fetch(BRAIN_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        prompt: message,
        history,
        // Left out when unknown, so the brain keeps its voice-assistant defaults.
        ...(context.userId ? { user_id: context.userId } : {}),
        ...(context.appUrl ? { app_url: context.appUrl } : {}),
        ...(context.speak ? { speak: true } : {}),
      }),
      cache: "no-store",
      signal: AbortSignal.timeout(BRAIN_TIMEOUT_MS),
    });
  } catch (error) {
    const timedOut = error instanceof DOMException && error.name === "TimeoutError";
    console.error(`Jarvis brain unreachable at ${BRAIN_URL}:`, error);
    throw new JarvisBrainError(
      timedOut ? "Jarvis brain took too long to answer." : "Jarvis brain is offline."
    );
  }

  if (!response.ok) {
    console.error(`Jarvis brain answered ${response.status} ${response.statusText}`);
    throw new JarvisBrainError(`Jarvis brain returned an error (${response.status}).`);
  }

  const data = (await response.json().catch(() => null)) as { reply?: unknown; audio?: unknown } | null;
  const reply = replyAsText(data?.reply);
  if (!reply) throw new JarvisBrainError("Jarvis brain sent an empty reply.");
  return { reply, audio: context.speak ? parseReplyAudio(data?.audio) : null };
}

/** A spoken turn that did not produce an answer. `message` is safe to show the person as it stands. */
export class JarvisVoiceError extends JarvisBrainError {
  constructor(
    message: string,
    /** True when the recording held no speech: the person simply tries again. */
    readonly noSpeech = false,
    /** What was heard, when the words got as far as the brain and only the answer failed. */
    readonly transcript?: string
  ) {
    super(message);
  }
}

export interface VoiceAnswer {
  transcript: string;
  reply: string;
  /** Who transcribed it: "openai" (the Whisper API) or "local" (faster-whisper on the brain's machine). */
  engine: string;
  /** The answer voiced, when it was asked for and the brain could (see BrainAnswer). */
  audio: ReplyAudio | null;
}

/** Transcribes `audio` and answers what it says, in one trip to the brain. */
export async function askJarvisByVoice(
  audio: Blob,
  history: ChatTurn[],
  context: ChatContext = {}
): Promise<VoiceAnswer> {
  const form = new FormData();
  form.append("audio", audio, "voice");
  form.append("history", JSON.stringify(history));
  if (context.userId) form.append("user_id", context.userId);
  if (context.appUrl) form.append("app_url", context.appUrl);
  if (context.speak) form.append("speak", "true");

  let response: Response;
  try {
    // No Content-Type header: fetch sets multipart/form-data with its boundary.
    response = await fetch(VOICE_URL, {
      method: "POST",
      body: form,
      cache: "no-store",
      signal: AbortSignal.timeout(BRAIN_TIMEOUT_MS),
    });
  } catch (error) {
    const timedOut = error instanceof DOMException && error.name === "TimeoutError";
    console.error(`Jarvis voice endpoint unreachable at ${VOICE_URL}:`, error);
    throw new JarvisVoiceError(timedOut ? "Jarvis brain took too long to answer." : "Jarvis brain is offline.");
  }

  const data: unknown = await response.json().catch(() => null);
  const body = (data && typeof data === "object" ? data : {}) as Record<string, unknown>;

  if (!response.ok) {
    console.error(`Jarvis voice endpoint answered ${response.status}:`, body.detail);
    // Python's `detail` is a sentence written for the person only on these two; elsewhere it is not.
    const sentence = typeof body.detail === "string" && body.detail.trim() ? body.detail : null;
    const transcript = typeof body.transcript === "string" && body.transcript.trim() ? body.transcript : undefined;
    if (response.status === 422 && sentence) throw new JarvisVoiceError(sentence, true);
    // Heard but not answered: the words are kept. Not heard (no transcript): speech-to-text itself failed.
    if (response.status === 502 && transcript) throw new JarvisVoiceError("Jarvis brain sent an empty reply.", false, transcript);
    if (response.status === 502 && sentence) throw new JarvisVoiceError(sentence);
    throw new JarvisVoiceError(`Jarvis brain returned an error (${response.status}).`);
  }

  const transcript = typeof body.transcript === "string" ? body.transcript.trim() : "";
  const reply = replyAsText(body.reply);
  if (!transcript || !reply) throw new JarvisVoiceError("Jarvis brain sent an empty reply.", false, transcript || undefined);
  return {
    transcript,
    reply,
    engine: typeof body.engine === "string" ? body.engine : "unknown",
    audio: context.speak ? parseReplyAudio(body.audio) : null,
  };
}

/**
 * What the brain's `reply` field is, as the string the chat stores: a string
 * as it is, an object that carries a widget as its JSON, an object that is only
 * `{ text }` as that text. Anything else - missing, blank, a number, an object
 * that says nothing - is null, which the caller reports as an empty reply.
 */
export function replyAsText(reply: unknown): string | null {
  if (typeof reply === "string") return reply.trim() ? reply : null;
  if (typeof reply !== "object" || reply === null || Array.isArray(reply)) return null;

  const body = reply as Record<string, unknown>;
  const carriesWidget =
    body.type === "widget" ||
    (typeof body.widget === "string" && body.widget.trim() !== "") ||
    (typeof body.widget === "object" && body.widget !== null) ||
    (Array.isArray(body.widgets) && body.widgets.length > 0);
  if (carriesWidget) return JSON.stringify(body);

  return typeof body.text === "string" && body.text.trim() ? body.text : null;
}
