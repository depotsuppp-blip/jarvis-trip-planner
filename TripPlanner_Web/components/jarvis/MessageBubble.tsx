import { memo } from "react";
import { formatMessageTime } from "@/lib/jarvis/sessions";
import type { ChatMessage } from "@/lib/jarvis/types";
import { DynamicRenderer } from "./DynamicRenderer";
import { SampleBadge } from "./widgets/SampleBadge";

/**
 * One entry of the conversation log (an `<li>`): the person's words in an ink
 * bubble on the right, or Jarvis's reply on the left under a quiet byline.
 *
 * Memoised on the message object. The store keeps every message the same object
 * until it is replaced, so when a reply arrives the rows above it do not
 * re-render - which is what keeps a long conversation from costing the orb
 * frames.
 *
 * The person's text is always plain (never parsed for widgets); only Jarvis's
 * goes through DynamicRenderer.
 */
export const MessageBubble = memo(function MessageBubble({ message }: { message: ChatMessage }) {
  const time = formatMessageTime(message.createdAt);

  if (message.role === "user") {
    return (
      <li className="flex flex-col items-end gap-1.5">
        <p className="max-w-[85%] whitespace-pre-line rounded-md bg-slate-900 px-4 py-2.5 text-[15px] leading-6 text-white [overflow-wrap:anywhere]">
          {message.content.text}
        </p>
        <p className="flex items-center gap-2 text-xs text-slate-600">
          {message.origin === "demo" && <SampleBadge />}
          <time className="tabular-nums" dateTime={new Date(message.createdAt).toISOString()}>
            {time}
          </time>
        </p>
      </li>
    );
  }

  return (
    <li className="max-w-full">
      <p className="flex items-center gap-2">
        <span className="text-xs font-semibold text-slate-900">Jarvis</span>
        <time className="text-xs tabular-nums text-slate-600" dateTime={new Date(message.createdAt).toISOString()}>
          {time}
        </time>
        {message.origin === "demo" && <SampleBadge />}
      </p>
      <div className="mt-1.5">
        <DynamicRenderer content={message.content} tone={message.tone} />
      </div>
    </li>
  );
});
