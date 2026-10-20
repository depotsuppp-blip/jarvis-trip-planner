import { chatApi, type ApiFailure } from "./chatApi";
import { demoReply, initialAgents, parseDemoCommand, type DemoTool } from "./demo";
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
import {
  startRecording,
  VoiceRecorderError,
  type ActiveRecording,
  type RecordedAudio,
  type VoiceRecorderFailure,
} from "./voiceRecorder";

/**
 * Everything the hub DOES, with no React in it: load and switch sessions, send
 * a typed turn, record and send a spoken one, keep the orb's mode and the
 * agents' status. Components read two stores (`chat`, `orb`) through selectors
 * and call `actions`; they hold no chat logic and no timers of their own.
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

// How long the widget previews "think" for.
const DEMO_THINK_MS = 900;

// A spoken turn: a recording ends by itself at the maximum (and is sent), and one
// shorter than the minimum is a mis-tap, not a message.
const MAX_RECORDING_MS = 60_000;
const MIN_RECORDING_MS = 600;
// What the Voice row says when nothing is happening (demo.ts's initialAgents starts it the same way).
const VOICE_IDLE_DETAIL = "Tap Talk to speak";

// The server titles a session with the start of its first message (app/api/chat/route.ts).
const TITLE_CHARS = 60;

const MICROPHONE_PROBLEM: Record<VoiceRecorderFailure, { message: string; detail: string }> = {
  unsupported: {
    message: "This browser can't record audio here. Voice needs a recent browser, on https:// or localhost.",
    detail: "Browser can't record",
  },
  denied: {
    message: "Microphone access is blocked. Allow it for this site in your browser's address bar, then tap Talk again.",
    detail: "Microphone blocked",
  },
  "no-device": { message: "No microphone was found. Plug one in, then tap Talk again.", detail: "No microphone found" },
  busy: { message: "The microphone is busy - another app may be using it.", detail: "Microphone busy" },
  failed: { message: "Couldn't start the microphone. Please try again.", detail: "Couldn't start" },
};

export interface ChatActions {
  /** One typed turn. A message that is exactly a demo command (`/status`) is answered on this page instead. */
  send(text: string): Promise<void>;
  selectSession(id: string): Promise<void>;
  /** Opens an empty session; it is saved by its first message. */
  newSession(): void;
  refreshSessions(): Promise<void>;
  /** Loads the open session again after it failed to load. */
  retryConversation(): Promise<void>;
  /**
   * The Talk button, a toggle: idle -> asks for the microphone and starts recording;
   * recording -> stops and sends what was said; waiting for the answer -> gives the turn up.
   */
  toggleVoice(): void;
  /** Stops recording and sends it (what Talk does while recording). */
  finishVoice(): Promise<void>;
  /** Throws a recording away unsent, or gives up on a turn that is waiting for its answer. */
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
  /** The recording in progress, if any. */
  private recorder: ActiveRecording | null = null;
  /** True while the browser is asking for the microphone, before any audio flows. */
  private starting = false;
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
    finishVoice: () => this.finishVoice(),
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
    this.abandonRecording();
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

  /** `agent` is the one the failure is charged to: the brain for a typed turn, Voice for a spoken one. */
  private reportFailure(failure: ApiFailure, epoch: number, agent: "brain" | "voice" = "brain"): void {
    const offline = failure.kind === "brain" || failure.kind === "network";
    this.setAgent(agent, offline ? "offline" : "idle", offline ? failure.message : undefined);
    if (failure.kind === "signed-out") this.chat.setState({ access: "signed-out" });
    if (epoch !== this.epoch) return;
    this.notify(failure.message);
  }

  // -------------------------------------------------------------------- voice

  toggleVoice(): void {
    const { mode } = this.orb.getState();
    if (mode === "listening") {
      void this.finishVoice();
    } else if (mode === "thinking") {
      this.cancelTurn();
    } else {
      void this.startVoice();
    }
  }

  /** Asks for the microphone and, once audio flows, listens until told (or MAX_RECORDING_MS) to stop. */
  private async startVoice(): Promise<void> {
    if (this.starting || this.recorder) return;

    this.clearTimers();
    const mine = ++this.turn;
    this.starting = true;
    // Pressed from the click on, so the button answers while the browser's permission prompt is up.
    this.orb.setState({ voice: true });

    let recording: ActiveRecording;
    try {
      recording = await startRecording({
        // The microphone went away mid-sentence: send what there is.
        onEnded: () => {
          if (this.turn === mine) void this.finishVoice();
        },
      });
    } catch (error) {
      this.starting = false;
      if (this.disposed || this.turn !== mine) return;
      const problem = MICROPHONE_PROBLEM[error instanceof VoiceRecorderError ? error.kind : "failed"];
      const missing = error instanceof VoiceRecorderError && ["unsupported", "denied", "no-device"].includes(error.kind);
      this.setAgent("voice", missing ? "unavailable" : "offline", problem.detail);
      this.notify(problem.message);
      this.orb.setState({ mode: "idle", voice: false });
      return;
    }

    this.starting = false;
    if (this.disposed || this.turn !== mine) {
      // The person ended the turn, or opened another session, while the browser was asking.
      recording.discard();
      return;
    }

    this.recorder = recording;
    this.orb.setState({ mode: "listening", voice: true });
    this.setAgent("voice", "working", "Recording");
    this.later(MAX_RECORDING_MS, () => void this.finishVoice());
  }

  /** Stops recording and sends it: transcribed and answered on the server, then shown like a typed turn. */
  async finishVoice(): Promise<void> {
    const recording = this.recorder;
    if (!recording) {
      // Nothing is recording (the test bench forced the orb into this mode): just go home.
      this.cancelTurn();
      return;
    }
    this.recorder = null;
    this.clearTimers();
    const mine = ++this.turn;
    this.orb.setState({ mode: "thinking", voice: true });
    this.setAgent("voice", "working", "Transcribing");

    let audio: RecordedAudio;
    try {
      audio = await recording.stop();
    } catch {
      if (this.turn === mine) {
        this.setAgent("voice", "offline", "Recording failed");
        this.notify("Couldn't finish that recording. Please try again.");
        this.settle(mine);
      }
      return;
    }
    if (this.disposed || this.turn !== mine) return;

    if (audio.blob.size === 0 || audio.durationMs < MIN_RECORDING_MS) {
      this.setAgent("voice", "idle", "Last recording was too short");
      this.notify("That recording was too short. Tap Talk, say it, then tap Send.");
      this.settle(mine);
      return;
    }

    await this.bootstrapping;
    if (this.disposed) return;

    const epoch = this.epoch;
    const { activeSessionId } = this.chat.getState();
    const result = await chatApi.sendVoice(
      audio.blob,
      activeSessionId ? { conversationId: activeSessionId } : { newConversation: true }
    );
    if (this.disposed) return;

    if (result.ok) {
      // "mock" means the brain has no speech-to-text key: what it "heard" is a stand-in sentence, and the page says so.
      const mock = result.data.engine === "mock";
      this.setAgent("voice", "ready", mock ? "Mock transcription - no speech-to-text key" : "Heard you");
      this.setAgent("brain", "ready");
      const heard = toChatMessage(result.data.userMessage);
      const reply = toChatMessage(result.data.reply);
      const { conversationId } = result.data;
      if (epoch === this.epoch) {
        this.chat.setState((state) => ({
          access: "signed-in",
          activeSessionId: conversationId,
          messages: [...state.messages, heard, reply],
          sessions: {
            status: "ready",
            items: touchSession(
              state.sessions.items,
              conversationId,
              result.data.transcript.slice(0, TITLE_CHARS),
              reply.createdAt
            ),
          },
        }));
      } else {
        // The person opened another session meanwhile: the turn is saved there, this page just refreshes the list.
        void this.refreshSessions();
      }
    } else {
      this.reportFailure(result, epoch, "voice");
    }

    this.settle(mine);
  }

  // -------------------------------------------------------------- test bench

  forceOrb(mode: OrbMode): void {
    this.abandonRecording();
    this.resetVoiceAgent();
    this.clearTimers();
    this.turn++;
    this.orb.setState({ mode, voice: mode !== "idle" });
  }

  previewWidget(tool: DemoTool): void {
    this.abandonRecording();
    this.resetVoiceAgent();
    this.clearTimers();
    const mine = ++this.turn;
    this.orb.setState({ mode: "thinking", voice: false });
    this.later(DEMO_THINK_MS, () => {
      this.append({ role: "jarvis", content: demoReply(tool, Date.now()), origin: "demo" });
      this.settle(mine);
    });
  }

  // ------------------------------------------------------------------ helpers

  /** Stops whatever the orb is part-way through and sends it home. A recording in progress is thrown away. */
  private cancelTurn(): void {
    this.abandonRecording();
    this.clearTimers();
    this.turn++;
    this.orb.setState({ mode: "idle", voice: false });
    this.resetVoiceAgent();
  }

  /** A cancelled recording or upload must not leave the sidebar saying Voice is working. */
  private resetVoiceAgent(): void {
    if (this.chat.getState().agents.find((agent) => agent.id === "voice")?.state === "working") {
      this.setAgent("voice", "idle", VOICE_IDLE_DETAIL);
    }
  }

  /** Ends a recording unsent and lets the microphone go. */
  private abandonRecording(): void {
    this.recorder?.discard();
    this.recorder = null;
  }

  /** A line from Jarvis that is about the page, not a reply: a problem the person can act on. */
  private notify(text: string): void {
    this.append({ role: "jarvis", content: { text, widgets: [] }, origin: "server", tone: "error" });
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
