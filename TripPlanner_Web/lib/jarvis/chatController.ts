import { chatApi, type ApiFailure, type ApprovalRequest } from "./chatApi";
import { demoReply, initialAgents, parseDemoCommand, type DemoTool } from "./demo";
import { toChatMessage } from "./messageContent";
import { canSpeak, type ReplyAudio } from "./replyAudio";
import { createSpeechPlayer } from "./speechPlayer";
import { createStore } from "./store";
import { withStatus } from "./tools";
import {
  isFocalMode,
  type AgentState,
  type ChatMessage,
  type ChatState,
  type MessageContent,
  type OrbMode,
  type OrbState,
  type SessionSummary,
  type SpeechState,
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
 * agents' status (the tool rows come from the brain - see tools.ts). Components read two stores (`chat`, `orb`) through selectors
 * and call `actions`; they hold no chat logic and no timers of their own.
 *
 * Two stores, not one, on purpose: the orb mode changes a few times a turn and
 * the messages change whenever anything is said. Kept apart, a new message
 * re-renders the feed and never the orb, and the orb's mode never re-renders
 * the feed's rows - the animation keeps the main thread to itself.
 *
 * Spoken replies. While voice replies are on (`speech`, the person's choice, kept in
 * localStorage) every turn asks the server for audio (`speak: true`), and a reply
 * that comes back with audio is played - but only if its own text is at most
 * VOICE_REPLY_MAX_CHARS (replyAudio.ts), whatever the server decided. The orb shows
 * "speaking" while the clip plays. A new turn, a session change, the Talk button or
 * switching voice replies off cuts the clip off.
 *
 * Staleness. Two counters keep an old answer from landing in the wrong place:
 * `turn` says which turn the orb belongs to (Cancel, a new turn or a session
 * change bumps it, so a finished request does not drag the orb back to idle),
 * and `epoch` says which session the message list belongs to (a request that
 * finishes after the person has opened another session is not appended to it).
 *
 * Message actions. Edit hands a message's words to the composer (onDraftRequest): the
 * draft stays the composer's own state, so typing still re-renders nothing else. Delete
 * takes the message off the page at once and deletes the saved copy (DELETE
 * /api/chat/messages/<id>), so it is gone from the brain's history too; if the server
 * refuses, it comes back where it was, with a notice. A typed message is `local-N` until
 * its turn comes back with the saved copy, which replaces it; one whose turn never came
 * back (stopped, or the brain failed) is looked up by its words when it is deleted.
 */

// How long the widget previews "think" for.
const DEMO_THINK_MS = 900;

// A spoken turn: a recording ends by itself at the maximum (and is sent), and one
// shorter than the minimum is a mis-tap, not a message.
const MAX_RECORDING_MS = 60_000;
const MIN_RECORDING_MS = 600;
// What the Voice row says when nothing is happening (demo.ts's initialAgents starts it the same way).
const VOICE_IDLE_DETAIL = "Tap Talk to speak";

// How often the sidebar asks which tools the brain has connected (GET /api/chat/tools), while the tab is visible.
const TOOLS_REFRESH_MS = 60_000;

// The server titles a session with the start of its first message (app/api/chat/route.ts).
const TITLE_CHARS = 60;

// The person's voice-replies choice. "off" is the only value written; anything else (or no storage) means on.
const SPEECH_STORAGE_KEY = "jarvis:voice-replies";
// A spoken reply that has not started playing by now is given up on (see speakReply).
const SPEECH_START_MS = 4000;

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
  /**
   * One typed turn, optionally with a file (already checked with attachmentProblem; the text may then be empty).
   * A message that is exactly a demo command (`/status`) with no file is answered on this page instead.
   */
  send(text: string, file?: File | null): Promise<void>;
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
  /**
   * Throws a recording away unsent, or stops a turn that is waiting for its answer - spoken or
   * typed ("Stop generating", Cancel, Escape). A stopped turn's request is aborted: its answer is
   * neither shown nor saved (the server drops it too), and the person's own message stays.
   */
  endVoice(): void;
  /** Turns voice replies on or off. Turning them off also cuts off a reply that is being read out. */
  toggleSpeech(): void;
  /** Test bench: jump the orb straight to a mode. */
  forceOrb(mode: OrbMode): void;
  /** Test bench: reply with a made-up widget. */
  previewWidget(tool: DemoTool): void;
  /** Puts the words of one of the person's messages in the message field, to change and send again. */
  editMessage(id: string): void;
  /**
   * Deletes one message - from the page at once, and its saved copy on the server, so Jarvis
   * no longer sees it either. Not the message whose answer is still on its way (`sendingId`).
   */
  deleteMessage(id: string): Promise<void>;
  /**
   * The person's answer to an Approval Card: Allow runs the action Jarvis paused, Deny reports it
   * as declined. The turn carries on either way; a card that is no longer waiting just closes.
   */
  decideApproval(id: string, decision: "allow" | "deny"): Promise<void>;
}

