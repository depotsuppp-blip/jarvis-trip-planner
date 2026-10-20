import { Activity, CalendarDays } from "lucide-react";
import { SystemStatusWidget, systemStatusSchema } from "./SystemStatusWidget";
import type { WidgetDefinition, WidgetSpec } from "./types";

/**
 * Which tools have a view, and which have only a name.
 *
 * Adding a tool's view is one line here: `defineWidget({ tool, label, icon,
 * schema, component })`. The schema is the contract for the tool's `data`: a
 * payload that does not satisfy it gets the placeholder instead of a component
 * fed something it did not expect (the backend is a model; its JSON is not
 * trusted). A heavy view can be `lazy()` loaded inside its own component file
 * without touching this one.
 *
 * A tool listed WITHOUT a spec is known but not built: its payload is drawn as the
 * placeholder, with the right label and icon. `calendar` is one - the example the
 * rich-message path was specified with.
 */

function defineWidget<T>(spec: WidgetSpec<T>): WidgetDefinition {
  const Component = spec.component;
  return {
    tool: spec.tool,
    label: spec.label,
    icon: spec.icon,
    render(payload) {
      const parsed = spec.schema.safeParse(payload.data);
      return parsed.success ? <Component payload={payload} data={parsed.data} /> : null;
    },
  };
}

const DEFINITIONS: WidgetDefinition[] = [
  defineWidget({
    tool: "system_status",
    label: "System status",
    icon: Activity,
    schema: systemStatusSchema,
    component: SystemStatusWidget,
  }),
  { tool: "calendar", label: "Calendar", icon: CalendarDays },
];

const BY_TOOL = new Map(DEFINITIONS.map((definition) => [definition.tool, definition]));

/** The definition for a canonical tool name, or undefined for a tool this page has never heard of. */
export function getWidgetDefinition(tool: string): WidgetDefinition | undefined {
  return BY_TOOL.get(tool);
}
