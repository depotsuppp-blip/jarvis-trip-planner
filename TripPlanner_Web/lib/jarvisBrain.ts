import type { Attachment } from "@/lib/jarvis/attachments";
import { parseReplyAudio, type ReplyAudio } from "@/lib/jarvis/replyAudio";

/**
 * The one place the chat route asks "what does Jarvis say?": a POST to the
 * Python brain. The brain's side of the contract:
 *
 *   POST {JARVIS_BRAIN_URL}   default http://127.0.0.1:8000/chat
 *   body  { prompt: string,
 *           history: [{ role: "user" | "jarvis", content, attachments? }],
 *           user_id?: string, app_url?: string, speak?: true, interactive?: false,
 *           attachments?: [{ path, name, mime, size }] }
 *   reply text/event-stream - the brain's state machine as it works, one JSON per `data:`:
 *           { type: "progress", node?, status: "Delegating to Head of Analytics..." }   any number
 *           { type: "action_required", approval_id, tool, prompt, details: [{ name, value }], expires_in }
 *                                     a confirmation-gated tool is WAITING for the person's Allow or Deny
 *           { type: "action_resolved", approval_id, decision: "allow" | "deny" | "timeout" }   it was answered
 *           { type: "final", status: "Done", message: string | object, chat_text?: string,
 *             spoken_audio_text?: string | null, audio?: { mime, base64 } | null }
 *           { type: "error", status: "Failed", message, code }                            instead of final
 *         (a brain from before the stream answers { reply, audio } as JSON, and that is read too)
 *
 *   progress = what Jarvis is doing right now, in a few words, for the hub's thinking bubble
 *           (askJarvis hands it to `onProgress`). Free text from the brain: it is trimmed and
 *           capped here, and never stored.
 *
 *   action_required = a tool that changes something outside Jarvis (sends a LINE message, starts a
 *           server, writes a new tool) is paused until the person says yes. It reaches the page as an
 *           Approval Card (`onApproval`); the answer goes back through decideApproval (below), and
 *           the brain carries on - or reports the action as declined. The brain only asks the owner
 *           (JARVIS_OWNER_USER_ID) and gives up after JARVIS_APPROVAL_TIMEOUT (60 s), well inside
 *           BRAIN_TIMEOUT_MS. Every field is checked here (readApproval): the page shows what the
 *           brain says the action is, so it is trimmed, capped and never trusted as markup.
 *
 *   Stopping: when the person stops the turn (`signal`), the connection to the brain closes and
 *           the brain stops the turn itself - the model request in flight is closed, no further
 *           tool starts, nothing is kept - and is free for the next message at once.
 *
 *   interactive = false when the caller has no way to show an Approval Card (no `onApproval`): the
 *           brain then declines gated tools straight away instead of waiting for an answer.
 *
 *   attachments = files the person attached to this message, already saved under
 *           the Jarvis project's uploads/ folder (lib/uploads.ts); `path` is
 *           relative to that project ("uploads/3f...-report.pdf"). The brain adds a
 *           line to the prompt that says where the file is and which tool reads it,
 *           and the model reads it from there. `prompt` may be "" when a file came
 *           with no words. A user turn in `history` carries its attachments the same
 *           way, so a follow-up question can still find the file.
 *
 *   speak = the person has voice replies on: the brain also sends what the voice
 *           says - `spoken_audio_text`, ENGLISH ALWAYS (the reply when it is already
 *           short English, else its English translation and summary, at most 200
 *           characters) - and that text voiced (Edge-TTS, English voice) as `audio`.
 *           The reply itself (`message` / `chat_text`) may be Thai and is never read
 *           aloud. audio is null when the voice failed, and the reply is then just
 *           text. Both are checked here (parseReplyAudio, spokenText) and handed to
 *           the page; neither is stored.
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
 *           spoken_audio_text?: string | null, audio?: { mime, base64 } | null }
 *   422 { detail }                 nothing was heard in the recording
 *   502 { detail, transcript? }    transcription failed (no transcript), or the
 *                                  words were heard but the brain had no answer
 *
 * `engine` says who did the listening: "openai" is the Whisper API (the brain has
 * an OPENAI_API_KEY and the recording left the machine); "local" is faster-whisper
 * on the brain's own machine.
 *
 * The approvals door (decideApproval) carries the person's answer to an Approval Card back:
 *
 *   POST {JARVIS_APPROVALS_URL}/<approval_id>   default: JARVIS_BRAIN_URL with its /chat swapped for /approvals
 *   body  { user_id, decision: "allow" | "deny" }
 *   reply 200 { ok: true } | 404 the card is gone (answered, timed out, the turn stopped) | 403 not that user's
 *
 * The status door (getJarvisStatus) is what the hub's sidebar shows as "Agents & tools":
 *
 *   GET {JARVIS_HEALTH_URL}   default: JARVIS_BRAIN_URL with its /chat swapped for /health
 *   reply { ok: boolean, mcp_servers: [{ name, connected, tools: string[], error }],
 *           manager: { provider, model, on_standby, primary: { provider, model, state, last_error?, back_in? } },
 *           agents: [{ name, title, tool, active, routes: [{ role, provider, model, state, why?, last_error? }],
 *                      tools: string[] }],
 *           heads: [{ name, title, tools: string[], state: "ready" | "not configured" | "off", why?, owner_only }], ... }
 *
 *   ok = the brain has a language model to answer with. mcp_servers = its tool
 *   servers (web search, memory, ...) as they are right now; `error` is for the
 *   logs, never the page. manager = the brain's own model (Claude, or the Gemini
 *   standby and why); agents = the specialist agents it delegates to (Python
 *   agents/), each with its routes in order and the MCP tools it holds. A brain
 *   from before the agents sends neither, and the sidebar then shows none. heads = what Jarvis
 *   can do that is not an LLM agent: the Heads of Creative (images), Publishing (PowerPoint)
 *   and Analytics (Excel), and Google Classroom; `why` is a short fixed phrase from the brain
 *   ("needs OPENAI_API_KEY") and is checked against a pattern here before it is shown.
 *   Free text (`why`, `error`) never leaves this file: it can hold local paths.
 *
 * The artifacts door (fetchArtifact) is where a file Jarvis made comes from. A reply
 * shows a picture as `![Generated Image](/artifacts/image_YYYYMMDD_HHMMSS.png)` and offers
 * a presentation as `[Download Presentation](/artifacts/presentation_YYYYMMDD_HHMMSS.pptx)`,
 * a spreadsheet as `[Download Spreadsheet](/artifacts/spreadsheet_YYYYMMDD_HHMMSS.xlsx)`;
 * the browser asks THIS app for that address (app/artifacts/[name]/route.ts), which
 * fetches it from:
 *
 *   GET {JARVIS_ARTIFACTS_URL}/<name>   default: JARVIS_BRAIN_URL with its /chat swapped for /artifacts
 *   reply the file (image/png, image/jpeg, image/webp, the .pptx or .xlsx type, or application/pdf); 404 when there is none
 */

