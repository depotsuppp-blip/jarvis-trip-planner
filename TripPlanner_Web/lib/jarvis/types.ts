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
  /**
   * The saved message's id - or `local-N` for one made on this page: a notice, a sample, or the
   * person's typed message until its turn comes back and the saved copy replaces it.
   */
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
  /** The person's typed message whose turn is on its way: it cannot be deleted until the turn ends. */
  sendingId: string | null;
}

/**
 * What the thinking bubble says. `status` is what Jarvis is doing, in words - "Searching the
 * web", "Delegating to Head of Analytics" - as the brain streams each step of a typed turn
 * (ChatController.reportStatus); null means nothing has said yet (or the turn is spoken, which
 * does not stream), and the bubble says it is thinking.
 */
export interface PendingReply {
  status: string | null;
  /**
   * An action Jarvis has paused until the person says yes or no (a LINE message, starting a server,
   * writing a new tool): the Approval Card the bubble shows. Null when nothing is waiting.
   */
  approval?: PendingApproval | null;
}

/**
 * One action waiting for the person's Allow or Deny. Everything but `state` comes from the brain
 * (checked in lib/jarvisBrain.ts) and is shown as plain text, never as markup.
 */
export interface PendingApproval {
  /** Hands the answer to the right waiting action. */
  id: string;
  /** The tool's name ("send_line_notification"). */
  tool: string;
  /** The question: "Do you authorize ...?" */
  prompt: string;
  /** The tool's arguments, name and value. */
  details: { name: string; value: string }[];
  /** When the brain stops waiting and counts the silence as a no (milliseconds since the epoch, this page's clock). */
  expiresAt: number;
  /** "waiting": the buttons are live. "sending": an answer is on its way. */
  state: "waiting" | "sending";
  /** Set when the last answer could not be delivered - the buttons are live again. */
  error?: string;
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
