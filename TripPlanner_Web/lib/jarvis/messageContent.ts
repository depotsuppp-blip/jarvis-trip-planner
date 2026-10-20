import type { ChatMessage, MessageContent, MessageRole, WidgetPayload } from "./types";

/**
 * Turns what the backend stored for a reply into what the page draws.
 *
 * THE WIRE CONTRACT. A reply is still one string (`Message.content`; the brain
 * answers `{ reply: string }`), but that string may be a JSON object instead of
 * prose. The object forms the page understands - a widget that stands for the
 * whole reply, a text with one widget, a text with several:
 *
 *   { "type": "widget", "tool": "calendar", "data": { ... } }
 *   { "text": "Here is your day.", "widget": { "tool": "calendar", "data": { ... } } }
 *   { "text": "Two things came back.", "widgets": [ { "tool": "...", "data": ... }, ... ] }
 *   { "widget": "calendar", "data": { ... } }          (the tool's name as the value)
 *
 * A widget may also carry `"title"` (replaces the widget's own title) and
 * `"sample": true` (the data is a demonstration; the page says so). A `widget`
 * object that itself says `"type": "widget"` is fine - the type is not needed.
 * The same fields on the stored message object (`message.widget`,
 * `message.widgets`) work too, for a backend that keeps them apart from the text.
 *
 * What is NOT parsed, on purpose:
 *   - anything a person typed (role "user" is always plain text);
 *   - a reply that is not a bare JSON object (prose, or JSON in a code fence:
 *     the page never guesses that a code sample was meant as a widget);
 *   - a JSON object with none of the shapes above (it stays text).
 * Whatever does not parse as a widget falls back to the text it was, so a
 * backend mistake shows up as visible text rather than as an empty bubble.
 *
 * Pure: no React, no network, never throws.
 */

// A reply bigger than this is prose (or a runaway); do not hand it to JSON.parse.
const MAX_PARSE_CHARS = 200_000;
const MAX_WIDGETS = 6;
const TOOL_NAME = /^[a-z][a-z0-9_]{0,63}$/;
const MAX_TITLE_CHARS = 80;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** "Calendar", "system-status" and "system status" are the same tool: `system_status`. Null for anything that is not a tool name. */
export function normalizeToolName(tool: unknown): string | null {
  if (typeof tool !== "string") return null;
  const name = tool.trim().toLowerCase().replace(/[\s.-]+/g, "_");
  return TOOL_NAME.test(name) ? name : null;
}

/** "system_status" -> "System status": the label for a tool the page has no definition for. */
export function humanizeTool(tool: string): string {
  const words = tool.replace(/_+/g, " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : "Widget";
}

function readWidget(value: unknown): WidgetPayload | null {
  if (!isRecord(value)) return null;
  const tool = normalizeToolName(value.tool);
  if (!tool) return null;

  const widget: WidgetPayload = { tool, data: "data" in value ? value.data : null };
  if (typeof value.title === "string" && value.title.trim()) {
    widget.title = value.title.trim().slice(0, MAX_TITLE_CHARS);
  }
  if (value.sample === true) widget.sample = true;
  return widget;
}

function readText(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

/** The widgets an object asks for, or null when it asks for none or asks wrongly. */
function readWidgets(source: Record<string, unknown>): WidgetPayload[] | null {
  const widgets: WidgetPayload[] = [];

  if (source.type === "widget") {
    const widget = readWidget(source);
    if (!widget) return null;
    widgets.push(widget);
  }
  if (typeof source.widget === "string") {
    // Shorthand: the tool's name is the value and its data sits beside it.
    const widget = readWidget({ tool: source.widget, data: source.data, title: source.title, sample: source.sample });
    if (!widget) return null;
    widgets.push(widget);
  } else if ("widget" in source && source.widget !== undefined && source.widget !== null) {
    const widget = readWidget(source.widget);
    if (!widget) return null;
    widgets.push(widget);
  }
  if (Array.isArray(source.widgets)) {
    for (const item of source.widgets) {
      const widget = readWidget(item);
      if (!widget) return null;
      widgets.push(widget);
    }
  }

  return widgets.length > 0 ? widgets.slice(0, MAX_WIDGETS) : null;
}

function textContent(text: string): MessageContent {
  return { text, widgets: [] };
}

/**
 * What `raw` (a stored message body) says. People's words are always text;
 * Jarvis's may be a widget envelope - see the contract at the top of the file.
 */
export function parseMessageContent(raw: string, role: MessageRole): MessageContent {
  if (role !== "jarvis") return textContent(raw);

  const trimmed = raw.trim();
  if (trimmed.length === 0 || trimmed.length > MAX_PARSE_CHARS) return textContent(raw);
  if (!trimmed.startsWith("{") || !trimmed.endsWith("}")) return textContent(raw);

  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    return textContent(raw);
  }
  if (!isRecord(parsed)) return textContent(raw);

  const widgets = readWidgets(parsed);
  if (!widgets) return textContent(raw);
  return { text: readText(parsed.text), widgets };
}

/** A message as the API sends it. `widget` / `widgets` are the optional structured fields described above. */
export interface ServerMessageLike {
  id: string;
  role: string;
  content: string;
  createdAt?: string | number;
  widget?: unknown;
  widgets?: unknown;
}

function toTime(value: string | number | undefined): number {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  const time = typeof value === "string" ? Date.parse(value) : NaN;
  return Number.isFinite(time) ? time : Date.now();
}

/** A stored message, ready for the feed. Anything that is not "user" is Jarvis. */
export function toChatMessage(message: ServerMessageLike): ChatMessage {
  const role: MessageRole = message.role === "user" ? "user" : "jarvis";
  let content = parseMessageContent(String(message.content ?? ""), role);

  if (role === "jarvis" && (message.widget != null || message.widgets != null)) {
    const extra = readWidgets({ widget: message.widget, widgets: message.widgets });
    if (extra) {
      content = {
        text: content.text,
        widgets: [...content.widgets, ...extra].slice(0, MAX_WIDGETS),
      };
    }
  }

  return { id: String(message.id), role, content, createdAt: toTime(message.createdAt), origin: "server" };
}