export type ChatTurn = { role: "user" | "jarvis"; content: string; attachments?: Attachment[] };

/**
 * Who is asking, where this app lives, and whether to voice the reply - see the contract above.
 * `signal` is the person's own request: when they stop it (the hub's "Stop generating"), the
 * call to the brain is dropped and a JarvisBrainCancelled is thrown, so nothing is saved - and
 * the brain, seeing its connection close, stops the turn too (see Stopping above).
 */
export type ChatContext = { userId?: string; appUrl?: string; speak?: boolean; signal?: AbortSignal };

/**
 * A typed turn's extras: the files attached to it (see `attachments` in the contract above), and
 * `onProgress`, told each step the brain reports while it works ("Searching the web...").
 */
export type AskContext = ChatContext & {
  attachments?: Attachment[];
  onProgress?: (status: string) => void;
  /** A confirmation-gated tool is waiting for the person (see action_required above). */
  onApproval?: (approval: BrainApproval) => void;
  /** That card was answered, timed out or was given up on: the page can close it. */
  onApprovalResolved?: (resolved: BrainApprovalResolved) => void;
};

/** What the brain said, and its voice if it was asked for one and could give it. */
export interface BrainAnswer {
  reply: string;
  audio: ReplyAudio | null;
  /** What the voice says: the English spoken text the audio is made from (null without a voice). */
  spokenText: string | null;
}

// The brain keeps the spoken text within 200 characters; this only refuses a runaway value.
const MAX_SPOKEN_CHARS = 400;

