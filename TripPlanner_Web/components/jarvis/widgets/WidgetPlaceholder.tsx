import { Puzzle } from "lucide-react";
import { humanizeTool } from "@/lib/jarvis/messageContent";
import type { WidgetPayload } from "@/lib/jarvis/types";
import { WidgetFrame } from "./WidgetFrame";
import type { WidgetDefinition } from "./types";

/**
 * What a widget looks like before it has a view of its own: its tool's name and
 * icon in the usual frame, one sentence on why this is a placeholder, and the raw
 * payload behind a disclosure so whoever is wiring the backend can see exactly
 * what arrived.
 *
 * It is also the fallback when a view exists but cannot be used: the data was not
 * what the widget needs, or the view crashed. A broken widget never takes the
 * conversation down with it.
 */

export type PlaceholderReason = "unknown" | "not-built" | "invalid" | "crashed";

const MAX_PAYLOAD_CHARS = 4000;

function describe(reason: PlaceholderReason, label: string): string {
  switch (reason) {
    case "unknown":
      return `This reply came with a “${label}” widget, a tool this page does not know yet.`;
    case "not-built":
      return `This reply came with a ${label} widget. Its view is not built yet, so the raw data is shown below.`;
    case "invalid":
      return `The data for this ${label} widget is not in the shape its view expects, so the raw data is shown below.`;
    case "crashed":
      return `The ${label} widget could not be drawn, so the raw data is shown below.`;
  }
}

function formatPayload(data: unknown): string {
  let text: string;
  try {
    text = JSON.stringify(data, null, 2) ?? "null";
  } catch {
    text = "(the data could not be shown)";
  }
  return text.length > MAX_PAYLOAD_CHARS ? `${text.slice(0, MAX_PAYLOAD_CHARS)}\n…` : text;
}

export function WidgetPlaceholder({
  payload,
  definition,
  reason,
}: {
  payload: WidgetPayload;
  definition?: WidgetDefinition;
  reason: PlaceholderReason;
}) {
  const label = definition?.label ?? humanizeTool(payload.tool);
  const Icon = definition?.icon ?? Puzzle;

  return (
    <WidgetFrame icon={Icon} title={payload.title ?? label} payload={payload}>
      <p className="text-sm leading-6 text-slate-700">{describe(reason, label)}</p>
      <details className="group mt-3">
        <summary className="cursor-pointer text-sm font-semibold text-slate-900 outline-none marker:text-slate-500 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-rose-600">
          Raw data
        </summary>
        <pre className="mt-2 max-h-56 overflow-auto rounded-md border border-slate-200 bg-slate-50 p-3 font-mono text-xs leading-5 text-slate-800 [scrollbar-color:#cad5e2_transparent] [scrollbar-width:thin]">
          {formatPayload(payload.data)}
        </pre>
      </details>
    </WidgetFrame>
  );
}
