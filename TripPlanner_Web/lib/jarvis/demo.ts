import type { AgentStatus, MessageContent, WidgetPayload } from "./types";

/**
 * Everything on the hub that is made up on this page rather than answered by
 * Jarvis, so the screen can be reviewed without a backend that sends widgets:
 * the slash commands and test-bench buttons that "reply" with a widget, and the
 * agents the sidebar lists. All of it is labelled as a sample where a person
 * could mistake it for real; none of it is sent to Jarvis or saved. (Voice is
 * not here any more: the Talk button records and sends for real.)
 */

export type DemoTool = "system_status" | "calendar" | "mystery_tool";

export interface DemoCommand {
  /** What to type: `/status`. */
  command: string;
  tool: DemoTool;
  /** The test-bench button. */
  label: string;
}

export const DEMO_COMMANDS: readonly DemoCommand[] = [
  { command: "/status", tool: "system_status", label: "System status" },
  { command: "/calendar", tool: "calendar", label: "Calendar" },
  { command: "/mystery", tool: "mystery_tool", label: "Unknown tool" },
];

/** `/status` (any case, nothing after it) -> the tool it previews; anything else is a real message. */
export function parseDemoCommand(text: string): DemoTool | null {
  const typed = text.trim().toLowerCase();
  return DEMO_COMMANDS.find((entry) => entry.command === typed)?.tool ?? null;
}

function widgetReply(text: string, widget: WidgetPayload): MessageContent {
  return { text, widgets: [{ ...widget, sample: true }] };
}

/** A made-up reply that carries `tool`'s widget. `now` is when it "happened". */
export function demoReply(tool: DemoTool, now: number): MessageContent {
  switch (tool) {
    case "system_status":
      return widgetReply("Here is how the systems look. This is a sample of a widget reply.", {
        tool: "system_status",
        data: {
          updatedAt: new Date(now).toISOString(),
          services: [
            { name: "Language model", status: "operational", latencyMs: 182, uptimePct: 99.98 },
            { name: "Trip Planner", status: "operational", latencyMs: 240, uptimePct: 99.91 },
            { name: "Places search", status: "degraded", latencyMs: 910, uptimePct: 98.4, note: "Slower than usual" },
            { name: "Voice pipeline", status: "down", note: "Not connected in this preview" },
          ],
        },
      });
    case "calendar":
      return widgetReply("Your day, as a sample. The calendar has no dedicated view yet, so this is the placeholder.", {
        tool: "calendar",
        data: {
          date: new Date(now).toISOString().slice(0, 10),
          events: [
            { title: "Plan the Chiang Mai trip", start: "10:00", end: "10:30" },
            { title: "Lunch with the group", start: "12:30", end: "13:30" },
          ],
        },
      });
    case "mystery_tool":
      return widgetReply("This reply names a tool the page has never heard of.", {
        tool: "mystery_tool",
        data: { note: "Unknown tools fall back to a placeholder that shows their payload." },
      });
  }
}

/**
 * The sidebar's agents and tools as the page opens. `brain` and `voice` are live
 * (the controller moves them with each real turn, and the brain row with each
 * status check); Trip Planner says what it is. The brain's tool servers - web
 * search, memory, data, ... - are added below these by the first status check
 * (GET /api/chat/tools, see tools.ts), so only what is really connected is listed.
 */
export function initialAgents(): AgentStatus[] {
  return [
    { id: "brain", name: "Jarvis brain", kind: "brain", state: "unknown" },
    { id: "trip-planner", name: "Trip Planner", kind: "tool", state: "idle", detail: "Plans trips from chat" },
    { id: "voice", name: "Voice", kind: "voice", state: "idle", detail: "Tap Talk to speak" },
  ];
}
