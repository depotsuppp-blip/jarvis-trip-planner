/**
 * The vocabulary of the Jarvis hub (`/jarvis`): what a message is, what a
 * session is, what the orb can be doing. Plain data only - no React, no
 * network - so the state layer (`chatController.ts`), the parser
 * (`messageContent.ts`) and the components all speak the same types.
 */

/** What the orb (and so the whole hub) is doing right now. */
export type OrbMode = "idle" | "listening" | "thinking" | "speaking";

export type MessageRole = "user" | "jarvis";

/**
 * One widget a reply asks the page to draw: a tool's structured result
 * (`{ tool: "calendar", data: {...} }`) in place of, or beside, plain text.
 *
 * `tool` is already canonical here (lower-case snake_case, see
 * `normalizeToolName`); `data` is whatever the tool sent and is NOT trusted:
 * the widget registry validates it before a component sees it.
 */
export interface WidgetPayload {
  tool: string;
  data: unknown;
  /** Overrides the widget's own title ("Tokyo, 5 days"). */
  title?: string;
  /** True when the data is a demonstration, not a real tool result: the widget says so. */
  sample?: boolean;
}

/** A reply, parsed: optional text, then zero or more widgets. Never both empty. */
export interface MessageContent {
  text: string | null;
  widgets: WidgetPayload[];
}

/**
 * Where a message came from. "demo" messages are made on this page (the widget
 * previews): nothing was sent to Jarvis, nothing was saved, and the page labels
 * them as samples.
 */
export type MessageOrigin = "server" | "demo";

export interface ChatMessage {
  id: string;
  role: MessageRole;
  /** For the person's own message, `text` is null when they sent only a file. */
  content: MessageContent;
  /** Files the person sent with this message (their names and sizes; see attachments.ts). */
  attachments?: { name: string; size: number }[];
  /** Milliseconds since the epoch. */
  createdAt: number;
  origin: MessageOrigin;
  /** "error" draws the text as a problem (the brain failed, you are signed out). */
  tone?: "error";
}

export interface SessionSummary {
  id: string;
  title: string | null;
  /** Milliseconds since the epoch. */
  updatedAt: number;
}

/**
 * How an agent or tool is doing, in the words the sidebar uses:
 * unknown "Not checked", idle "Idle", working "Working", ready "Ready" (its last
 * run worked), degraded "On fallback" (answering, but not on its primary model),
 * offline "Offline", unavailable "Not connected".
 */
export type AgentState = "unknown" | "idle" | "working" | "ready" | "degraded" | "offline" | "unavailable";

/**
 * Which group of the sidebar a row is in: "brain" is the executive manager (Jarvis
 * itself), "agent" a specialist it delegates to, "tool" and "voice" the tools and
 * services under them.
 */
export type AgentKind = "brain" | "agent" | "tool" | "voice";

export interface AgentStatus {
  id: string;
  name: string;
  kind: AgentKind;
  state: AgentState;
  /** One short line shown under the name: what it does, why it is offline, what it needs. */
  detail?: string;
  /** The model it is on right now, and why when that is not its primary ("Gemini Flash-Lite · Gemini Pro out of quota"). */
  model?: string;
  /** Its model routes in order, for the row's hover text ("Gemini Pro → gpt-oss-120b on Groq → Gemini Flash-Lite"). */
  routes?: string;
}

export type LoadState = "loading" | "ready" | "error";

/** Whether the server knows who is asking: decided by the first answer it gives. */
export type Access = "unknown" | "signed-in" | "signed-out";

export interface ChatState {
  access: Access;
  sessions: { status: LoadState; items: SessionSummary[] };
  /** The open session, or null for a new one that has no server record until its first message. */
  activeSessionId: string | null;
  /** Whether the open session's messages have arrived. */
  conversation: LoadState;
  messages: ChatMessage[];
  /** Bumped whenever the whole message list is swapped (a session change), so the feed jumps instead of gliding. */
  epoch: number;
  agents: AgentStatus[];
  /** A reply is on its way: the feed ends in Jarvis's "thinking" bubble (PendingReply). Null when none is. */
  pending: PendingReply | null;
}

/**
 * What the thinking bubble says. `status` is what Jarvis is doing, in words - "Searching the
 * web", "Generating an image" - from whatever can tell (ChatController.reportStatus); null
 * means nothing has, and the bubble says it is thinking. Today nothing does: the brain
 * answers each turn in one response, with no progress on the way, so this is the slot a
 * streamed status will fill.
 */
export interface PendingReply {
  status: string | null;
}

export interface OrbState {
  mode: OrbMode;
  /** True from the moment Talk is pressed until the spoken turn ends (asking for the microphone, recording, waiting for the answer). */
  voice: boolean;
}

/** Whether Jarvis's short replies are read aloud (the person's choice, kept on this device). */
export interface SpeechState {
  enabled: boolean;
}

/** A turn is in progress: Jarvis is being listened to or is working on an answer (the dock offers Cancel; idle and speaking do not). */
export function isFocalMode(mode: OrbMode): boolean {
  return mode === "listening" || mode === "thinking";
}