/** Where to find the saved copy of a typed message whose turn never came back with it. */
type SavedCopy = { found: string } | { none: true } | { failed: ApiFailure };

/** A reply that may be read aloud: the audio the server sent (if any) and the reply's own words. */
interface SpokenReply {
  audio: ReplyAudio | null | undefined;
  text: string | null;
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
    pending: null,
    sendingId: null,
  };
}

const LOCAL_ID = "local-";

/** Made on this page and never saved as such (a notice, a sample, a typed message not yet confirmed). */
function isLocal(message: ChatMessage): boolean {
  return message.id.startsWith(LOCAL_ID);
}

/** The same words and files: how a typed message is matched with its saved copy. */
function sameMessage(a: ChatMessage, b: ChatMessage): boolean {
  const names = (message: ChatMessage) => (message.attachments ?? []).map((file) => file.name).join("\n");
  return a.role === b.role && (a.content.text ?? "") === (b.content.text ?? "") && names(a) === names(b);
}

/** `messages` with `message` put back in time order (it was deleted, and the server said no). */
function restore(messages: ChatMessage[], message: ChatMessage): ChatMessage[] {
  if (messages.some((item) => item.id === message.id)) return messages;
  const at = messages.findIndex((item) => item.createdAt > message.createdAt);
  return at < 0 ? [...messages, message] : [...messages.slice(0, at), message, ...messages.slice(at)];
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
  /** On until the person turns it off; the saved choice is read in `start` (never on the server). */
  readonly speech = createStore<SpeechState>({ enabled: true });

  private readonly player = createSpeechPlayer();
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
  /** The tool-status refresh (see refreshTools); its own timer, because clearTimers runs on every turn. */
  private toolsTimer: number | null = null;
  /** The current turn's request to the server: what "Stop generating" aborts. */
  private request: AbortController | null = null;
  /** The composer's ear for Edit (onDraftRequest). */
  private readonly draftListeners = new Set<(text: string) => void>();

  /** Stable for the life of the controller: pass them around freely, nothing re-renders because of them. */
  readonly actions: ChatActions = {
    send: (text, file) => this.send(text, file),
    selectSession: (id) => this.selectSession(id),
    newSession: () => this.newSession(),
    refreshSessions: () => this.refreshSessions(),
    retryConversation: () => this.retryConversation(),
    toggleVoice: () => this.toggleVoice(),
    finishVoice: () => this.finishVoice(),
    endVoice: () => this.cancelTurn(),
    toggleSpeech: () => this.toggleSpeech(),
    forceOrb: (mode) => this.forceOrb(mode),
    previewWidget: (tool) => this.previewWidget(tool),
    editMessage: (id) => this.editMessage(id),
    deleteMessage: (id) => this.deleteMessage(id),
    decideApproval: (id, decision) => this.decideApproval(id, decision),
  };

  /**
   * The composer listens here for Edit: `listener` gets the words to put in the message field.
   * Returns the way to stop listening. Stable, like `actions`.
   */
  readonly onDraftRequest = (listener: (text: string) => void): (() => void) => {
    this.draftListeners.add(listener);
    return () => {
      this.draftListeners.delete(listener);
    };
  };

  // ---------------------------------------------------------------- lifecycle

  /** Loads the saved sessions. Safe to call again after `dispose` (React StrictMode mounts twice in development). */
  start(): void {
    this.disposed = false;
    this.loadSpeechPreference();
    this.bootstrapping = this.bootstrap();
    void this.refreshTools();
    this.stopToolsTimer();
    this.toolsTimer = window.setInterval(() => {
      if (document.visibilityState === "visible") void this.refreshTools();
    }, TOOLS_REFRESH_MS);
  }

  dispose(): void {
    this.disposed = true;
    this.stopToolsTimer();
    this.request?.abort();
    this.request = null;
    this.abandonRecording();
    this.clearTimers();
    this.turn++;
    this.player.stop();
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

  /**
   * Asks the brain which tool servers it has connected and redraws the sidebar's
   * tool rows (and the brain row, unless a turn is running). Signed out, or the
   * page's own server failing, leaves the list as it is.
   */
  async refreshTools(): Promise<void> {
    const result = await chatApi.tools();
    if (this.disposed || !result.ok) return;
    this.chat.setState((current) => {
      const agents = withStatus(current.agents, result.data);
      // Unchanged (the usual minute-to-minute answer): keep the same array, so nothing re-renders.
      return JSON.stringify(agents) === JSON.stringify(current.agents) ? {} : { agents };
    });
  }

  private stopToolsTimer(): void {
    if (this.toolsTimer !== null) window.clearInterval(this.toolsTimer);
    this.toolsTimer = null;
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

    // Leaving a session is not "stop": a turn on its way finishes, and its reply is saved there.
    this.cancelTurn({ keepRequest: true });
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
    // As in selectSession: a turn on its way finishes in the session it belongs to.
    this.cancelTurn({ keepRequest: true });
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

  async send(raw: string, file: File | null = null): Promise<void> {
    const text = raw.trim();
    if ((!text && !file) || isFocalMode(this.orb.getState().mode)) return;

    this.clearTimers();
    const mine = ++this.turn;
    this.player.stop();
    const demo = file ? null : parseDemoCommand(text);

    const sentId = this.append({
      role: "user",
      content: { text: text || null, widgets: [] },
      origin: demo ? "demo" : "server",
      ...(file ? { attachments: [{ name: file.name, size: file.size }] } : {}),
    });
    if (!demo) this.chat.setState({ sendingId: sentId });
    this.orb.setState({ mode: "thinking", voice: false });
    this.showPending();

    if (demo) {
      this.later(DEMO_THINK_MS, () => {
        this.append({ role: "jarvis", content: demoReply(demo, Date.now()), origin: "demo" });
        this.settle(mine);
      });
      return;
    }

    this.setAgent("brain", "working");
    await this.bootstrapping;
    // Stopped (or the session left) while the page was still loading: the turn never goes out.
    if (this.disposed || this.turn !== mine) return;

    const epoch = this.epoch;
    const { activeSessionId } = this.chat.getState();
    const request = this.beginRequest();
    const result = await chatApi.send(
      {
        message: text,
        ...(file ? { file } : {}),
        ...(activeSessionId ? { conversationId: activeSessionId } : { newConversation: true }),
        ...(this.speech.getState().enabled ? { speak: true } : {}),
      },
      request.signal,
      // Jarvis's steps on the way, into the thinking bubble - and an action waiting for a yes or no,
      // as the Approval Card under it - only while this turn is the one waiting.
      {
        onProgress: (status) => {
          if (this.turn === mine && this.request === request) this.reportStatus(status);
        },
        onApproval: (approval) => {
          if (this.turn === mine && this.request === request) this.showApproval(approval);
        },
        onApprovalResolved: ({ id }) => {
          if (this.turn === mine && this.request === request) this.closeApproval(id);
        },
      }
    );
    if (this.request === request) this.request = null;
    if (this.disposed) return;
    // Stopped: endVoice has already put everything back; the answer is dropped on purpose.
    if (!result.ok && result.kind === "cancelled") return;
    this.hidePending(mine);
    if (this.chat.getState().sendingId === sentId) this.chat.setState({ sendingId: null });

    let toSpeak: SpokenReply | null = null;
    if (result.ok) {
      this.setAgent("brain", "ready");
      const reply = toChatMessage(result.data.reply);
      // The saved copy of what was sent: its real id is what Delete needs.
      const sent = toChatMessage(result.data.userMessage);
      const { conversationId } = result.data;
      if (epoch === this.epoch) {
        toSpeak = { audio: result.data.audio, text: reply.content.text };
        this.chat.setState((state) => ({
          access: "signed-in",
          activeSessionId: conversationId,
          messages: [...state.messages.map((message) => (message.id === sentId ? sent : message)), reply],
          sessions: {
            status: "ready",
            // A file sent with no words titles its session with its name, as the server does.
            items: touchSession(
              state.sessions.items,
              conversationId,
              (text || file?.name || "Attachment").slice(0, TITLE_CHARS),
              reply.createdAt
            ),
          },
        }));
      } else {
        // The person opened another session meanwhile: the reply is saved there, this page just refreshes the list.
        void this.refreshSessions();
      }
    } else {
      this.reportFailure(result, epoch);
    }

    // A reply that is being read out keeps the orb on "thinking" until the sound starts, then "speaking".
    if (!toSpeak || !this.speakReply(toSpeak, mine)) this.settle(mine);
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
    // The microphone must not hear Jarvis: a reply still being read out stops now.
    this.player.stop();
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
    this.showPending();
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
    if (this.disposed || this.turn !== mine) return;

    const epoch = this.epoch;
    const { activeSessionId } = this.chat.getState();
    const request = this.beginRequest();
    const result = await chatApi.sendVoice(
      audio.blob,
      {
        ...(activeSessionId ? { conversationId: activeSessionId } : { newConversation: true }),
        ...(this.speech.getState().enabled ? { speak: true } : {}),
      },
      request.signal
    );
    if (this.request === request) this.request = null;
    if (this.disposed) return;
    // Stopped: endVoice has already put everything back; nothing of this turn is shown (or saved).
    if (!result.ok && result.kind === "cancelled") return;
    this.hidePending(mine);

    let toSpeak: SpokenReply | null = null;
    if (result.ok) {
      // Say where the words were heard: "local" never left the brain's machine, "openai" went to the Whisper API.
      this.setAgent("voice", "ready", result.data.engine === "local" ? "Heard you, on this device" : "Heard you");
      this.setAgent("brain", "ready");
      const heard = toChatMessage(result.data.userMessage);
      const reply = toChatMessage(result.data.reply);
      const { conversationId } = result.data;
      if (epoch === this.epoch) {
        toSpeak = { audio: result.data.audio, text: reply.content.text };
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

    if (!toSpeak || !this.speakReply(toSpeak, mine)) this.settle(mine);
  }

  // ------------------------------------------------------------------- speech

  /** Reads the saved choice. Only `start` calls this: it needs `window`, which the server render has not got. */
  private loadSpeechPreference(): void {
    try {
      this.speech.setState({ enabled: window.localStorage.getItem(SPEECH_STORAGE_KEY) !== "off" });
    } catch {
      // Storage blocked (private window, site data off): the choice just lasts until the page closes.
    }
  }

  toggleSpeech(): void {
    const enabled = !this.speech.getState().enabled;
    this.speech.setState({ enabled });
    try {
      if (enabled) window.localStorage.removeItem(SPEECH_STORAGE_KEY);
      else window.localStorage.setItem(SPEECH_STORAGE_KEY, "off");
    } catch {
      // See loadSpeechPreference.
    }
    if (!enabled) this.player.stop();
  }

  /**
   * Plays `reply`'s audio - if voice replies are still on, the turn is still the current one,
   * and the reply's own text is short enough (the server applies the same limit; this is the
   * page's own check). Returns true when the audio was handed to the player, which then owns
   * the end of the turn: the orb stays on "thinking" until sound really starts (so it never
   * flashes "idle" in between, nor claims "speaking" over silence), says "speaking" while it
   * plays, and goes idle when it ends - however it ends (finished, refused by the browser,
   * cut off). The give-up timer is for a clip that neither starts nor fails, which would
   * otherwise leave the orb thinking, and the input locked, for good.
   */
  private speakReply({ audio, text }: SpokenReply, turn: number): boolean {
    if (!audio || !canSpeak(text) || !this.speech.getState().enabled) return false;
    if (this.disposed || this.turn !== turn) return false;

    let started = false;
    const giveUp = window.setTimeout(() => {
      if (!started) this.player.stop();
    }, SPEECH_START_MS);

    this.player.play(audio, {
      onStart: () => {
        started = true;
        window.clearTimeout(giveUp);
        if (this.turn === turn) this.orb.setState({ mode: "speaking", voice: false });
      },
      onEnd: () => {
        window.clearTimeout(giveUp);
        if (this.turn === turn) this.orb.setState({ mode: "idle", voice: false });
      },
    });
    return true;
  }

  // -------------------------------------------------------------- test bench

  forceOrb(mode: OrbMode): void {
    this.abandonRecording();
    this.resetVoiceAgent();
    this.clearTimers();
    this.turn++;
    this.player.stop();
    this.orb.setState({ mode, voice: mode !== "idle" });
    // "Thinking" shows the thinking bubble too, so it can be looked at without a real turn.
    this.chat.setState({ pending: mode === "thinking" ? { status: null } : null });
  }

  previewWidget(tool: DemoTool): void {
    this.abandonRecording();
    this.resetVoiceAgent();
    this.clearTimers();
    const mine = ++this.turn;
    this.player.stop();
    this.orb.setState({ mode: "thinking", voice: false });
    this.showPending();
    this.later(DEMO_THINK_MS, () => {
      this.append({ role: "jarvis", content: demoReply(tool, Date.now()), origin: "demo" });
      this.settle(mine);
    });
  }

  // --------------------------------------------------------- message actions

  editMessage(id: string): void {
    const message = this.chat.getState().messages.find((item) => item.id === id);
    const text = message?.role === "user" ? message.content.text : null;
    if (!text) return;
    for (const listener of [...this.draftListeners]) listener(text);
  }

  async deleteMessage(id: string): Promise<void> {
    const { messages, sendingId, activeSessionId } = this.chat.getState();
    const message = messages.find((item) => item.id === id);
    if (!message || id === sendingId) return;

    const epoch = this.epoch;
    this.chat.setState((state) => ({ messages: state.messages.filter((item) => item.id !== id) }));

    let savedId: string | null = isLocal(message) ? null : id;
    // A typed message whose turn never came back with its saved copy may still have been saved.
    if (!savedId && message.role === "user" && message.origin === "server" && activeSessionId) {
      const copy = await this.findSavedCopy(activeSessionId, message);
      if ("failed" in copy) return this.undoDelete(message, epoch, copy.failed);
      savedId = "found" in copy ? copy.found : null;
    }
    // Never saved (a notice, a sample, a turn that did not reach the server): off the page is all there is.
    if (!savedId) return;

    const result = await chatApi.deleteMessage(savedId);
    // Gone already (deleted in another tab) is as good as deleted.
    if (result.ok || result.kind === "not-found") return;
    this.undoDelete(message, epoch, result);
  }

  /** The server would not delete it: back where it was, and say why. */
  private undoDelete(message: ChatMessage, epoch: number, failure: ApiFailure): void {
    if (failure.kind === "signed-out") this.chat.setState({ access: "signed-out" });
    if (this.disposed || epoch !== this.epoch) return;
    this.chat.setState((state) => ({ messages: restore(state.messages, message) }));
    this.notify(
      failure.kind === "signed-out"
        ? failure.message
        : failure.kind === "network"
          ? "Couldn't delete that message - check your connection, then try again."
          : failure.message
    );
  }

  /**
   * The saved copy of `message`, a typed message the page only knows as `local-N` (its turn was
   * stopped, or the brain failed, so the saved copy never came back): the session's saved user
   * message with the same words that the page does not already show, closest in time.
   */
  private async findSavedCopy(conversationId: string, message: ChatMessage): Promise<SavedCopy> {
    const result = await chatApi.conversation(conversationId);
    if (!result.ok) return result.kind === "not-found" ? { none: true } : { failed: result };
    const shown = new Set(this.chat.getState().messages.map((item) => item.id));
    let best: ChatMessage | null = null;
    for (const saved of result.data.messages.map(toChatMessage)) {
      if (shown.has(saved.id) || !sameMessage(saved, message)) continue;
      if (!best || Math.abs(saved.createdAt - message.createdAt) < Math.abs(best.createdAt - message.createdAt)) best = saved;
    }
    return best ? { found: best.id } : { none: true };
  }

  // ------------------------------------------------------------------ helpers

  /**
   * Stops whatever the orb is part-way through and sends it home. A recording in progress is
   * thrown away, and the request of a turn waiting for its answer is aborted ("Stop generating"):
   * the page drops the answer and the server does not save it. `keepRequest` is for leaving a
   * session (selectSession, newSession): that turn's request carries on, and its reply is saved
   * in the session it belongs to.
   */
  private cancelTurn({ keepRequest = false }: { keepRequest?: boolean } = {}): void {
    if (!keepRequest) this.request?.abort();
    this.request = null;
    this.abandonRecording();
    this.clearTimers();
    this.turn++;
    this.player.stop();
    this.orb.setState({ mode: "idle", voice: false });
    if (this.chat.getState().pending) this.chat.setState({ pending: null });
    if (this.chat.getState().sendingId) this.chat.setState({ sendingId: null });
    this.resetVoiceAgent();
    // A stopped typed turn will never hear back: the manager is not working on it any more as far as this page knows.
    if (!keepRequest && this.chat.getState().agents.find((agent) => agent.id === "brain")?.state === "working") {
      this.setAgent("brain", "ready");
    }
  }

  /** A new request for this turn: its signal is what cancelTurn aborts. */
  private beginRequest(): AbortController {
    const request = new AbortController();
    this.request = request;
    return request;
  }

  /** Jarvis's thinking bubble at the foot of the feed, for this turn. */
  private showPending(): void {
    this.chat.setState({ pending: { status: null } });
  }

  /** Takes the bubble away - unless a newer turn already has its own. */
  private hidePending(turn: number): void {
    if (this.turn === turn && this.chat.getState().pending) this.chat.setState({ pending: null });
  }

  /**
   * What Jarvis is doing, in words ("Searching the web", "Delegating to Head of Analytics"), for
   * the thinking bubble while a turn is on its way; null goes back to "thinking". A typed turn
   * calls it for each step the brain streams (send -> chatApi.send's onProgress). The bubble's
   * own dots say "working", so a trailing "..." is dropped.
   */
  reportStatus(status: string | null): void {
    const pending = this.chat.getState().pending;
    if (!pending) return;
    const words = status?.replace(/\s*(?:\.{3}|…)\s*$/, "").trim() || null;
    // An Approval Card that is waiting stays: a new step is not an answer to it.
    this.chat.setState({ pending: { ...pending, status: words } });
  }

  /** An action is paused for the person's yes or no: the card goes under the bubble. */
  private showApproval(request: ApprovalRequest): void {
    const pending = this.chat.getState().pending;
    if (!pending) return;
    this.chat.setState({
      pending: {
        ...pending,
        status: "Waiting for your approval",
        approval: {
          id: request.id,
          tool: request.tool,
          prompt: request.prompt,
          details: request.details,
          expiresAt: Date.now() + request.expiresInSeconds * 1000,
          state: "waiting",
        },
      },
    });
  }

  /** The card `id` was answered, timed out, or was given up on: take it away (a different card stays). */
  private closeApproval(id: string): void {
    const pending = this.chat.getState().pending;
    if (!pending?.approval || pending.approval.id !== id) return;
    this.chat.setState({ pending: { ...pending, status: null, approval: null } });
  }

  async decideApproval(id: string, decision: "allow" | "deny"): Promise<void> {
    const approval = this.chat.getState().pending?.approval;
    if (!approval || approval.id !== id || approval.state !== "waiting") return;
    const update = (change: Partial<NonNullable<typeof approval>> | null, status?: string | null) =>
      this.chat.setState((state) => {
        if (!state.pending?.approval || state.pending.approval.id !== id) return {};
        return {
          pending: {
            ...state.pending,
            ...(status !== undefined ? { status } : {}),
            approval: change ? { ...state.pending.approval, ...change } : null,
          },
        };
      });

    update({ state: "sending", error: undefined });
    const result = await chatApi.decideApproval(id, decision);
    if (this.disposed) return;
    if (result.ok) {
      // The brain has it and carries on; its closing line would take the card away a moment later anyway.
      update(null, decision === "allow" ? "Approved - carrying on" : "Declined - carrying on");
    } else if (result.kind === "not-found") {
      // Timed out, or the turn was stopped: nothing is waiting any more.
      update(null, null);
    } else {
      update({ state: "waiting", error: result.message });
    }
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

  /** Back to idle - but only if nobody pressed Cancel or began another turn meanwhile. */
  private settle(turn: number): void {
    if (this.turn === turn) this.orb.setState({ mode: "idle", voice: false });
    this.hidePending(turn);
  }

  /** Adds a message made on this page; returns its `local-N` id. */
  private append(
    message: Pick<ChatMessage, "role" | "origin" | "attachments"> & { content: MessageContent; tone?: "error" }
  ): string {
    const next: ChatMessage = {
      id: `${LOCAL_ID}${this.nextId++}`,
      createdAt: Date.now(),
      ...message,
    };
    this.chat.setState((state) => ({ messages: [...state.messages, next] }));
    return next.id;
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
