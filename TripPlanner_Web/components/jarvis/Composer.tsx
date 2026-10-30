"use client";

import { memo, useEffect, useRef, useState, type ChangeEvent, type FormEvent } from "react";
import { ArrowUp, Mic, Paperclip, Square, X } from "lucide-react";
import { ACCEPTED_FILE_TYPES, attachmentProblem } from "@/lib/jarvis/attachments";
import { useChatActions, useOrb } from "@/lib/jarvis/JarvisProvider";
import { isFocalMode } from "@/lib/jarvis/types";
import { AttachmentChip } from "./AttachmentChip";
import { focusRing } from "./styles";

/**
 * The dock at the bottom centre: the message field, Send, Attach and Talk in ONE
 * ruled control - and every control of a turn in progress. Jarvis's face is the
 * Signal Orb in the sidebar (JarvisOrb.tsx); nothing is drawn over the conversation.
 *
 * Talk records for real (lib/jarvis/voiceRecorder.ts) and is a toggle: one press
 * starts, the next sends. While it records, the field gives its place to a
 * "Recording 0:07" clock, Talk says "Send" and Cancel throws the recording away.
 * While Jarvis works on an answer - typed or spoken - the field, Send and Attach
 * wait and Talk is off; the way out is "Stop generating" under the thinking bubble
 * at the foot of the conversation (PendingReply). Escape does either, any time.
 *
 * Attach (the paperclip) picks one file - PDF, Word .docx or text, the kinds
 * Jarvis's reader can open (lib/jarvis/attachments.ts) - which waits as a chip
 * above the field, with a button to take it off, and goes with the next typed
 * message (or on its own: Send works with a file and no words). A file that
 * cannot be read is refused at once, with the reason under the chip's place.
 * A spoken turn does not take the file: it stays in the chip, unsent, for the
 * next typed message.
 *
 * The draft - words and file - lives here, in this component's own state, and
 * nowhere else. Every keystroke re-renders this form and only this form - not the
 * conversation, not the orb (which has its own store) - so typing never costs the
 * animation a frame.
 */
