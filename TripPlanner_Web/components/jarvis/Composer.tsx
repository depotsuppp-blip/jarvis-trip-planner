"use client";

import { memo, useEffect, useRef, useState, type ChangeEvent, type FormEvent } from "react";
import { ArrowUp, Mic, Paperclip, Square } from "lucide-react";
import { ACCEPTED_FILE_TYPES, attachmentProblem } from "@/lib/jarvis/attachments";
import { useChatActions, useOrb, useOrbAnchors } from "@/lib/jarvis/JarvisProvider";
import { isFocalMode } from "@/lib/jarvis/types";
import { AttachmentChip } from "./AttachmentChip";
import { focusRing } from "./styles";

/**
 * The dock at the bottom centre: the orb's small home, the message field, Send,
 * Attach and Talk in ONE ruled control, so the orb reads as part of the input
 * rather than a badge beside it.
 *
 * Talk records for real (lib/jarvis/voiceRecorder.ts) and is a toggle: one press
 * starts, the next sends. While it records, the orb comes to the middle of the
 * stage with a "Recording" clock and Send / Cancel (OrbStage), frosted over this
 * dock - so the button here is reached by keyboard, and says what its next press does.
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
  const { slot } = useOrbAnchors();
  const actions = useChatActions();
  const focal = useOrb((state) => isFocalMode(state.mode));
  const listening = useOrb((state) => state.mode === "listening");
  const voice = useOrb((state) => state.voice);
  const [draft, setDraft] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [fileProblem, setFileProblem] = useState<string | null>(null);
  const talk = useRef<HTMLButtonElement>(null);
  const attach = useRef<HTMLButtonElement>(null);
  const picker = useRef<HTMLInputElement>(null);
  const wasVoice = useRef(false);

  const canSend = (draft.trim().length > 0 || file !== null) && !focal;

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

  // A spoken turn is over and the buttons that held focus (Send / Cancel on the stage) are gone:
  // hand focus back to Talk so the next press is one key away - unless the person already went elsewhere.
  useEffect(() => {
    if (wasVoice.current && !voice && document.activeElement === document.body) talk.current?.focus();
    wasVoice.current = voice;
  }, [voice]);

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
        {file && <AttachmentChip name={file.name} size={file.size} onRemove={removeFile} removeDisabled={focal} />}
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
          ref={attach}
          type="button"
          onClick={() => picker.current?.click()}
          disabled={focal}
          aria-label={file ? `Attach a different file (now ${file.name})` : "Attach a file"}
          title="Attach a PDF, Word or text file"
          className={`grid size-11 shrink-0 place-items-center rounded-md border border-slate-300 text-slate-900 transition-[border-color,scale] duration-150 hover:border-slate-900 active:scale-[0.97] disabled:cursor-not-allowed disabled:hover:border-slate-300 ${focusRing}`}
        >
          <Paperclip className="size-5" aria-hidden="true" />
        </button>

        <button
          ref={talk}
          type="button"
          onClick={actions.toggleVoice}
          aria-label={listening ? "Stop recording and send" : focal ? "Cancel this turn" : "Talk to Jarvis"}
          aria-pressed={voice}
          className={`inline-flex h-11 w-11 shrink-0 items-center justify-center gap-2 rounded-md border border-slate-300 text-sm font-semibold text-slate-900 transition-[background-color,border-color,color,scale] duration-150 hover:border-slate-900 active:scale-[0.97] aria-pressed:border-slate-900 aria-pressed:bg-slate-900 aria-pressed:text-white sm:w-auto sm:px-3.5 ${focusRing}`}
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