/** The brain's `spoken_audio_text` as one trimmed line, or null when there is none (or it is not text). */
function spokenText(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const text = value.replace(/\s+/g, " ").trim();
  return text && text.length <= MAX_SPOKEN_CHARS ? text : null;
}

/** What a paused action is, as the Approval Card shows it. Nothing here is trusted as markup. */
export interface BrainApproval {
  /** Hands the answer back to the right waiting action (decideApproval). */
  id: string;
  /** The tool's name: "send_line_notification", "start_subtrack_env", ... */
  tool: string;
  /** The question, in a sentence: "Do you authorize sending this LINE message?" */
  prompt: string;
  /** The tool's arguments, as name and value (a credential's value arrives as "(hidden)"). */
  details: { name: string; value: string }[];
  /** Seconds the brain waits before it counts the silence as a "no". */
  expiresInSeconds: number;
}

export interface BrainApprovalResolved {
  id: string;
  decision: "allow" | "deny" | "timeout";
}

const BRAIN_URL = process.env.JARVIS_BRAIN_URL ?? "http://127.0.0.1:8000/chat";
const VOICE_URL = process.env.JARVIS_VOICE_URL ?? BRAIN_URL.replace(/\/chat\/?$/, "/voice");
const HEALTH_URL = process.env.JARVIS_HEALTH_URL ?? BRAIN_URL.replace(/\/chat\/?$/, "/health");
const ARTIFACTS_URL = process.env.JARVIS_ARTIFACTS_URL ?? BRAIN_URL.replace(/\/chat\/?$/, "/artifacts");
const APPROVALS_URL = process.env.JARVIS_APPROVALS_URL ?? BRAIN_URL.replace(/\/chat\/?$/, "/approvals");
// Answering a card only wakes a waiting thread: it comes back at once.
const APPROVAL_TIMEOUT_MS = 8_000;
// A file on the brain's own disk: a picture of a few MB, or a deck, arrives in well under a second.
const ARTIFACT_TIMEOUT_MS = 15_000;
// The brain answers /health at once, except while it is still starting (it connects its
// tool servers first, up to ~15 s). A sidebar that waits longer than this just says "not checked".
const HEALTH_TIMEOUT_MS = 8_000;
// Tool calls and the reasoning tier can take a while - and "plan a trip" is a
// tool call that itself runs a whole generation (two LLM calls, Places, Routes:
// ~15-30s) between the model's turns. Past this the turn fails.
const BRAIN_TIMEOUT_MS = 120_000;

export class JarvisBrainError extends Error {}

/** The person stopped waiting (their request was aborted): not a failure, and nothing to save or show. */
export class JarvisBrainCancelled extends JarvisBrainError {
  constructor() {
    super("Stopped.");
  }
}

/** The brain call's signal: our own time limit, and the person's request when there is one. */
function brainSignal(context: ChatContext): AbortSignal {
  const limit = AbortSignal.timeout(BRAIN_TIMEOUT_MS);
  return context.signal ? AbortSignal.any([limit, context.signal]) : limit;
}

export async function askJarvis(
  message: string,
  history: ChatTurn[],
  context: AskContext = {}
): Promise<BrainAnswer> {
  let response: Response;
  try {
    response = await fetch(BRAIN_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify({
        prompt: message,
        history,
        // Left out when unknown, so the brain keeps its voice-assistant defaults.
        ...(context.userId ? { user_id: context.userId } : {}),
        ...(context.appUrl ? { app_url: context.appUrl } : {}),
        ...(context.speak ? { speak: true } : {}),
        // A caller that cannot show an Approval Card says so, and gated tools are declined at once.
        ...(context.onApproval ? {} : { interactive: false }),
        ...(context.attachments?.length ? { attachments: context.attachments } : {}),
      }),
      cache: "no-store",
      signal: brainSignal(context),
    });
  } catch (error) {
    if (context.signal?.aborted) throw new JarvisBrainCancelled();
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

  const streamed = (response.headers.get("content-type") ?? "").includes("text/event-stream");
  const data =
    streamed && response.body
      ? await readBrainStream(response.body, context)
      : ((await response.json().catch(() => null)) as { reply?: unknown; audio?: unknown; spoken?: unknown } | null);
  if (context.signal?.aborted) throw new JarvisBrainCancelled();
  const reply = replyAsText(data?.reply);
  if (!reply) throw new JarvisBrainError("Jarvis brain sent an empty reply.");
  return {
    reply,
    audio: context.speak ? parseReplyAudio(data?.audio) : null,
    spokenText: context.speak ? spokenText(data?.spoken) : null,
  };
}

