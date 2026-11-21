import { memo } from "react";
import { documentIngestText, documentMemoryText } from "@/lib/jarvis/documentMemory";
import { formatMessageTime } from "@/lib/jarvis/sessions";
import type { ChatMessage } from "@/lib/jarvis/types";
import { AttachmentChip } from "./AttachmentChip";
import { DocumentIngestPill, DocumentMemoryPill } from "./DocumentMemoryPill";
import { DynamicRenderer } from "./DynamicRenderer";
import { MemoryPill } from "./MemoryPill";
import { MessageActions } from "./MessageActions";
import { SampleBadge } from "./widgets/SampleBadge";

/**
 * One entry of the conversation log (an `<li>`): the person's words in an ink
 * bubble on the right, or Jarvis's reply on the left under a quiet byline.
 *
 * Memoised on the message object (and `locked`). The store keeps every message the
 * same object until it is replaced, so when a reply arrives the rows above it do not
 * re-render - which is what keeps a long conversation from costing the orb
 * frames.
 *
 * The person's text is always plain (never parsed for widgets); only Jarvis's
 * goes through DynamicRenderer. A file they sent shows as its chip above their
 * words (or alone, when they sent only the file).
 *
 * The message's actions (MessageActions) sit on lines it already has, so they add no
 * height: Edit and Delete beside the time under the person's bubble, Copy and Delete
 * at the end of Jarvis's byline. A reply that saved to Jarvis's memory ends with a
 * MemoryPill ("Saved to memory · 2 entries"), and one whose question came with a file and Save to
 * Memory switched on, first a DocumentIngestPill ("Saving key facts from CV.pdf…" - the brain reads
 * the file after it has answered) and then a DocumentMemoryPill per file ("Saved 6 facts from
 * Spec_v2.pdf", which opens onto the facts). `locked` - this message's answer is still on its way -
 * holds Delete back.
 */
export const MessageBubble = memo(function MessageBubble({ message, locked = false }: { message: ChatMessage; locked?: boolean }) {
  const time = formatMessageTime(message.createdAt);

  if (message.role === "user") {
    return (
      <li className="group flex flex-col items-end gap-1.5">
        {message.attachments?.map((file, index) => (
          <AttachmentChip key={index} name={file.name} size={file.size} className="max-w-[85%]" />
        ))}
        {message.content.text && (
          <p className="max-w-[85%] whitespace-pre-line rounded-md bg-slate-900 px-4 py-2.5 text-[15px] leading-6 text-white [overflow-wrap:anywhere]">
            {message.content.text}
          </p>
        )}
        <div className="flex items-center gap-2 text-xs text-slate-600">
          <MessageActions message={message} locked={locked} />
          {message.origin === "demo" && <SampleBadge />}
          <time className="tabular-nums" dateTime={new Date(message.createdAt).toISOString()}>
            {time}
          </time>
        </div>
      </li>
    );
  }

  return (
    <li className="group max-w-full">
      <div className="flex items-center gap-2">
        <span className="text-xs font-semibold text-slate-900">Jarvis</span>
        <time className="text-xs tabular-nums text-slate-600" dateTime={new Date(message.createdAt).toISOString()}>
          {time}
        </time>
        {message.origin === "demo" && <SampleBadge />}
        <MessageActions message={message} locked={locked} className="ml-auto" />
      </div>
      <div className="mt-1.5">
        <DynamicRenderer content={message.content} tone={message.tone} />
      </div>
      {/* What Jarvis's memory really wrote while answering (only on a reply that just arrived). */}
      {message.memory && <MemoryPill note={message.memory} />}
      {/* Save to Memory, which reads the attached file after the reply: how it is going, then what it saved
          (only on a reply that just arrived). */}
      {message.documentIngest && <DocumentIngestPill ingest={message.documentIngest} />}
      {message.documentMemories?.map((memory) => (
        <DocumentMemoryPill key={memory.sourceFile} memory={memory} />
      ))}
      {(message.documentIngest !== undefined || (message.documentMemories?.length ?? 0) > 0) && (
        // One line for assistive tech, there from "Saving…" on, so the change to "Saved 6 facts" is announced.
        <p role="status" className="sr-only">
          {message.documentIngest
            ? documentIngestText(message.documentIngest)
            : (message.documentMemories ?? []).map(documentMemoryText).join(". ")}
        </p>
      )}
    </li>
  );
});
