"use client";

import { useCallback, useEffect, useLayoutEffect, useRef, useState, type FormEvent } from "react";
import Link from "next/link";
import { useSession } from "next-auth/react";
import { ArrowUp, Mic, X } from "lucide-react";
import { Orb, type OrbHandle } from "@/components/jarvis/Orb";

// The Jarvis hub: a chat surface with one orb that lives in the input bar and
// swells to the middle of the screen while Jarvis is being listened to,
// thinking or speaking. Everything here is mocked - no model, no microphone -
// so the orb's states and the move between its two homes can be tested by eye.

type OrbMode = "idle" | "listening" | "thinking" | "speaking";
type Message = { id: string; role: "user" | "jarvis"; text: string };
type ServerMessage = { id: string; role: string; content: string };

// Our four states -> thinking-orbs' nine animations.
const ORB_ANIMATION = {
  idle: "breathing",
  listening: "listening",
  thinking: "working",
  speaking: "composing",
} as const;

const ORB_LABEL: Record<OrbMode, string> = {
  idle: "Idle",
  listening: "Listening",
  thinking: "Thinking",
  speaking: "Speaking",
};

// One orb that travels between its homes instead of two that swap. It is drawn
// at its real pixel size in each home, never a small orb enlarged with CSS
// scale() (which blurs it) - see components/jarvis/Orb.tsx.
const ORB_SMALL_PX = 36;
const ORB_FOCAL_PX = 224;

// Mock timings for a full voice turn: listen, think, speak, back to idle.
const MOCK_LISTEN_MS = 2600;
const MOCK_THINK_MS = 2200;
const MOCK_SPEAK_MS = 3000;

const GREETING: Message = { id: "greeting", role: "jarvis", text: "Good evening. What are we planning?" };

const REPLIES = [
  "Done. I've noted that for your next trip.",
  "Three places match. Want me to open the first one?",
  "Your poll has four votes so far, mostly for cafe hopping.",
];

