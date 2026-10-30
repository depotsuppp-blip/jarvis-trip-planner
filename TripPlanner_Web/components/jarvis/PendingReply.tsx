"use client";

import { memo } from "react";
import { Square } from "lucide-react";
import { useChat, useChatActions } from "@/lib/jarvis/JarvisProvider";
import { focusRing } from "./styles";

/** What the bubble says when nothing has said what Jarvis is doing (see PendingReply in lib/jarvis/types.ts). */
const THINKING = "Jarvis is thinking";

/**
 * The foot of the conversation while a reply is on its way: Jarvis's byline over a small
 * bubble - three dots that pulse in turn, and what Jarvis is doing in words - with
 * "Stop generating" under it. It stands where the reply will appear and is replaced by
 * it; nothing is laid over the conversation, which stays readable and scrollable.
 *
 * The words come from the chat store's `pending.status` ("Searching the web",
 * "Generating an image") whenever something reports one (ChatController.reportStatus);
 * until then, "Jarvis is thinking". Only the words are a live region, so a change of
 * status is announced once and the dots never are. Under reduced motion the dots hold still.
 *
 * Stop generating aborts the turn's request (ChatController: endVoice): the reply is
 * neither shown nor saved, and the person's message stays.
 */
export const PendingReply = memo(function PendingReply() {
  const pending = useChat((state) => state.pending);
  const actions = useChatActions();
  if (!pending) return null;

  return (
    <div className="mt-8 max-w-full" data-pending-reply="">
      <p className="text-xs font-semibold text-slate-900">Jarvis</p>
      <div className="mt-1.5 inline-flex items-center gap-3 rounded-md border border-slate-200 bg-slate-50 px-3.5 py-2.5">
        <span aria-hidden="true" className="flex items-center gap-1">
          {[0, 1, 2].map((dot) => (
            <span
              key={dot}
              className="size-1.5 rounded-full bg-slate-500 motion-safe:animate-pulse"
              style={{ animationDelay: `${dot * 200}ms` }}
            />
          ))}
        </span>
        <span role="status" className="text-[15px] leading-6 text-slate-700">
          {pending.status ?? THINKING}
        </span>
      </div>
      <div className="mt-1.5">
        <button
          type="button"
          onClick={actions.endVoice}
          className={`inline-flex min-h-9 items-center gap-1.5 rounded-md px-2 text-[13px] font-medium text-slate-600 transition-[background-color,color,scale] duration-150 hover:bg-slate-100 hover:text-slate-900 active:scale-[0.97] ${focusRing}`}
        >
          <Square className="size-3 fill-current" aria-hidden="true" />
          Stop generating
        </button>
      </div>
    </div>
  );
});