// One progress line as the page may show it: one line, no control characters, a sensible length.
const MAX_PROGRESS_CHARS = 120;

function progressText(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const text = value.replace(/[\u0000-\u001f\u007f]+/g, " ").replace(/\s+/g, " ").trim();
  if (!text) return null;
  return text.length > MAX_PROGRESS_CHARS ? `${text.slice(0, MAX_PROGRESS_CHARS - 1).trimEnd()}…` : text;
}

const APPROVAL_ID = /^[A-Za-z0-9_-]{8,64}$/;
const APPROVAL_TOOL = /^[A-Za-z0-9_.:-]{1,80}$/;
const MAX_APPROVAL_DETAILS = 12;
const MAX_APPROVAL_VALUE_CHARS = 6000;

function cleanText(value: unknown, max: number, keepLines = false): string {
  if (typeof value !== "string") return "";
  // Control characters out (a newline and tab stay for code and multi-line messages when asked).
  const controls = keepLines ? /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]+/g : /[\u0000-\u001f\u007f]+/g;
  const text = value.replace(controls, " ").trim();
  return text.length > max ? `${text.slice(0, max - 1).trimEnd()}…` : text;
}

/** An action_required event as the page may show it, or null when it is not a well-formed request. */
export function readApproval(event: Record<string, unknown>): BrainApproval | null {
  const id = typeof event.approval_id === "string" ? event.approval_id : "";
  const tool = typeof event.tool === "string" ? event.tool : "";
  if (!APPROVAL_ID.test(id) || !APPROVAL_TOOL.test(tool)) return null;
  const details: BrainApproval["details"] = [];
  for (const item of Array.isArray(event.details) ? event.details : []) {
    const row = record(item);
    const name = cleanText(row?.name, 64);
    if (!row || !name || details.length >= MAX_APPROVAL_DETAILS) continue;
    details.push({ name, value: cleanText(row.value, MAX_APPROVAL_VALUE_CHARS, true) });
  }
  const expires = typeof event.expires_in === "number" && Number.isFinite(event.expires_in) ? event.expires_in : 60;
  return {
    id,
    tool,
    prompt: cleanText(event.prompt, 600) || `Do you authorize running ${tool}?`,
    details,
    expiresInSeconds: Math.max(5, Math.min(Math.round(expires), 100)),
  };
}

export function readApprovalResolved(event: Record<string, unknown>): BrainApprovalResolved | null {
  const id = typeof event.approval_id === "string" ? event.approval_id : "";
  const decision = event.decision === "allow" || event.decision === "deny" || event.decision === "timeout" ? event.decision : null;
  return APPROVAL_ID.test(id) && decision ? { id, decision } : null;
}

/** What came of an answer to a card. */
export type ApprovalOutcome = "recorded" | "gone" | "forbidden";

/**
 * Carries the person's Allow or Deny to the brain (POST /approvals/<id>). "gone": the card is no longer
 * waiting - it timed out, was answered, or its turn was stopped. "forbidden": it was not this user's.
 * Throws JarvisBrainError (safe to show) when the brain cannot be reached or fails.
 */
