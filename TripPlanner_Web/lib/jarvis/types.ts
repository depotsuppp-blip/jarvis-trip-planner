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
  content: MessageContent;
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
 * run worked), offline "Offline", unavailable "Not connected".
 */
export type AgentState = "unknown" | "idle" | "working" | "ready" | "offline" | "unavailable";

export interface AgentStatus {
  id: string;
  name: string;
  kind: "brain" | "tool" | "voice";
  state: AgentState;
  /** One short line shown under the name: why it is offline, what it needs. */
  detail?: string;
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

/** Centre stage only while Jarvis is being listened to or is thinking; idle and speaking send the orb home. */
export function isFocalMode(mode: OrbMode): boolean {
  return mode === "listening" || mode === "thinking";
}
