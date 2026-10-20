import type { ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import type { WidgetPayload } from "@/lib/jarvis/types";
import { SampleBadge } from "./SampleBadge";

/**
 * The one chrome every widget shares: a ruled block with an icon-and-title bar,
 * the widget's own body, and an optional quiet footer. Same shape for a real
 * view and for a placeholder, so a tool that gains a real view later does not
 * change the rhythm of the conversation around it.
 *
 * Ruled, not carded: a 1px hairline and a 6px corner, no shadow - the same
 * Editorial dialect as the rest of the hub (DESIGN.md, "Jarvis Hub").
 */
export function WidgetFrame({
  icon: Icon,
  title,
  payload,
  footer,
  children,
}: {
  icon: LucideIcon;
  title: string;
  payload: WidgetPayload;
  footer?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section
      aria-label={`${title} widget`}
      className="w-full max-w-xl overflow-hidden rounded-md border border-slate-200 bg-white"
    >
      <header className="flex h-11 items-center justify-between gap-3 border-b border-slate-200 px-4">
        <h2 className="flex min-w-0 items-center gap-2 text-sm font-semibold text-slate-900">
          <Icon className="size-4 shrink-0" aria-hidden="true" />
          <span className="truncate">{title}</span>
        </h2>
        {payload.sample && <SampleBadge />}
      </header>
      <div className="px-4 py-4">{children}</div>
      {footer && <footer className="border-t border-slate-200 bg-slate-50 px-4 py-2.5 text-xs text-slate-600">{footer}</footer>}
    </section>
  );
}
