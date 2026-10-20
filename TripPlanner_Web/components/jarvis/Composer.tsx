"use client";

import { memo, useState, type FormEvent } from "react";
import { ArrowUp, Mic } from "lucide-react";
import { useChatActions, useOrb, useOrbAnchors } from "@/lib/jarvis/JarvisProvider";
import { isFocalMode } from "@/lib/jarvis/types";
import { focusRing } from "./styles";

/**
 * The dock at the bottom centre: the orb's small home, the message field, Send
 * and Talk in ONE ruled control, so the orb reads as part of the input rather
 * than a badge beside it.
 *
 * The draft lives here, in this component's own state, and nowhere else. Every
 * keystroke re-renders this form and only this form - not the conversation, not
 * the orb (which has its own store) - so typing never costs the animation a frame.
 */
export const Composer = memo(function Composer() {
  const { slot } = useOrbAnchors();
  const actions = useChatActions();
  const focal = useOrb((state) => isFocalMode(state.mode));
  const voice = useOrb((state) => state.voice);
  const [draft, setDraft] = useState("");

  const canSend = draft.trim().length > 0 && !focal;

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!canSend) return;
    const text = draft;
    setDraft("");
    void actions.send(text);
  }

  return (
    <div className="relative z-10 shrink-0 border-t border-slate-200 bg-white px-4 pb-[max(0.75rem,env(safe-area-inset-bottom))] pt-3">
      <form
        onSubmit={submit}
        className={`mx-auto flex max-w-2xl items-center gap-1.5 rounded-md border border-slate-300 bg-white p-1.5 transition-[border-color,opacity] duration-500 has-[input:focus-visible]:border-slate-900 has-[input:focus-visible]:outline-2 has-[input:focus-visible]:outline-offset-2 has-[input:focus-visible]:outline-rose-600 ${
          focal ? "opacity-40" : ""
        }`}
      >
        {/* Home of the small orb. The orb itself is drawn by OrbStage and floats over this empty box. */}
        <div ref={slot} className="size-11 shrink-0" aria-hidden="true" />

        <input
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          disabled={focal}
          placeholder="Message Jarvis"
          aria-label="Message Jarvis"
          autoComplete="off"
          className="h-11 min-w-0 flex-1 bg-transparent px-1 text-base text-slate-900 caret-rose-600 outline-none placeholder:text-slate-500 disabled:cursor-not-allowed"
        />

        <button
          type="submit"
          disabled={!canSend}
          aria-label="Send"
          className={`grid size-11 shrink-0 place-items-center rounded-md bg-slate-900 text-white transition-[background-color,scale] duration-150 hover:bg-slate-700 active:scale-[0.97] disabled:bg-slate-100 disabled:text-slate-500 disabled:hover:bg-slate-100 ${focusRing}`}
        >
          <ArrowUp className="size-5" aria-hidden="true" />
        </button>

        <button
          type="button"
          onClick={actions.toggleVoice}
          aria-label={focal ? "Stop voice mode" : "Talk to Jarvis"}
          aria-pressed={voice}
          className={`inline-flex h-11 w-11 shrink-0 items-center justify-center gap-2 rounded-md border border-slate-300 text-sm font-semibold text-slate-900 transition-[background-color,border-color,color,scale] duration-150 hover:border-slate-900 active:scale-[0.97] aria-pressed:border-slate-900 aria-pressed:bg-slate-900 aria-pressed:text-white sm:w-auto sm:px-3.5 ${focusRing}`}
        >
          <Mic className="size-5" aria-hidden="true" />
          <span className="hidden sm:inline">Talk</span>
        </button>
      </form>
    </div>
  );
});
