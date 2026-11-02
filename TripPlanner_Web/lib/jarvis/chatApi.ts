import type { ServerMessageLike } from "./messageContent";
import type { ReplyAudio } from "./replyAudio";

/**
 * The hub's only door to the server: a handful of calls on `/api/chat`, each answering
 * `{ ok: true, data }` or `{ ok: false, kind, message }` and never throwing, so
 * the state layer (`chatController.ts`) reads results instead of catching.
 *
 *   GET  /api/chat                          the latest session's messages
 *   GET  /api/chat?conversationId=<id>      one session's messages
 *   GET  /api/chat/sessions                 the signed-in person's sessions, newest first
 *   POST /api/chat { message, conversationId?, newConversation?, speak? }   one typed turn
 *   POST /api/chat (multipart: message, file, conversationId?, newConversation?, speak?)   one typed turn with a file
 *        - asked for as lines of JSON (Accept: application/x-ndjson) when the caller wants Jarvis's
 *          steps on the way ("Searching the web..."): `send` with `onProgress`
 *   POST /api/chat/voice (multipart: audio, conversationId?, newConversation?, speak?)   one spoken turn
 *   GET  /api/chat/tools                    the manager's model, the specialist agents and the tool servers
 *   DELETE /api/chat/messages/<id>          deletes one saved message (the person's own)
 *
 * `speak: true` asks for the reply to come back with audio too (`audio` in the answer)
 * when it is short enough to be read aloud - see replyAudio.ts.
 *
 * `message` / `content` come back as stored: the controller parses a reply into
 * text and widgets (`messageContent.ts`), this file only moves bytes.
 */

export type ApiFailureKind =
  | "signed-out"
  | "not-found"
  | "rate-limited"
  | "no-speech"
  | "brain"
  | "server"
  | "network"
  // The page itself stopped the request ("Stop generating"): not a failure to report.
  | "cancelled";

export interface ApiFailure {
  ok: false;
  kind: ApiFailureKind;
  status: number;
  /** Safe to show the person as it stands. */
  message: string;
}

export type ApiResult<T> = { ok: true; data: T } | ApiFailure;

export interface ConversationData {
  conversationId: string | null;
  messages: ServerMessageLike[];
}

export interface SessionsData {
  sessions: { id: string; title: string | null; updatedAt: string }[];
}

export interface TurnData {
  conversationId: string;
  userMessage: ServerMessageLike;
  reply: ServerMessageLike;
  /** The reply read aloud, when `speak` was asked for and the reply was short enough. Not stored: it is for this response only. */
  audio?: ReplyAudio | null;
}

/** A spoken turn: the same answer as a typed one, plus the words that were heard. */
export interface VoiceTurnData extends TurnData {
  transcript: string;
  /** Who transcribed it: "openai" (the Whisper API) or "local" (faster-whisper on the brain's own machine). */
  engine: string;
}

/** How one model route is (see BrainRouteState in lib/jarvisBrain.ts). */
export type RouteState = "ready" | "resting" | "failed" | "not configured";

export interface ModelRoute {
  role: "primary" | "fallback" | "last resort";
  provider: string;
  model: string;
  state: RouteState;
  lastError?: string;
}

/** What the sidebar's "Agents & tools" is built from (app/api/chat/tools/route.ts). */
export interface ToolsData {
  /** The brain answered at all. */
  reachable: boolean;
  /** It has a language model to answer with. */
  ready: boolean;
  /** The executive manager's model; null from a brain that does not say (or is unreachable). */
  manager?: {
    provider: string;
    model: string;
    onStandby: boolean;
    primary: { provider: string; model: string; state: RouteState; lastError?: string; backInSeconds?: number };
  } | null;
  /** The specialist agents: name, title, whether taking tasks, model routes in order, servers held. */
  agents?: { name: string; title: string; active: boolean; routes: ModelRoute[]; servers: string[] }[];
  /** Its tool servers: config name, connected or not, how many tools, and the agent that uses it (null = the manager). */
  servers: { name: string; connected: boolean; tools: number; agent?: string | null }[];
}

