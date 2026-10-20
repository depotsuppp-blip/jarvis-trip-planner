import type { ComponentType, ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import type { ZodType } from "zod";
import type { WidgetPayload } from "@/lib/jarvis/types";

/**
 * How a tool's result gets a view in the chat.
 *
 * A reply can carry `{ tool: "calendar", data: {...} }` (see lib/jarvis/
 * messageContent.ts for the wire contract). `registry.tsx` maps a tool name to
 * one of these definitions; `WidgetHost` looks the tool up, validates the data
 * and draws the view - or a placeholder when there is none, or the data is not
 * what the view needs.
 *
 * To give a tool its own view: write the component, then add one `defineWidget`
 * line to the registry. Nothing else in the hub changes.
 */

/** What a widget component is handed: the payload as sent, and its data AFTER validation. */
export interface WidgetViewProps<T> {
  payload: WidgetPayload;
  data: T;
}

export interface WidgetDefinition {
  /** Canonical tool name, lower-case snake_case: "system_status". */
  tool: string;
  /** What people call it: "System status". Also the placeholder's title. */
  label: string;
  icon: LucideIcon;
  /**
   * Validates `payload.data` and returns the finished view, or null when the data
   * is not what this widget needs. Absent for a tool that is known but has no view
   * yet: it gets the placeholder, with this definition's label and icon.
   */
  render?: (payload: WidgetPayload) => ReactNode | null;
}

/** What `defineWidget` is given: the schema the data must satisfy, and the component that draws it. */
export interface WidgetSpec<T> {
  tool: string;
  label: string;
  icon: LucideIcon;
  schema: ZodType<T>;
  component: ComponentType<WidgetViewProps<T>>;
}
