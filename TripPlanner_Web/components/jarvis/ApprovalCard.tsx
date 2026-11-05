"use client";

import { memo, useEffect, useRef, useState } from "react";
import { Check, ShieldAlert, X } from "lucide-react";
import { useChatActions } from "@/lib/jarvis/JarvisProvider";
import type { PendingApproval } from "@/lib/jarvis/types";
import { focusRing } from "./styles";

/** What a gated tool does, in words a person recognises. Anything else is shown by its own name. */
const ACTION_LABELS: Record<string, string> = {
  send_line_notification: "Send a LINE message",
  trigger_n8n_webhook: "Run an n8n automation",
  start_subtrack_env: "Start the Subtrack servers",
  stop_subtrack_env: "Stop the Subtrack servers",
  create_new_tool: "Create a new tool",
};

function actionLabel(tool: string): string {
  return ACTION_LABELS[tool] ?? tool.replace(/_/g, " ");
}

function secondsLeft(expiresAt: number): number {
  return Math.max(0, Math.ceil((expiresAt - Date.now()) / 1000));
}

/**
 * The Approval Card: a tool that changes something outside Jarvis (a LINE message, a server, a new
 * piece of code on this machine) is paused until the person decides. It stands under the thinking
 * bubble (PendingReply) and says what is about to happen, with the arguments exactly as the tool
 * will receive them - for a new tool, its code in full, because approving code you have not read
 * is not consent. Deny and Allow are the only two ways out; no answer is a no.
 *
 * The brain's own words (`prompt`, `details`) are shown as plain text. Focus lands on the card, not
 * on a button, so a stray Enter cannot approve anything; Deny comes first in the tab order.
 * The countdown is the brain's own clock: when it runs out the brain counts the silence as a no
 * and the card goes away by itself.
 */
export const ApprovalCard = memo(function ApprovalCard({ approval }: { approval: PendingApproval }) {
  const actions = useChatActions();
  const cardRef = useRef<HTMLElement>(null);
  const [left, setLeft] = useState(() => secondsLeft(approval.expiresAt));
  const busy = approval.state === "sending";

  useEffect(() => {
    cardRef.current?.focus({ preventScroll: false });
  }, [approval.id]);

  useEffect(() => {
    setLeft(secondsLeft(approval.expiresAt));
    const timer = window.setInterval(() => setLeft(secondsLeft(approval.expiresAt)), 1000);
    return () => window.clearInterval(timer);
  }, [approval.expiresAt]);

  const answer = (decision: "allow" | "deny") => {
    if (!busy) void actions.decideApproval(approval.id, decision);
  };

  return (
    <section
      ref={cardRef}
      tabIndex={-1}
      role="group"
      aria-labelledby={`approval-${approval.id}-title`}
      aria-describedby={`approval-${approval.id}-prompt`}
      data-approval-card=""
      className={`mt-3 w-full max-w-xl overflow-hidden rounded-md border border-slate-300 bg-white outline-none ${focusRing}`}
    >
      <header className="flex h-11 items-center justify-between gap-3 border-b border-slate-200 px-4">
        <h2 id={`approval-${approval.id}-title`} className="flex min-w-0 items-center gap-2 text-sm font-semibold text-slate-900">
          <ShieldAlert className="size-4 shrink-0" aria-hidden="true" />
          <span className="truncate">Approval needed · {actionLabel(approval.tool)}</span>
        </h2>
        <p className="shrink-0 text-xs tabular-nums text-slate-600">{left > 0 ? `${left} s left` : "Out of time"}</p>
      </header>

      <div className="px-4 py-4">
        <p id={`approval-${approval.id}-prompt`} className="text-[15px] leading-6 text-slate-900">
          {approval.prompt}
        </p>

        {approval.details.length > 0 && (
          <dl className="mt-3 divide-y divide-slate-200 border-y border-slate-200">
            {approval.details.map((detail, index) => (
              <div key={`${index}:${detail.name}`} className="py-2.5">
                <dt className="text-xs font-semibold text-slate-600">{detail.name}</dt>
                <dd className="mt-1">
                  <pre className="max-h-56 overflow-auto whitespace-pre-wrap break-words font-mono text-[13px] leading-5 text-slate-900">
                    {detail.value}
                  </pre>
                </dd>
              </div>
            ))}
          </dl>
        )}

        {approval.error && (
          <p role="alert" className="mt-3 text-xs font-medium text-red-700">
            {approval.error}
          </p>
        )}
      </div>

      <footer className="flex flex-wrap items-center justify-between gap-3 border-t border-slate-200 bg-slate-50 px-4 py-2.5">
        <p className="text-xs text-slate-600">
          {busy ? "Sending your answer…" : "If you don't answer, Jarvis treats it as a no."}
        </p>
        <div className="flex items-center gap-2">
          <button
            type="button"
            disabled={busy}
            onClick={() => answer("deny")}
            className={`inline-flex min-h-9 items-center gap-1.5 rounded-md border border-slate-300 bg-white px-3 text-[13px] font-medium text-slate-900 transition-[background-color,scale] duration-150 hover:bg-slate-100 active:scale-[0.97] disabled:opacity-50 ${focusRing}`}
          >
            <X className="size-3.5" aria-hidden="true" />
            Deny
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => answer("allow")}
            className={`inline-flex min-h-9 items-center gap-1.5 rounded-md bg-slate-900 px-3 text-[13px] font-medium text-white transition-[background-color,scale] duration-150 hover:bg-slate-700 active:scale-[0.97] disabled:opacity-50 ${focusRing}`}
          >
            <Check className="size-3.5" aria-hidden="true" />
            Allow
          </button>
        </div>
      </footer>
    </section>
  );
});