export interface TurnRequest {
  /** May be "" when a file is attached. */
  message: string;
  /** A file the person attached (checked first: attachmentProblem in attachments.ts). Sends the turn as multipart. */
  file?: File | null;
  /** Continue this session. */
  conversationId?: string;
  /** Start a new session instead of continuing the latest one. */
  newConversation?: boolean;
  /** Also return the reply as audio (when it is short enough). */
  speak?: boolean;
}

const READ_TIMEOUT_MS = 15_000;

const SIGNED_OUT_MESSAGE = "You're not signed in on this device. Use Sign in above, then try again.";
const FALLBACK_MESSAGE = "Jarvis couldn't answer just now.";

function kindFor(status: number): ApiFailureKind {
  if (status === 401) return "signed-out";
  if (status === 404) return "not-found";
  if (status === 429) return "rate-limited";
  if (status === 422) return "no-speech";
  if (status === 502) return "brain";
  return "server";
}

/** A read's own time limit ran out - unlike the page stopping a turn, that IS a failure to report. */
function isTimeout(signal: AbortSignal): boolean {
  return signal.reason instanceof DOMException && signal.reason.name === "TimeoutError";
}

async function request<T>(url: string, init: RequestInit = {}): Promise<ApiResult<T>> {
  let response: Response;
  try {
    response = await fetch(url, { credentials: "same-origin", ...init });
  } catch {
    if (init.signal?.aborted && !isTimeout(init.signal)) return { ok: false, kind: "cancelled", status: 0, message: "" };
    return { ok: false, kind: "network", status: 0, message: FALLBACK_MESSAGE };
  }

  const data: unknown = await response.json().catch(() => null);
  if (init.signal?.aborted && !isTimeout(init.signal)) return { ok: false, kind: "cancelled", status: 0, message: "" };
  if (response.ok) return { ok: true, data: data as T };
  return failure(response.status, data);
}

/** A refused or failed request as the controller reads it: `data` is the server's JSON body. */
function failure(status: number, data: unknown): ApiFailure {
  const kind = kindFor(status);
  const serverMessage = (data as { error?: unknown } | null)?.error;
  return {
    ok: false,
    kind,
    status,
    message:
      kind === "signed-out"
        ? SIGNED_OUT_MESSAGE
        : typeof serverMessage === "string" && serverMessage.trim()
          ? serverMessage
          : FALLBACK_MESSAGE,
  };
}

/**
 * A typed turn asked for as lines of JSON (app/api/chat/route.ts): each `progress` line goes to
 * `onProgress` as it arrives, the `final` line is the answer (the JSON answer's body), an `error`
 * line is a failure with its status. Everything refused before the turn started comes back as a
 * plain JSON response and is read like any other request; so is the answer of a server that does
 * not stream. Never throws: a stream that breaks off is a network failure, a stopped one "cancelled".
 */
async function streamedTurn(url: string, init: RequestInit, onProgress: (status: string) => void): Promise<ApiResult<TurnData>> {
  const signal = init.signal ?? undefined;
  const cancelled = (): ApiFailure | null =>
    signal?.aborted && !isTimeout(signal) ? { ok: false, kind: "cancelled", status: 0, message: "" } : null;
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/x-ndjson");

  let response: Response;
  try {
    response = await fetch(url, { credentials: "same-origin", ...init, headers });
  } catch {
    return cancelled() ?? { ok: false, kind: "network", status: 0, message: FALLBACK_MESSAGE };
  }
  const lines = (response.headers.get("content-type") ?? "").includes("application/x-ndjson");
  if (!response.ok || !lines || !response.body) {
    const data: unknown = await response.json().catch(() => null);
    const stopped = cancelled();
    if (stopped) return stopped;
    return response.ok ? { ok: true, data: data as TurnData } : failure(response.status, data);
  }

  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  try {
    for (;;) {
      const chunk = await reader.read();
      if (chunk.done) break;
      buffer += chunk.value;
      for (let end = buffer.indexOf("\n"); end !== -1; end = buffer.indexOf("\n")) {
        const line = buffer.slice(0, end).trim();
        buffer = buffer.slice(end + 1);
        if (!line) continue;
        let event: Record<string, unknown>;
        try {
          event = JSON.parse(line) as Record<string, unknown>;
        } catch {
          continue;
        }
        if (event.type === "progress" && typeof event.status === "string") {
          onProgress(event.status);
        } else if (event.type === "final") {
          return cancelled() ?? { ok: true, data: event as unknown as TurnData };
        } else if (event.type === "error") {
          return failure(typeof event.status === "number" ? event.status : 502, event);
        }
      }
    }
  } catch {
    // The read failed: stopped by the page, or the connection broke.
  } finally {
    reader.cancel().catch(() => {});
  }
  return cancelled() ?? { ok: false, kind: "network", status: 0, message: FALLBACK_MESSAGE };
}

