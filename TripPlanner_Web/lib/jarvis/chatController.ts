import { chatApi, type ApiFailure } from "./chatApi";
import {
  demoReply,
  initialAgents,
  MOCK_VOICE_QUESTION,
  MOCK_VOICE_REPLIES,
  parseDemoCommand,
  type DemoTool,
} from "./demo";
import { toChatMessage } from "./messageContent";
import { createStore } from "./store";
import {
  isFocalMode,
  type AgentState,
  type ChatMessage,
  type ChatState,
  type MessageContent,
  type OrbMode,
  type OrbState,
  type SessionSummary,
} from "./types";

/**
 * Everything the hub DOES, with no React in it: load and switch sessions, send
 * a turn, play the simulated voice turn, keep the orb's mode and the agents'
 * status. Components read two stores (`chat`, `orb`) through selectors and call
 * `actions`; they hold no chat logic and no timers of their own.
 *
 * Two stores, not one, on purpose: the orb mode changes a few times a turn and
 * the messages change whenever anything is said. Kept apart, a new message
 * re-renders the feed and never the orb, and the orb's mode never re-renders
 * the feed's rows - the animation keeps the main thread to itself.
 *
 * Staleness. Two counters keep an old answer from landing in the wrong place:
 * `turn` says which turn the orb belongs to (End, a new turn or a session
 * change bumps it, so a finished request does not drag the orb back to idle),
 * and `epoch` says which session the message list belongs to (a request that
 * finishes after the person has opened another session is not appended to it).
 */

// The simulated voice turn and the widget previews: how long each stage lasts.
const MOCK_LISTEN_MS = 2600;
const MOCK_THINK_MS = 2200;
const MOCK_SPEAK_MS = 3000;
const DEMO_THINK_MS = 900;

// The server titles a session with the start of its first message (app/api/chat/route.ts).
const TITLE_CHARS = 60;

export interface ChatActions {
  /** One typed turn. A message that is exactly a demo command (`/status`) is answered on this page instead. */
  send(text: string): Promise<void>;
  selectSession(id: string): Promise<void>;
  /** Opens an empty session; it is saved by its first message. */
  newSession(): void;
  refreshSessions(): Promise<void>;
  /** Loads the open session again after it failed to load. */
  retryConversation(): Promise<void>;
  /** The Talk button: starts the simulated voice turn, or ends the one in progress. */
  toggleVoice(): void;
  endVoice(): void;
  /** Test bench: jump the orb straight to a mode. */
  forceOrb(mode: OrbMode): void;
  /** Test bench: reply with a made-up widget. */
  previewWidget(tool: DemoTool): void;
}

function initialChat(): ChatState {
  return {
    access: "unknown",
    sessions: { status: "loading", items: [] },
    activeSessionId: null,
    conversation: "loading",
    messages: [],
    epoch: 0,
    agents: initialAgents(),
  };
}

function toSessionSummary(session: { id: string; title: string | null; updatedAt: string }): SessionSummary {
  const time = Date.parse(session.updatedAt);
  return { id: session.id, title: session.title, updatedAt: Number.isFinite(time) ? time : Date.now() };
}

/** The session list after `id` was just talked in: it moves to the top, and is added if it is new. */
function touchSession(items: SessionSummary[], id: string, title: string, at: number): SessionSummary[] {
  const existing = items.find((item) => item.id === id);
  const touched: SessionSummary = { id, title: existing?.title ?? title, updatedAt: at };
  return [touched, ...items.filter((item) => item.id !== id)];
}

export class ChatController {
  readonly chat = createStore<ChatState>(initialChat());
  readonly orb = createStore<OrbState>({ mode: "idle", voice: false });

  private timers: number[] = [];
  private nextId = 1;
  private turn = 0;
  private epoch = 0;
  private replyIndex = 0;
  private disposed = false;
  private bootstrapping: Promise<void> = Promise.resolve();

  /** Stable for the life of the controller: pass them around freely, nothing re-renders because of them. */
  readonly actions: ChatActions = {
    send: (text) => this.send(text),
    selectSession: (id) => this.selectSession(id),
    newSession: () => this.newSession(),
    refreshSessions: () => this.refreshSessions(),
    retryConversation: () => this.retryConversation(),
    toggleVoice: () => this.toggleVoice(),
    endVoice: () => this.cancelTurn(),
    forceOrb: (mode) => this.forceOrb(mode),
    previewWidget: (tool) => this.previewWidget(tool),
  };

