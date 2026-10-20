"use client";

import { useMemo } from "react";
import type { WidgetPayload } from "@/lib/jarvis/types";
import { getWidgetDefinition } from "./registry";
import { WidgetBoundary } from "./WidgetBoundary";
import { WidgetPlaceholder } from "./WidgetPlaceholder";

/**
 * Draws one widget payload: its own view when the tool has one and the data
 * fits, a placeholder in every other case - unknown tool, known tool without a
 * view, data in the wrong shape, a view that throws.
 */
export function WidgetHost({ payload }: { payload: WidgetPayload }) {
  const definition = getWidgetDefinition(payload.tool);
  // Validated once per payload, not on every render of the feed.
  const view = useMemo(() => definition?.render?.(payload) ?? null, [definition, payload]);

  if (!definition) return <WidgetPlaceholder payload={payload} reason="unknown" />;
  if (!definition.render) return <WidgetPlaceholder payload={payload} definition={definition} reason="not-built" />;
  if (view === null) return <WidgetPlaceholder payload={payload} definition={definition} reason="invalid" />;

  return (
    <WidgetBoundary fallback={<WidgetPlaceholder payload={payload} definition={definition} reason="crashed" />}>
      {view}
    </WidgetBoundary>
  );
}