export async function decideApproval(id: string, userId: string, decision: "allow" | "deny"): Promise<ApprovalOutcome> {
  if (!APPROVAL_ID.test(id)) return "gone";
  let response: Response;
  try {
    response = await fetch(`${APPROVALS_URL}/${encodeURIComponent(id)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: userId, decision }),
      cache: "no-store",
      signal: AbortSignal.timeout(APPROVAL_TIMEOUT_MS),
    });
  } catch (error) {
    console.error(`Jarvis approvals endpoint unreachable at ${APPROVALS_URL}:`, error);
    throw new JarvisBrainError("Jarvis brain is offline.");
  }
  if (response.ok) return "recorded";
  if (response.status === 404) return "gone";
  if (response.status === 403) return "forbidden";
  console.error(`Jarvis approvals endpoint answered ${response.status} ${response.statusText}`);
  throw new JarvisBrainError(`Jarvis brain returned an error (${response.status}).`);
}

/** One server-sent event's JSON (its `data:` lines joined), or null for a comment or anything unreadable. */
function parseEvent(block: string): Record<string, unknown> | null {
  const data = block
    .split(/\r?\n/)
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(line.startsWith("data: ") ? 6 : 5))
    .join("\n");
  if (!data) return null;
  try {
    return record(JSON.parse(data));
  } catch {
    return null;
  }
}

/**
 * Reads the brain's server-sent events (see the contract above) until its final event: each
 * progress step goes to `onProgress`, and the final event's message and audio come back as
 * `{ reply, audio }`. An error event, a stream that ends without an answer, or a read that
 * fails throw a JarvisBrainError - JarvisBrainCancelled when the person stopped it.
 */
async function readBrainStream(
  body: ReadableStream<Uint8Array>,
  context: AskContext
): Promise<{ reply?: unknown; audio?: unknown; spoken?: unknown }> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      let chunk: ReadableStreamReadResult<Uint8Array>;
      try {
        chunk = await reader.read();
      } catch (error) {
        if (context.signal?.aborted) throw new JarvisBrainCancelled();
        const timedOut = error instanceof DOMException && error.name === "TimeoutError";
        console.error("Jarvis brain stream broke off:", error);
        throw new JarvisBrainError(timedOut ? "Jarvis brain took too long to answer." : "Jarvis brain stopped answering.");
      }
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });
      for (let end = buffer.search(/\r?\n\r?\n/); end !== -1; end = buffer.search(/\r?\n\r?\n/)) {
        const block = buffer.slice(0, end);
        buffer = buffer.slice(end).replace(/^\r?\n\r?\n/, "");
        const event = parseEvent(block);
        if (!event) continue;
        if (event.type === "progress") {
          const status = progressText(event.status);
          if (status) context.onProgress?.(status);
        } else if (event.type === "action_required") {
          const approval = readApproval(event);
          if (approval) context.onApproval?.(approval);
          else console.error("Jarvis brain sent an approval request that could not be read; it will time out as a no.");
        } else if (event.type === "action_resolved") {
          const resolved = readApprovalResolved(event);
          if (resolved) context.onApprovalResolved?.(resolved);
        } else if (event.type === "final") {
          return { reply: event.message, audio: event.audio, spoken: event.spoken_audio_text };
        } else if (event.type === "error") {
          const code = typeof event.code === "number" ? event.code : 502;
          console.error(`Jarvis brain reported an error (${code}) mid-turn.`);
          // The brain's own words are for its console; the person gets the same sentences as before.
          throw new JarvisBrainError(code === 502 ? "Jarvis brain sent an empty reply." : `Jarvis brain returned an error (${code}).`);
        }
      }
    }
  } finally {
    // After the final event (or a failure) nothing more is read: let the connection go.
    reader.cancel().catch(() => {});
  }
  if (context.signal?.aborted) throw new JarvisBrainCancelled();
  throw new JarvisBrainError("Jarvis brain stopped before it answered.");
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
  /** The English text that audio says (see BrainAnswer). */
  spokenText: string | null;
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
      signal: brainSignal(context),
    });
  } catch (error) {
    if (context.signal?.aborted) throw new JarvisBrainCancelled();
    const timedOut = error instanceof DOMException && error.name === "TimeoutError";
    console.error(`Jarvis voice endpoint unreachable at ${VOICE_URL}:`, error);
    throw new JarvisVoiceError(timedOut ? "Jarvis brain took too long to answer." : "Jarvis brain is offline.");
  }

  const data: unknown = await response.json().catch(() => null);
  if (context.signal?.aborted) throw new JarvisBrainCancelled();
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
    spokenText: context.speak ? spokenText(body.spoken_audio_text) : null,
  };
}

/** One of the brain's tool servers, as the sidebar needs it. */
export interface BrainToolServer {
  /** The server's config name: "web_search", "memory", ... */
  name: string;
  connected: boolean;
  /** How many tools it offers while connected. */
  tools: number;
}

/** How one model route is: answering, resting after a failure (credit, quota, rate limit), failed until restart, or without a key. */
export type BrainRouteState = "ready" | "resting" | "failed" | "not configured";

export interface BrainRoute {
  role: "primary" | "fallback" | "last resort";
  /** "anthropic", "gemini", "openai", "groq". */
  provider: string;
  model: string;
  state: BrainRouteState;
  /** Why it last failed, as a kind ("billing", "quota", "rate_limit", ...). */
  lastError?: string;
}

/** One specialist agent (Python agents/). */
export interface BrainAgent {
  /** "data", "research", "productivity". */
  name: string;
  title: string;
  /** It is taking tasks; when false the manager does its work itself. */
  active: boolean;
  routes: BrainRoute[];
  /** The tool servers whose tools it holds. */
  servers: string[];
}

/** The manager's own model: Claude, or the Gemini standby and why. */
export interface BrainManager {
  provider: string;
  model: string;
  onStandby: boolean;
  primary: { provider: string; model: string; state: BrainRouteState; lastError?: string; backInSeconds?: number };
}

/** One capability that is not an LLM agent: the Heads of Creative, Publishing and Analytics, or Google Classroom. */
export interface BrainHead {
  /** "creative", "publishing", "analytics", "classroom". */
  name: string;
  title: string;
  /** "ready": Jarvis can use it now. "not configured": on, but something is not set up. "off": switched off. */
  state: "ready" | "not configured" | "off";
  /** What is missing, in a few words ("needs OPENAI_API_KEY"); empty when ready. */
  why: string;
  /** Only the owner's own account can use it (Google Classroom). */
  ownerOnly: boolean;
}

export interface BrainStatus {
  /** The brain has a language model to answer with. */
  ready: boolean;
  servers: BrainToolServer[];
  /** Null from a brain that does not report it. */
  manager: BrainManager | null;
  /** Empty with the team off, or from a brain without one. */
  agents: BrainAgent[];
  /** Creative, Publishing, Analytics and Classroom. Empty from a brain from before they were reported. */
  heads: BrainHead[];
}

const SERVER_NAME = /^[a-z0-9_]{1,64}$/;
const PROVIDER = /^[a-z0-9_-]{1,32}$/;
const MODEL = /^[A-Za-z0-9._:/-]{1,80}$/;
const ERROR_KIND = /^[a-z_]{1,32}$/;
const ROLES = ["primary", "fallback", "last resort"] as const;
const ROUTE_STATES: readonly BrainRouteState[] = ["ready", "resting", "failed", "not configured"];
const MCP_TOOL = /^mcp__([a-z0-9_]{1,64})__/;

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

function routeState(value: unknown): BrainRouteState | null {
  return ROUTE_STATES.includes(value as BrainRouteState) ? (value as BrainRouteState) : null;
}

function errorKind(value: unknown): string | undefined {
  return typeof value === "string" && ERROR_KIND.test(value) ? value : undefined;
}

function readRoute(value: unknown): BrainRoute | null {
  const route = record(value);
  if (!route) return null;
  const role = ROLES.find((candidate) => candidate === route.role);
  const state = routeState(route.state);
  if (!role || !state || typeof route.provider !== "string" || !PROVIDER.test(route.provider)) return null;
  if (typeof route.model !== "string" || !MODEL.test(route.model)) return null;
  const lastError = errorKind(route.last_error);
  return { role, provider: route.provider, model: route.model, state, ...(lastError ? { lastError } : {}) };
}

function readAgent(value: unknown): BrainAgent | null {
  const agent = record(value);
  if (!agent || typeof agent.name !== "string" || !SERVER_NAME.test(agent.name)) return null;
  const routes = (Array.isArray(agent.routes) ? agent.routes : []).map(readRoute);
  if (routes.length === 0 || routes.some((route) => route === null)) return null;
  const servers = new Set<string>();
  for (const tool of Array.isArray(agent.tools) ? agent.tools : []) {
    const match = typeof tool === "string" ? MCP_TOOL.exec(tool) : null;
    if (match) servers.add(match[1]);
  }
  const title = typeof agent.title === "string" ? agent.title.trim().slice(0, 80) : "";
  return { name: agent.name, title: title || agent.name, active: agent.active === true, routes: routes as BrainRoute[], servers: [...servers] };
}

const HEAD_STATES: readonly BrainHead["state"][] = ["ready", "not configured", "off"];
// `why` is a short phrase the brain wrote for the sidebar; anything longer or odder is dropped, not shown.
const HEAD_WHY = /^[A-Za-z0-9 ()_./,=-]{0,80}$/;

function readHead(value: unknown): BrainHead | null {
  const head = record(value);
  if (!head || typeof head.name !== "string" || !SERVER_NAME.test(head.name)) return null;
  const state = HEAD_STATES.find((candidate) => candidate === head.state);
  if (!state) return null;
  const title = typeof head.title === "string" ? head.title.trim().slice(0, 80) : "";
  const why = typeof head.why === "string" && HEAD_WHY.test(head.why) ? head.why.trim() : "";
  return { name: head.name, title: title || head.name, state, why, ownerOnly: head.owner_only === true };
}

function readManager(value: unknown): BrainManager | null {
  const manager = record(value);
  const primary = record(manager?.primary);
  if (!manager || !primary) return null;
  const state = routeState(primary.state);
  const named = (provider: unknown, model: unknown) =>
    typeof provider === "string" && PROVIDER.test(provider) && typeof model === "string" && MODEL.test(model);
  if (!state || !named(manager.provider, manager.model) || !named(primary.provider, primary.model)) return null;
  const lastError = errorKind(primary.last_error);
  const backIn = typeof primary.back_in === "number" && Number.isFinite(primary.back_in) && primary.back_in >= 0 ? Math.round(primary.back_in) : undefined;
  return {
    provider: manager.provider as string,
    model: manager.model as string,
    onStandby: manager.on_standby === true,
    primary: {
      provider: primary.provider as string,
      model: primary.model as string,
      state,
      ...(lastError ? { lastError } : {}),
      ...(backIn !== undefined ? { backInSeconds: backIn } : {}),
    },
  };
}

/** The brain's GET /health, narrowed to what the hub shows. Throws JarvisBrainError when it cannot be read. */
export async function getJarvisStatus(): Promise<BrainStatus> {
  let response: Response;
  try {
    response = await fetch(HEALTH_URL, { cache: "no-store", signal: AbortSignal.timeout(HEALTH_TIMEOUT_MS) });
  } catch (error) {
    const timedOut = error instanceof DOMException && error.name === "TimeoutError";
    throw new JarvisBrainError(timedOut ? "Jarvis brain is still starting." : "Jarvis brain is offline.");
  }
  if (!response.ok) throw new JarvisBrainError(`Jarvis brain returned an error (${response.status}).`);

  const data: unknown = await response.json().catch(() => null);
  const body = (data && typeof data === "object" ? data : {}) as Record<string, unknown>;
  const listed = Array.isArray(body.mcp_servers) ? body.mcp_servers : [];
  const servers: BrainToolServer[] = [];
  for (const item of listed) {
    if (!item || typeof item !== "object") continue;
    const server = item as Record<string, unknown>;
    // Names come from the brain's own config; anything odd is left out rather than shown.
    if (typeof server.name !== "string" || !SERVER_NAME.test(server.name)) continue;
    const connected = server.connected === true;
    servers.push({ name: server.name, connected, tools: connected && Array.isArray(server.tools) ? server.tools.length : 0 });
  }
  const agents = (Array.isArray(body.agents) ? body.agents : [])
    .map(readAgent)
    .filter((agent): agent is BrainAgent => agent !== null)
    .slice(0, 12);
  const heads = (Array.isArray(body.heads) ? body.heads : [])
    .map(readHead)
    .filter((head): head is BrainHead => head !== null)
    .slice(0, 12);
  return { ready: body.ok === true, servers, manager: readManager(body.manager), agents, heads };
}

/**
 * The brain's GET /artifacts/<name>: one file Jarvis made - a picture, a presentation - as
 * the brain answers it (any status - the caller decides what a 404 means). `name` must
 * already be checked (lib/jarvis/artifacts.ts). Throws JarvisBrainError when the brain
 * cannot be reached.
 */
export async function fetchArtifact(name: string): Promise<Response> {
  try {
    return await fetch(`${ARTIFACTS_URL}/${encodeURIComponent(name)}`, {
      cache: "no-store",
      signal: AbortSignal.timeout(ARTIFACT_TIMEOUT_MS),
    });
  } catch (error) {
    const timedOut = error instanceof DOMException && error.name === "TimeoutError";
    throw new JarvisBrainError(timedOut ? "Jarvis brain took too long to send the file." : "Jarvis brain is offline.");
  }
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