  // ---------------------------------------------------------------- lifecycle

  /** Loads the saved sessions. Safe to call again after `dispose` (React StrictMode mounts twice in development). */
  start(): void {
    this.disposed = false;
    this.bootstrapping = this.bootstrap();
  }

  dispose(): void {
    this.disposed = true;
    this.clearTimers();
    this.turn++;
    this.epoch++;
  }

  // ----------------------------------------------------------------- sessions

  private async bootstrap(): Promise<void> {
    const epoch = ++this.epoch;
    this.chat.setState((state) => ({
      sessions: { status: "loading", items: state.sessions.items },
      conversation: "loading",
    }));

    const [latest, sessions] = await Promise.all([chatApi.latest(), chatApi.sessions()]);
    if (epoch !== this.epoch) return;

    if (!latest.ok) {
      this.chat.setState((state) => ({
        access: latest.kind === "signed-out" ? "signed-out" : state.access,
        sessions: { status: latest.kind === "signed-out" ? "ready" : "error", items: [] },
        conversation: latest.kind === "signed-out" ? "ready" : "error",
        epoch,
      }));
      return;
    }

    this.chat.setState((state) => ({
      access: "signed-in",
      sessions: {
        status: sessions.ok ? "ready" : "error",
        items: sessions.ok ? sessions.data.sessions.map(toSessionSummary) : [],
      },
      activeSessionId: latest.data.conversationId,
      conversation: "ready",
      // Anything typed while this was loading stays, after the saved history.
      messages: [...latest.data.messages.map(toChatMessage), ...state.messages],
      epoch,
    }));
  }

  async refreshSessions(): Promise<void> {
    this.chat.setState((state) => ({ sessions: { status: "loading", items: state.sessions.items } }));
    const result = await chatApi.sessions();
    if (this.disposed) return;

    if (!result.ok) {
      this.chat.setState((state) => ({
        access: result.kind === "signed-out" ? "signed-out" : state.access,
        sessions: { status: "error", items: state.sessions.items },
      }));
      return;
    }
    this.chat.setState({ sessions: { status: "ready", items: result.data.sessions.map(toSessionSummary) } });
  }

  async selectSession(id: string): Promise<void> {
    const state = this.chat.getState();
    if (id === state.activeSessionId && state.conversation !== "error") return;

    this.cancelTurn();
    const epoch = ++this.epoch;
    this.chat.setState({ activeSessionId: id, conversation: "loading", messages: [], epoch });

    const result = await chatApi.conversation(id);
    if (epoch !== this.epoch) return;

    if (!result.ok) {
      if (result.kind === "not-found") {
        // Deleted somewhere else: drop it and start fresh rather than show a dead session.
        this.chat.setState((current) => ({
          sessions: { status: current.sessions.status, items: current.sessions.items.filter((item) => item.id !== id) },
        }));
        this.newSession();
        return;
      }
      this.chat.setState((current) => ({
        access: result.kind === "signed-out" ? "signed-out" : current.access,
        conversation: result.kind === "signed-out" ? "ready" : "error",
      }));
      return;
    }

    this.chat.setState({ conversation: "ready", messages: result.data.messages.map(toChatMessage), epoch });
  }

  newSession(): void {
    this.cancelTurn();
    const epoch = ++this.epoch;
    this.chat.setState({ activeSessionId: null, conversation: "ready", messages: [], epoch });
  }

  async retryConversation(): Promise<void> {
    const { activeSessionId } = this.chat.getState();
    if (activeSessionId) return this.selectSession(activeSessionId);
    this.bootstrapping = this.bootstrap();
    return this.bootstrapping;
  }

  // -------------------------------------------------------------------- turns

