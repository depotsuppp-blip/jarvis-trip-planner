import { Activity, CircleCheck, CircleX, TriangleAlert } from "lucide-react";
import { z } from "zod";
import { formatMessageTime } from "@/lib/jarvis/sessions";
import { WidgetFrame } from "./WidgetFrame";
import type { WidgetViewProps } from "./types";

/**
 * `system_status`: how the systems behind Jarvis are doing, one row each. The
 * demonstration widget for the rich-message path: a reply that is
 *
 *   { "type": "widget", "tool": "system_status", "data": { "services": [...] } }
 *
 * is drawn as this instead of as text. The test bench and `/status` send a
 * sample (it carries `sample: true`, so the frame says so); a real tool would
 * send the same shape.
 *
 * Status is told three ways, never by colour alone: a different icon, a word,
 * and colour - Go Green, Ink, Alert Red (the design system has no warning
 * colour, and amber is reserved for the rating star).
 */

export const systemStatusSchema = z.object({
  /** ISO time the numbers were taken. */
  updatedAt: z.string().optional(),
  services: z
    .array(
      z.object({
        name: z.string().min(1).max(80),
        status: z.enum(["operational", "degraded", "down"]),
        latencyMs: z.number().nonnegative().optional(),
        uptimePct: z.number().min(0).max(100).optional(),
        note: z.string().max(160).optional(),
      })
    )
    .min(1)
    .max(12),
});

export type SystemStatusData = z.infer<typeof systemStatusSchema>;

const STATUS = {
  operational: { label: "Operational", Icon: CircleCheck, tone: "text-emerald-700" },
  degraded: { label: "Degraded", Icon: TriangleAlert, tone: "text-slate-900" },
  down: { label: "Down", Icon: CircleX, tone: "text-red-700" },
} as const;

const percent = new Intl.NumberFormat("en", { maximumFractionDigits: 2 });

function headline(services: SystemStatusData["services"]): string {
  const attention = services.filter((service) => service.status !== "operational").length;
  if (attention === 0) return "All systems operational";
  return `${attention} of ${services.length} ${services.length === 1 ? "system needs" : "systems need"} attention`;
}

export function SystemStatusWidget({ payload, data }: WidgetViewProps<SystemStatusData>) {
  const taken = data.updatedAt ? Date.parse(data.updatedAt) : NaN;

  return (
    <WidgetFrame
      icon={Activity}
      title={payload.title ?? "System status"}
      payload={payload}
      footer={payload.sample ? "Sample data: a real tool result would replace these numbers." : undefined}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <p className="text-[17px] font-bold tracking-tight text-slate-900">{headline(data.services)}</p>
        {Number.isFinite(taken) && <p className="text-xs tabular-nums text-slate-600">As of {formatMessageTime(taken)}</p>}
      </div>

      <ul className="mt-3 divide-y divide-slate-200 border-y border-slate-200">
        {data.services.map((service, index) => {
          const { label, Icon, tone } = STATUS[service.status];
          const metrics = [
            service.latencyMs !== undefined ? `${Math.round(service.latencyMs)} ms` : null,
            service.uptimePct !== undefined ? `${percent.format(service.uptimePct)}% uptime` : null,
          ].filter(Boolean);

          return (
            <li key={`${index}:${service.name}`} className="flex items-start gap-3 py-3">
              <Icon className={`mt-0.5 size-4 shrink-0 ${tone}`} aria-hidden="true" />
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium text-slate-900">{service.name}</p>
                {metrics.length > 0 && <p className="mt-0.5 text-xs tabular-nums text-slate-600">{metrics.join(" · ")}</p>}
                {service.note && <p className="mt-0.5 text-xs text-slate-600">{service.note}</p>}
              </div>
              <p className={`shrink-0 text-xs font-semibold ${tone}`}>{label}</p>
            </li>
          );
        })}
      </ul>
    </WidgetFrame>
  );
}
