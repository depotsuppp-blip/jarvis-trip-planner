"use client";

import { useEffect, useRef, useState, type MouseEvent } from "react";
import { Check, Copy, Pencil, Trash2, X } from "lucide-react";
import { copyText } from "@/lib/jarvis/clipboard";
import { useChatActions } from "@/lib/jarvis/JarvisProvider";
import type { ChatMessage } from "@/lib/jarvis/types";
import { focusRing } from "./styles";

/** How long "Copied" stays before the button is a copy icon again. */
const FEEDBACK_MS = 2000;
/** How long Delete waits for its second press before it stands down. */
const CONFIRM_MS = 4000;

/**
 * Hidden until the message is hovered or holds keyboard focus - on a device that can hover.
 * On touch there is no hover, so the row is always there, in Muted. A button that is
 * saying something ("Copied", "Delete?") keeps the row up while it does.
 */
const ROW =
  "flex items-center gap-0.5 transition-opacity duration-150 motion-reduce:transition-none [@media(hover:hover)]:opacity-0 group-hover:opacity-100 focus-within:opacity-100 has-[[data-active]]:opacity-100";

/**
 * A quiet 32px ghost button (40px under a finger). Its negative margin keeps the 16px line
 * it sits on - the time under a bubble, Jarvis's byline - from growing, so the actions add
 * no height to a message.
 */
const BUTTON = `inline-flex h-8 min-w-8 -my-2 shrink-0 items-center justify-center gap-1.5 rounded-md text-xs font-medium transition-[background-color,color] duration-150 disabled:cursor-not-allowed disabled:opacity-40 pointer-coarse:h-10 pointer-coarse:min-w-10 pointer-coarse:-my-3 ${focusRing}`;

/** One colour set per button (two text colours in one class list would be decided by stylesheet order, not by intent). */
const TONE = {
  quiet: "text-slate-500 hover:bg-slate-100 hover:text-slate-900 disabled:hover:bg-transparent disabled:hover:text-slate-500",
  done: "px-2 text-emerald-700",
  problem: "px-2 text-red-700",
  confirm: "bg-red-50 px-2 text-red-700",
} as const;

const ICON = "size-4 shrink-0";

/**
 * What can be done with one message: Copy (Jarvis's replies), Edit (the person's own
 * words, back into the message field) and Delete (both). A message with no words - a
 * file sent on its own, a widget-only reply - has nothing to copy or edit.
 *
 * `locked` is the person's message whose answer is still on its way: it cannot be
 * deleted until the turn ends (ChatState.sendingId).
 */
export function MessageActions({
  message,
  locked = false,
  className = "",
}: {
  message: ChatMessage;
  locked?: boolean;
  className?: string;
}) {
  const text = message.content.text;
  return (
    <div role="group" aria-label="Message actions" className={`${ROW} ${className}`}>
      {message.role === "jarvis" && text && <CopyButton text={text} />}
      {message.role === "user" && text && <EditButton id={message.id} />}
      <DeleteButton id={message.id} disabled={locked} />
    </div>
  );
}

/** Copies the reply's own words, as they were sent - Markdown links and all. */
function CopyButton({ text }: { text: string }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  const timer = useRef<number | null>(null);

  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    },
    []
  );

  async function copy() {
    const copied = await copyText(text);
    setState(copied ? "copied" : "failed");
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setState("idle"), FEEDBACK_MS);
  }

  return (
    <>
      <button
        type="button"
        onClick={() => void copy()}
        data-message-action=""
        data-active={state === "idle" ? undefined : ""}
        aria-label="Copy reply"
        title="Copy"
        className={`${BUTTON} ${state === "copied" ? TONE.done : state === "failed" ? TONE.problem : TONE.quiet}`}
      >
        {state === "copied" ? (
          <Check className={ICON} aria-hidden="true" />
        ) : state === "failed" ? (
          <X className={ICON} aria-hidden="true" />
        ) : (
          <Copy className={ICON} aria-hidden="true" />
        )}
        {state === "copied" && <span aria-hidden="true">Copied</span>}
        {state === "failed" && <span aria-hidden="true">Couldn&apos;t copy</span>}
      </button>
      <span role="status" className="sr-only">
        {state === "copied" ? "Copied to the clipboard" : state === "failed" ? "Couldn't copy the reply" : ""}
      </span>
    </>
  );
}

/** Puts the message's words in the message field (Composer), to change and send again. */
function EditButton({ id }: { id: string }) {
  const actions = useChatActions();
  return (
    <button
      type="button"
      onClick={() => actions.editMessage(id)}
      data-message-action=""
      aria-label="Edit this message"
      title="Edit"
      className={`${BUTTON} ${TONE.quiet}`}
    >
      <Pencil className={ICON} aria-hidden="true" />
    </button>
  );
}

/**
 * Two presses: the first turns the button into "Delete?" in Alert Red for a few seconds,
 * the second deletes - from the page and from the saved session, which is not undone.
 * Escape, leaving the button or waiting stands it down. Deleted from the keyboard, the
 * focus moves on to the next message's actions (or the one before), not to the page.
 */
function DeleteButton({ id, disabled }: { id: string; disabled: boolean }) {
  const actions = useChatActions();
  const [armed, setArmed] = useState(false);
  const timer = useRef<number | null>(null);
  const confirming = armed && !disabled;

  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    },
    []
  );

  function standDown() {
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = null;
    setArmed(false);
  }

  function press(event: MouseEvent<HTMLButtonElement>) {
    if (!confirming) {
      setArmed(true);
      if (timer.current !== null) window.clearTimeout(timer.current);
      timer.current = window.setTimeout(standDown, CONFIRM_MS);
      return;
    }
    standDown();
    const button = event.currentTarget;
    const fromKeyboard = button.matches(":focus-visible");
    const item = button.closest("li");
    const neighbour = item?.nextElementSibling ?? item?.previousElementSibling ?? null;
    void actions.deleteMessage(id);
    if (fromKeyboard) {
      window.requestAnimationFrame(() => neighbour?.querySelector<HTMLElement>("[data-message-action]")?.focus());
    }
  }

  return (
    <button
      type="button"
      onClick={press}
      onBlur={() => armed && standDown()}
      onKeyDown={(event) => {
        if (event.key === "Escape" && armed) {
          event.stopPropagation();
          standDown();
        }
      }}
      disabled={disabled}
      data-message-action=""
      data-active={confirming ? "" : undefined}
      aria-label={confirming ? "Confirm: delete this message" : "Delete this message"}
      title={disabled ? "You can delete this once Jarvis has answered" : confirming ? "Press again to delete" : "Delete"}
      className={`${BUTTON} ${confirming ? TONE.confirm : TONE.quiet}`}
    >
      <Trash2 className={ICON} aria-hidden="true" />
      {confirming && <span aria-hidden="true">Delete?</span>}
    </button>
  );
}
