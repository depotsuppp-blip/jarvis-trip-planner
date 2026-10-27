import type { Attachment } from "@/lib/jarvis/attachments";
import { parseReplyAudio, type ReplyAudio } from "@/lib/jarvis/replyAudio";

/**
 * The one place the chat route asks "what does Jarvis say?": a POST to the
 * Python brain. The brain's side of the contract:
 *
 *   POST {JARVIS_BRAIN_URL}   default http://127.0.0.1:8000/chat
 *   body  { prompt: string,
 *           history: [{ role: "user" | "jarvis", content, attachments? }],
 *           user_id?: string, app_url?: string, speak?: true,
 *           attachments?: [{ path, name, mime, size }] }
 *   reply { reply: string | object, audio?: { mime, base64 } | null }
 *
 *   attachments = files the person attached to this message, already saved under
 *           the Jarvis project's uploads/ folder (lib/uploads.ts); `path` is
 *           relative to that project ("uploads/3f...-report.pdf"). The brain adds a
 *           line to the prompt that says where the file is and which tool reads it,
 *           and the model reads it from there. `prompt` may be "" when a file came
 *           with no words. A user turn in `history` carries its attachments the same
 *           way, so a follow-up question can still find the file.
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
 *
 * The status door (getJarvisStatus) is what the hub's sidebar shows as "Agents & tools":
 *
 *   GET {JARVIS_HEALTH_URL}   default: JARVIS_BRAIN_URL with its /chat swapped for /health
 *   reply { ok: boolean, mcp_servers: [{ name, connected, tools: string[], error }],
 *           manager: { provider, model, on_standby, primary: { provider, model, state, last_error?, back_in? } },
 *           agents: [{ name, title, tool, active, routes: [{ role, provider, model, state, why?, last_error? }],
 *                      tools: string[] }], ... }
 *
 *   ok = the brain has a language model to answer with. mcp_servers = its tool
 *   servers (web search, memory, ...) as they are right now; `error` is for the
 *   logs, never the page. manager = the brain's own model (Claude, or the Gemini
 *   standby and why); agents = the specialist agents it delegates to (Python
 *   agents/), each with its routes in order and the MCP tools it holds. A brain
 *   from before the agents sends neither, and the sidebar then shows none.
 *   Free text (`why`, `error`) never leaves this file: it can hold local paths.
 */

export type ChatTurn = { role: "user" | "jarvis"; content: string; attachments?: Attachment[] };

/** Who is asking, where this app lives, and whether to voice the reply - see the contract above. */
export type ChatContext = { userId?: string; appUrl?: string; speak?: boolean };

/** A typed turn's extras: the files attached to it (see `attachments` in the contract above). */
export type AskContext = ChatContext & { attachments?: Attachment[] };

/** What the brain said, and its voice if it was asked for one and could give it. */
export interface BrainAnswer {
  reply: string;
  audio: ReplyAudio | null;
}

const BRAIN_URL = process.env.JARVIS_BRAIN_URL ?? "http://127.0.0.1:8000/chat";
const VOICE_URL = process.env.JARVIS_VOICE_URL ?? BRAIN_URL.replace(/\/chat\/?$/, "/voice");
const HEALTH_URL = process.env.JARVIS_HEALTH_URL ?? BRAIN_URL.replace(/\/chat\/?$/, "/health");
// The brain answers /health at once, except while it is still starting (it connects its
// tool servers first, up to ~15 s). A sidebar that waits longer than this just says "not checked".
const HEALTH_TIMEOUT_MS = 8_000;
// Tool calls and the reasoning tier can take a while - and "plan a trip" is a
// tool call that itself runs a whole generation (two LLM calls, Places, Routes:
// ~15-30s) between the model's turns. Past this the turn fails.
const BRAIN_TIMEOUT_MS = 120_000;

export class JarvisBrainError extends Error {}

export async function askJarvis(
  message: string,
  history: ChatTurn[],
  context: AskContext = {}
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
        ...(context.attachments?.length ? { attachments: context.attachments } : {}),
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

export interface BrainStatus {
  /** The brain has a language model to answer with. */
  ready: boolean;
  servers: BrainToolServer[];
  /** Null from a brain that does not report it. */
  manager: BrainManager | null;
  /** Empty with the team off, or from a brain without one. */
  agents: BrainAgent[];
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
  return { ready: body.ok === true, servers, manager: readManager(body.manager), agents };
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