export const chatApi = {
  latest: () =>
    request<ConversationData>("/api/chat", { cache: "no-store", signal: AbortSignal.timeout(READ_TIMEOUT_MS) }),

  conversation: (id: string) =>
    request<ConversationData>(`/api/chat?conversationId=${encodeURIComponent(id)}`, {
      cache: "no-store",
      signal: AbortSignal.timeout(READ_TIMEOUT_MS),
    }),

  sessions: () =>
    request<SessionsData>("/api/chat/sessions", { cache: "no-store", signal: AbortSignal.timeout(READ_TIMEOUT_MS) }),

  tools: () =>
    request<ToolsData>("/api/chat/tools", { cache: "no-store", signal: AbortSignal.timeout(READ_TIMEOUT_MS) }),

  deleteMessage: (id: string) =>
    request<{ deleted: string }>(`/api/chat/messages/${encodeURIComponent(id)}`, {
      method: "DELETE",
      signal: AbortSignal.timeout(READ_TIMEOUT_MS),
    }),

  // No client timeout: the brain may run a whole trip plan (tens of seconds) and the server enforces its own limit.
  // With a file the turn goes as multipart (no Content-Type set, so the browser adds the boundary); without, as JSON.
  // `signal` is "Stop generating": aborting it answers { ok: false, kind: "cancelled" }, and the server saves no reply
  // (and the brain stops working on it). `onProgress` hears each step Jarvis reports on the way (streamedTurn).
  send: ({ file, ...turn }: TurnRequest, signal?: AbortSignal, onProgress?: (status: string) => void) => {
    let init: RequestInit;
    if (!file) {
      init = { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(turn), signal };
    } else {
      const form = new FormData();
      form.append("message", turn.message);
      form.append("file", file, file.name);
      if (turn.conversationId) form.append("conversationId", turn.conversationId);
      else if (turn.newConversation) form.append("newConversation", "true");
      if (turn.speak) form.append("speak", "true");
      init = { method: "POST", body: form, signal };
    }
    return onProgress ? streamedTurn("/api/chat", init, onProgress) : request<TurnData>("/api/chat", init);
  },

  // The recording goes as multipart, and no Content-Type is set so the browser adds the boundary.
  // No client timeout, for the same reason as `send`: transcription and then the brain's whole turn.
  sendVoice: (audio: Blob, turn: Omit<TurnRequest, "message">, signal?: AbortSignal) => {
    const form = new FormData();
    form.append("audio", audio, `voice.${extensionFor(audio.type)}`);
    if (turn.conversationId) form.append("conversationId", turn.conversationId);
    else if (turn.newConversation) form.append("newConversation", "true");
    if (turn.speak) form.append("speak", "true");
    return request<VoiceTurnData>("/api/chat/voice", { method: "POST", body: form, signal });
  },
};

/** A file extension for a recording's content type, so the upload is named for what it is. */
function extensionFor(type: string): string {
  if (type.includes("mp4")) return "mp4";
  if (type.includes("ogg")) return "ogg";
  return "webm";
}