export const Composer = memo(function Composer() {
  const actions = useChatActions();
  // A turn is in progress: recording, or waiting for the answer to a spoken or typed message.
  const busy = useOrb((state) => isFocalMode(state.mode));
  const listening = useOrb((state) => state.mode === "listening");
  const voice = useOrb((state) => state.voice);
  const [draft, setDraft] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [fileProblem, setFileProblem] = useState<string | null>(null);
  const field = useRef<HTMLInputElement>(null);
  const talk = useRef<HTMLButtonElement>(null);
  const attach = useRef<HTMLButtonElement>(null);
  const picker = useRef<HTMLInputElement>(null);
  const wasBusy = useRef(false);
  const wasVoice = useRef(false);

  const canSend = (draft.trim().length > 0 || file !== null) && !busy;

  function pick(event: ChangeEvent<HTMLInputElement>) {
    const chosen = event.target.files?.[0] ?? null;
    // Cleared so that picking the same file again (after removing it) still counts as a change.
    event.target.value = "";
    if (!chosen) return;
    const problem = attachmentProblem(chosen.name, chosen.size);
    setFileProblem(problem);
    // A file that cannot be sent does not replace one that can.
    if (!problem) setFile(chosen);
  }

  function removeFile() {
    setFile(null);
    setFileProblem(null);
    // The remove button is gone with the chip: keep the keyboard where the file came from.
    attach.current?.focus();
  }

  // Escape ends the turn in progress, wherever focus is.
  useEffect(() => {
    if (!busy) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") actions.endVoice();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [busy, actions]);

  // A turn is over and the control that held focus is gone or was switched off (Cancel, Stop
  // generating, or the field while it waited): put the keyboard back where the next turn starts - Talk after a
  // spoken one, the field after a typed one - unless the person already went elsewhere.
  useEffect(() => {
    if (busy) {
      wasVoice.current = voice;
    } else if (wasBusy.current && document.activeElement === document.body) {
      (wasVoice.current ? talk.current : field.current)?.focus();
    }
    wasBusy.current = busy;
  }, [busy, voice]);

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!canSend) return;
    const text = draft;
    const attached = file;
    setDraft("");
    setFile(null);
    setFileProblem(null);
    void actions.send(text, attached);
  }

  return (
    <div className="relative z-10 shrink-0 border-t border-slate-200 bg-white px-4 pb-[max(0.75rem,env(safe-area-inset-bottom))] pt-3">
      {/* Always in the page (empty when there is nothing to say), so a new chip or problem is announced. */}
      <div
        aria-live="polite"
        className={`mx-auto flex max-w-2xl flex-wrap items-center gap-x-3 gap-y-1.5 ${file || fileProblem ? "mb-2" : ""}`}
      >
        {file && <AttachmentChip name={file.name} size={file.size} onRemove={removeFile} removeDisabled={busy} />}
        {fileProblem && <p className="text-sm leading-5 text-red-700">{fileProblem}</p>}
      </div>

      {/* Outside the form: the dock's focus ring follows its text field only. Opened by the paperclip. */}
      <input
        ref={picker}
        type="file"
        accept={ACCEPTED_FILE_TYPES}
        onChange={pick}
        tabIndex={-1}
        aria-hidden="true"
        className="hidden"
      />

      <form
        onSubmit={submit}
        className="mx-auto flex max-w-2xl items-center gap-1.5 rounded-md border border-slate-300 bg-white p-1.5 has-[input:focus-visible]:border-slate-900 has-[input:focus-visible]:outline-2 has-[input:focus-visible]:outline-offset-2 has-[input:focus-visible]:outline-rose-600"
      >
        {listening ? (
          <RecordingClock />
        ) : (
          <input
            ref={field}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            disabled={busy}
            placeholder="Message Jarvis"
            aria-label="Message Jarvis"
            autoComplete="off"
            className="h-11 min-w-0 flex-1 bg-transparent px-2 text-base text-slate-900 caret-rose-600 outline-none placeholder:text-slate-500 disabled:cursor-not-allowed"
          />
        )}

        <button
          type="submit"
          disabled={!canSend}
          aria-label="Send"
          className={`grid size-11 shrink-0 place-items-center rounded-md bg-slate-900 text-white transition-[background-color,scale] duration-150 hover:bg-slate-700 active:scale-[0.97] disabled:bg-slate-100 disabled:text-slate-500 disabled:hover:bg-slate-100 ${focusRing}`}
        >
          <ArrowUp className="size-5" aria-hidden="true" />
        </button>

        <button
          ref={attach}
          type="button"
          onClick={() => picker.current?.click()}
          disabled={busy}
          aria-label={file ? `Attach a different file (now ${file.name})` : "Attach a file"}
          title="Attach a PDF, Word or text file"
          className={`grid size-11 shrink-0 place-items-center rounded-md border border-slate-300 text-slate-900 transition-[border-color,scale] duration-150 hover:border-slate-900 active:scale-[0.97] disabled:cursor-not-allowed disabled:text-slate-400 disabled:hover:border-slate-300 ${focusRing}`}
        >
          <Paperclip className="size-5" aria-hidden="true" />
        </button>

        {listening && (
          <button
            type="button"
            onClick={actions.endVoice}
            aria-label="Cancel recording"
            title="Cancel (Esc)"
            className={`inline-flex h-11 w-11 shrink-0 items-center justify-center gap-2 rounded-md border border-slate-300 text-sm font-semibold text-slate-900 transition-[border-color,scale] duration-150 hover:border-slate-900 active:scale-[0.97] sm:w-auto sm:px-3.5 ${focusRing}`}
          >
            <X className="size-5" aria-hidden="true" />
            <span className="hidden sm:inline">Cancel</span>
          </button>
        )}

        <button
          ref={talk}
          type="button"
          onClick={actions.toggleVoice}
          // While Jarvis is answering, Stop generating (under the thinking bubble) is the way out; Talk waits for the next turn.
          disabled={busy && !listening}
          aria-label={listening ? "Stop recording and send" : "Talk to Jarvis"}
          aria-pressed={voice}
          className={`inline-flex h-11 w-11 shrink-0 items-center justify-center gap-2 rounded-md border border-slate-300 text-sm font-semibold text-slate-900 transition-[background-color,border-color,color,scale] duration-150 hover:border-slate-900 active:scale-[0.97] disabled:cursor-not-allowed disabled:hover:border-slate-300 aria-pressed:border-slate-900 aria-pressed:bg-slate-900 aria-pressed:text-white aria-pressed:disabled:opacity-60 sm:w-auto sm:px-3.5 ${focusRing}`}
        >
          {listening ? (
            <Square className="size-4 fill-current" aria-hidden="true" />
          ) : (
            <Mic className="size-5" aria-hidden="true" />
          )}
          <span className="hidden sm:inline">{listening ? "Send" : "Talk"}</span>
        </button>
      </form>
    </div>
  );
});

/**
 * "Recording 0:07" in the field's place, with an ink dot that pulses like the sidebar's
 * "working" dot (still, under reduced motion). It counts from the moment it appears -
 * which is the moment recording starts - and ticks in its own state, so a second
 * passing re-renders this line and nothing else.
 */
function RecordingClock() {
  const [elapsedMs, setElapsedMs] = useState(0);

  useEffect(() => {
    const startedAt = Date.now();
    const timer = window.setInterval(() => setElapsedMs(Date.now() - startedAt), 250);
    return () => window.clearInterval(timer);
  }, []);

  const seconds = Math.floor(elapsedMs / 1000);
  return (
    // One line even on a phone, where four buttons share the dock: 14px there, 16px from sm up.
    <p className="flex h-11 min-w-0 flex-1 items-center gap-2 overflow-hidden whitespace-nowrap px-2 text-sm tabular-nums text-slate-700 sm:text-base">
      <span aria-hidden="true" className="relative grid size-2.5 shrink-0 place-items-center">
        <span className="absolute size-full rounded-full bg-slate-900/30 motion-safe:animate-ping" />
        <span className="size-2.5 rounded-full bg-slate-900" />
      </span>
      Recording {Math.floor(seconds / 60)}:{String(seconds % 60).padStart(2, "0")}
    </p>
  );
}
