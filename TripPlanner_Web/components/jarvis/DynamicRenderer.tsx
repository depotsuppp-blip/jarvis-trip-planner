import { memo } from "react";
import { CircleAlert } from "lucide-react";
import type { MessageContent } from "@/lib/jarvis/types";
import { MessageText } from "./MessageText";
import { WidgetHost } from "./widgets/WidgetHost";

/**
 * Draws a reply's content: its text, then each widget it carries.
 *
 * This is the seam the agentic backend plugs into. The state layer has already
 * parsed whatever the backend stored (lib/jarvis/messageContent.ts) into
 * `{ text, widgets }`; this component only decides how each part looks, and the
 * widget registry decides which view a tool gets. A plain-text reply is the same
 * path with an empty `widgets` list, so there is no second code path to keep in
 * step.
 */
export const DynamicRenderer = memo(function DynamicRenderer({
  content,
  tone,
}: {
  content: MessageContent;
  tone?: "error";
}) {
  return (
    <div className="space-y-3">
      {content.text &&
        (tone === "error" ? (
          <p className="flex items-start gap-2 text-[15px] leading-6 text-red-700">
            <CircleAlert className="mt-1 size-4 shrink-0" aria-hidden="true" />
            <span className="whitespace-pre-line [overflow-wrap:anywhere]">
              <MessageText text={content.text} />
            </span>
          </p>
        ) : (
          <p className="whitespace-pre-line text-[15px] leading-6 text-slate-700 [overflow-wrap:anywhere]">
            <MessageText text={content.text} />
          </p>
        ))}

      {content.widgets.map((widget, index) => (
        <WidgetHost key={`${index}:${widget.tool}`} payload={widget} />
      ))}
    </div>
  );
});