export default function JarvisPage() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState("");
  const [orbMode, setOrbMode] = useState<OrbMode>("idle");
  const [isVoiceMode, setIsVoiceMode] = useState(false);

  const timers = useRef<number[]>([]);
  const nextId = useRef(1);
  // The saved conversation (null until the first message), and a counter that
  // lets a finished request tell whether the turn it belonged to was cancelled.
  const conversationId = useRef<string | null>(null);
  const turn = useRef(0);
  const replyIndex = useRef(0);
  const scroller = useRef<HTMLDivElement | null>(null);
  const slot = useRef<HTMLDivElement | null>(null);
  const orb = useRef<OrbHandle | null>(null);

  // Centre stage only while Jarvis is listening or thinking; idle and speaking
  // send the orb back to the input bar.
  const isFocal = orbMode === "listening" || orbMode === "thinking";
  const { data: session, status: sessionStatus } = useSession();

  const clearTimers = useCallback(() => {
    timers.current.forEach((id) => window.clearTimeout(id));
    timers.current = [];
  }, []);

  const later = useCallback((ms: number, fn: () => void) => {
    timers.current.push(window.setTimeout(fn, ms));
  }, []);

  useEffect(() => clearTimers, [clearTimers]);

  // Pick up the saved conversation. Signed out or offline just means a fresh start.
  useEffect(() => {
    let cancelled = false;
    fetch("/api/chat", { cache: "no-store" })
      .then((res) => (res.ok ? res.json() : null))
      .then((data: { conversationId: string | null; messages: ServerMessage[] } | null) => {
        if (cancelled || !data) return;
        conversationId.current = data.conversationId;
        if (data.messages.length > 0) {
          // Keep anything typed while this was loading after the saved history.
          setMessages((current) => [
            ...data.messages.map((m): Message => ({ id: m.id, role: m.role === "user" ? "user" : "jarvis", text: m.content })),
            ...current,
          ]);
        }
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  const reply = useCallback(() => {
    const text = REPLIES[replyIndex.current++ % REPLIES.length];
    setMessages((m) => [...m, { id: `local-${nextId.current++}`, role: "jarvis", text }]);
  }, []);

  // thinking -> speaking -> idle, shared by typed and spoken turns.
  const runResponse = useCallback(
    (afterIdle?: () => void) => {
      setOrbMode("thinking");
      later(MOCK_THINK_MS, () => {
        reply();
        setOrbMode("speaking");
        later(MOCK_SPEAK_MS, () => {
          setOrbMode("idle");
          setIsVoiceMode(false);
          afterIdle?.();
        });
      });
    },
    [later, reply],
  );

  function endVoice() {
    clearTimers();
    turn.current++;
    setIsVoiceMode(false);
    setOrbMode("idle");
  }

  function toggleVoice() {
    if (isFocal) return endVoice();
    clearTimers();
    turn.current++;
    setIsVoiceMode(true);
    setOrbMode("listening");
    later(MOCK_LISTEN_MS, () => {
      setMessages((m) => [...m, { id: `local-${nextId.current++}`, role: "user", text: "Find me a quiet cafe near the old town." }]);
      runResponse();
    });
  }

  async function send(event: FormEvent) {
    event.preventDefault();
    const text = draft.trim();
    if (!text || isFocal) return;
    clearTimers();
    const mine = ++turn.current;
    setMessages((m) => [...m, { id: `local-${nextId.current++}`, role: "user", text }]);
    setDraft("");
    setOrbMode("thinking");

    let answer: string;
    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, conversationId: conversationId.current ?? undefined }),
      });
      const data = (await res.json().catch(() => null)) as
        | { conversationId?: string; reply?: ServerMessage; error?: string }
        | null;
      if (data?.conversationId) conversationId.current = data.conversationId;
      if (res.status === 401) throw new Error("You're not signed in on this device. Use Sign in above, then try again.");
      if (!res.ok || !data?.reply) throw new Error(data?.error ?? "Jarvis couldn't answer just now.");
      answer = data.reply.content;
    } catch (error) {
      answer = error instanceof Error && error.message ? error.message : "Jarvis couldn't answer just now.";
    }

    setMessages((m) => [...m, { id: `local-${nextId.current++}`, role: "jarvis", text: answer }]);
    // Only settle the orb if nobody pressed End or forced another state meanwhile.
    if (turn.current === mine) setOrbMode("idle");
  }

  // Manual driver for the test strip: jump straight to any state.
  function force(mode: OrbMode) {
    clearTimers();
    turn.current++;
    setOrbMode(mode);
    setIsVoiceMode(mode !== "idle");
  }

  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: "smooth" });
  }, [messages]);

  useEffect(() => {
    if (!isFocal) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && endVoice();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
    // endVoice only touches refs and setters, so it is safe to capture once per focal change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isFocal]);

  // Place the orb: over the slot in the input bar, or the middle of the screen.
  // Handed straight to the orb (no state) so a resize never re-renders. It
  // glides, growing as it goes, when `isFocal` flips; a resize just moves it.
  useLayoutEffect(() => {
    const place = (animate: boolean) => {
      const home = slot.current;
      if (!orb.current || !home) return;
      const px = isFocal ? ORB_FOCAL_PX : ORB_SMALL_PX;
      const rect = home.getBoundingClientRect();
      const x = isFocal ? (window.innerWidth - px) / 2 : rect.left + (rect.width - px) / 2;
      const y = isFocal ? window.innerHeight / 2 - px / 2 - 24 : rect.top + (rect.height - px) / 2;
      orb.current.place({ x, y, size: px }, { animate });
    };
    place(true);
    const onResize = () => place(false);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [isFocal]);

  return (
    <div className="relative flex h-svh flex-col overflow-hidden bg-white text-slate-900">
      <header className="flex h-14 shrink-0 items-center justify-between border-b border-slate-200 px-4">
        <h1 className="text-[15px] font-semibold tracking-tight">Jarvis</h1>
        <div className="flex items-center gap-3">
          <span className="text-xs font-medium uppercase tracking-[0.08em] text-slate-500" aria-live="polite">
            {ORB_LABEL[orbMode]}
          </span>
          {sessionStatus === "unauthenticated" && (
            <Link href="/login?callbackUrl=%2Fjarvis" className="text-sm font-semibold text-slate-900 underline underline-offset-2">
              Sign in
            </Link>
          )}
          {session?.user && (
            <span className="max-w-32 truncate text-sm text-slate-600" title={session.user.email ?? undefined}>
              {session.user.name ?? session.user.email}
            </span>
          )}
        </div>
      </header>

      <div
        ref={scroller}
        aria-hidden={isFocal}
        className={`min-h-0 flex-1 overflow-y-auto transition-opacity duration-500 ease-out motion-reduce:transition-none ${
          isFocal ? "pointer-events-none opacity-30" : "opacity-100"
        }`}
      >
        <ol role="log" aria-label="Conversation" className="mx-auto flex max-w-2xl flex-col gap-6 px-4 py-8">
          {[GREETING, ...messages].map((m) => (
            <li key={m.id} className={m.role === "user" ? "flex justify-end" : ""}>
              {m.role === "user" ? (
                <p className="max-w-[80%] rounded-md bg-slate-900 px-4 py-2.5 text-[15px] leading-6 text-white">{m.text}</p>
              ) : (
                <div className="max-w-[85%] border-l border-slate-200 pl-4">
                  <p className="text-[11px] font-medium uppercase tracking-[0.08em] text-slate-500">Jarvis</p>
                  <p className="mt-1 text-[15px] leading-6 text-slate-700">{m.text}</p>
                </div>
              )}
            </li>
          ))}
        </ol>
      </div>

      {/* Frosts the chat behind the centred orb. */}
      <div
        aria-hidden="true"
        className={`fixed inset-0 z-20 bg-white/60 backdrop-blur-md transition-opacity duration-500 ease-out motion-reduce:transition-none ${
          isFocal ? "opacity-100" : "pointer-events-none opacity-0"
        }`}
      />

      {/* Focal caption + exit, under the centred orb. */}
      {isFocal && (
        <div className="pointer-events-none fixed inset-x-0 top-1/2 z-40 flex flex-col items-center" style={{ marginTop: ORB_FOCAL_PX / 2 - 24 }}>
          <p className="text-lg font-semibold tracking-tight">{ORB_LABEL[orbMode]}…</p>
          <button
            type="button"
            onClick={endVoice}
            className="pointer-events-auto mt-4 inline-flex min-h-11 items-center gap-2 rounded-md border border-slate-200 px-4 text-sm font-medium text-slate-700 outline-none transition hover:border-slate-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-rose-600 active:scale-[0.97]"
          >
            <X className="h-4 w-4" aria-hidden="true" />
            End
          </button>
        </div>
      )}

      <div className="relative z-10 shrink-0 border-t border-slate-200 bg-white px-4 pb-[max(0.75rem,env(safe-area-inset-bottom))] pt-3">
        <div className="mx-auto max-w-2xl">
          <form onSubmit={send} className={`flex items-center gap-2 transition-opacity duration-500 ${isFocal ? "opacity-40" : ""}`}>
            {/* Home of the small orb; the orb itself is the fixed element below. */}
            <div ref={slot} className="h-11 w-11 shrink-0" aria-hidden="true" />
            <input
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              disabled={isFocal}
              placeholder="Message Jarvis"
              aria-label="Message Jarvis"
              className="h-11 min-w-0 flex-1 rounded-md border border-slate-200 bg-white px-3 text-base text-slate-900 outline-none placeholder:text-slate-500 focus:border-slate-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-rose-600 disabled:bg-slate-50"
            />
            <button
              type="submit"
              disabled={!draft.trim() || isFocal}
              aria-label="Send"
              className="grid h-11 w-11 shrink-0 place-items-center rounded-md bg-slate-900 text-white outline-none transition focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-rose-600 active:scale-[0.97] disabled:bg-slate-100 disabled:text-slate-400"
            >
              <ArrowUp className="h-5 w-5" aria-hidden="true" />
            </button>
            <button
              type="button"
              onClick={toggleVoice}
              aria-label={isFocal ? "Stop voice mode" : "Start voice mode"}
              aria-pressed={isVoiceMode}
              className="grid h-11 w-11 shrink-0 place-items-center rounded-md border border-slate-200 text-slate-900 outline-none transition hover:border-slate-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-rose-600 active:scale-[0.97] aria-pressed:border-slate-900 aria-pressed:bg-slate-900 aria-pressed:text-white"
            >
              <Mic className="h-5 w-5" aria-hidden="true" />
            </button>
          </form>

          {/* Test strip: force any orb state to check the animations and the move. */}
          <div role="group" aria-label="Orb state (test)" className="mt-3 flex items-center gap-1 text-xs">
            <span className="mr-1 font-medium uppercase tracking-[0.08em] text-slate-500">Orb</span>
            {(Object.keys(ORB_LABEL) as OrbMode[]).map((mode) => (
              <button
                key={mode}
                type="button"
                onClick={() => force(mode)}
                aria-pressed={orbMode === mode}
                className="min-h-8 rounded-md border border-slate-200 px-2.5 font-medium text-slate-600 outline-none transition hover:border-slate-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-rose-600 aria-pressed:border-slate-900 aria-pressed:bg-slate-900 aria-pressed:text-white"
              >
                {ORB_LABEL[mode]}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* One orb, two homes. Pinned light: this page is white. */}
      <Orb
        ref={orb}
        state={ORB_ANIMATION[orbMode]}
        theme="light"
        aria-label={`Jarvis is ${ORB_LABEL[orbMode].toLowerCase()}`}
        className="pointer-events-none fixed left-0 top-0 z-30"
      />
    </div>
  );
}
