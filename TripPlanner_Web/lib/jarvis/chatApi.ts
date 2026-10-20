import type { ServerMessageLike } from "./messageContent";

/**
 * The hub's only door to the server: four calls on `/api/chat`, each answering
 * `{ ok: true, data }` or `{ ok: false, kind, message }` and never throwing, so
 * the state layer (`chatController.ts`) reads results instead of catching.
 *
 *   GET  /api/chat                          the latest session's messages
 *   GET  /api/chat?conversationId=<id>      one session's messages
 *   GET  /api/chat/sessions                 the signed-in person's sessions, newest first
 *   POST /api/chat { message, conversationId?, newConversation? }   one turn
 *
 * `message` / `content` come back as stored: the controller parses a reply into
 * text and widgets (`messageContent.ts`), this file only moves bytes.
 */

export type ApiFailureKind = "signed-out" | "not-found" | "rate-limited" | "brain" | "server" | "network";

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
}

export interface TurnRequest {
  message: string;
  /** Continue this session. */
  conversationId?: string;
  /** Start a new session instead of continuing the latest one. */
  newConversation?: boolean;
}

const READ_TIMEOUT_MS = 15_000;

const SIGNED_OUT_MESSAGE = "You're not signed in on this device. Use Sign in above, then try again.";
const FALLBACK_MESSAGE = "Jarvis couldn't answer just now.";

function kindFor(status: number): ApiFailureKind {
  if (status === 401) return "signed-out";
  if (status === 404) return "not-found";
  if (status === 429) return "rate-limited";
  if (status === 502) return "brain";
  return "server";
}

async function request<T>(url: string, init: RequestInit = {}): Promise<ApiResult<T>> {
  let response: Response;
  try {
    response = await fetch(url, { credentials: "same-origin", ...init });
  } catch {
    return { ok: false, kind: "network", status: 0, message: FALLBACK_MESSAGE };
  }

  const data: unknown = await response.json().catch(() => null);
  if (response.ok) return { ok: true, data: data as T };

  const kind = kindFor(response.status);
  const serverMessage = (data as { error?: unknown } | null)?.error;
  return {
    ok: false,
    kind,
    status: response.status,
    message:
      kind === "signed-out"
        ? SIGNED_OUT_MESSAGE
        : typeof serverMessage === "string" && serverMessage.trim()
          ? serverMessage
          : FALLBACK_MESSAGE,
  };
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

  // No client timeout: the brain may run a whole trip plan (tens of seconds) and the server enforces its own limit.
  send: (turn: TurnRequest) =>
    request<TurnData>("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(turn),
    }),
};