  async send(raw: string): Promise<void> {
    const text = raw.trim();
    if (!text || isFocalMode(this.orb.getState().mode)) return;

    this.clearTimers();
    const mine = ++this.turn;
    const demo = parseDemoCommand(text);

    this.append({ role: "user", content: { text, widgets: [] }, origin: demo ? "demo" : "server" });
    this.orb.setState({ mode: "thinking", voice: false });

    if (demo) {
      this.later(DEMO_THINK_MS, () => {
        this.append({ role: "jarvis", content: demoReply(demo, Date.now()), origin: "demo" });
        this.settle(mine);
      });
      return;
    }

    this.setAgent("brain", "working");
    await this.bootstrapping;
    if (this.disposed) return;

    const epoch = this.epoch;
    const { activeSessionId } = this.chat.getState();
    const result = await chatApi.send({
      message: text,
      ...(activeSessionId ? { conversationId: activeSessionId } : { newConversation: true }),
    });
    if (this.disposed) return;

    if (result.ok) {
      this.setAgent("brain", "ready");
      const reply = toChatMessage(result.data.reply);
      const { conversationId } = result.data;
      if (epoch === this.epoch) {
        this.chat.setState((state) => ({
          access: "signed-in",
          activeSessionId: conversationId,
          messages: [...state.messages, reply],
          sessions: {
            status: "ready",
            items: touchSession(state.sessions.items, conversationId, text.slice(0, TITLE_CHARS), reply.createdAt),
          },
        }));
      } else {
        // The person opened another session meanwhile: the reply is saved there, this page just refreshes the list.
        void this.refreshSessions();
      }
    } else {
      this.reportFailure(result, epoch);
    }

    this.settle(mine);
  }

  private reportFailure(failure: ApiFailure, epoch: number): void {
    const offline = failure.kind === "brain" || failure.kind === "network";
    this.setAgent("brain", offline ? "offline" : "idle", offline ? failure.message : undefined);
    if (failure.kind === "signed-out") this.chat.setState({ access: "signed-out" });
    if (epoch !== this.epoch) return;
    this.append({ role: "jarvis", content: { text: failure.message, widgets: [] }, origin: "server", tone: "error" });
  }

  // --------------------------------------------------------- simulated voice

  toggleVoice(): void {
    if (isFocalMode(this.orb.getState().mode)) {
      this.cancelTurn();
      return;
    }

    this.clearTimers();
    this.turn++;
    this.orb.setState({ voice: true, mode: "listening" });
    this.later(MOCK_LISTEN_MS, () => {
      this.append({ role: "user", content: { text: MOCK_VOICE_QUESTION, widgets: [] }, origin: "demo" });
      this.runMockResponse();
    });
  }

  // thinking -> speaking -> idle.
  private runMockResponse(): void {
    this.orb.setState({ mode: "thinking" });
    this.later(MOCK_THINK_MS, () => {
      const text = MOCK_VOICE_REPLIES[this.replyIndex++ % MOCK_VOICE_REPLIES.length];
      this.append({ role: "jarvis", content: { text, widgets: [] }, origin: "demo" });
      this.orb.setState({ mode: "speaking" });
      this.later(MOCK_SPEAK_MS, () => this.orb.setState({ mode: "idle", voice: false }));
    });
  }

  // -------------------------------------------------------------- test bench

  forceOrb(mode: OrbMode): void {
    this.clearTimers();
    this.turn++;
    this.orb.setState({ mode, voice: mode !== "idle" });
  }

  previewWidget(tool: DemoTool): void {
    this.clearTimers();
    const mine = ++this.turn;
    this.orb.setState({ mode: "thinking", voice: false });
    this.later(DEMO_THINK_MS, () => {
      this.append({ role: "jarvis", content: demoReply(tool, Date.now()), origin: "demo" });
      this.settle(mine);
    });
  }

  // ------------------------------------------------------------------ helpers

  /** Stops whatever the orb is part-way through and sends it home. */
  private cancelTurn(): void {
    this.clearTimers();
    this.turn++;
    this.orb.setState({ mode: "idle", voice: false });
  }

  /** Back to idle - but only if nobody pressed End or began another turn meanwhile. */
  private settle(turn: number): void {
    if (this.turn === turn) this.orb.setState({ mode: "idle", voice: false });
  }

  private append(message: Pick<ChatMessage, "role" | "origin"> & { content: MessageContent; tone?: "error" }): void {
    const next: ChatMessage = {
      id: `local-${this.nextId++}`,
      createdAt: Date.now(),
      ...message,
    };
    this.chat.setState((state) => ({ messages: [...state.messages, next] }));
  }

  private setAgent(id: string, state: AgentState, detail?: string): void {
    this.chat.setState((current) => ({
      agents: current.agents.map((agent) => (agent.id === id ? { ...agent, state, detail } : agent)),
    }));
  }

  private later(ms: number, fn: () => void): void {
    this.timers.push(window.setTimeout(fn, ms));
  }

  private clearTimers(): void {
    for (const id of this.timers) window.clearTimeout(id);
    this.timers = [];
  }
}
