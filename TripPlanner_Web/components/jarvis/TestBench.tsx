"use client";

import { memo } from "react";
import { ChevronDown, FlaskConical } from "lucide-react";
import { useChatActions, useOrb } from "@/lib/jarvis/JarvisProvider";
import { DEMO_COMMANDS } from "@/lib/jarvis/demo";
import { ORB_LABEL, ORB_MODES } from "./orbModes";
import { focusRing } from "./styles";

/**
 * Two things to try by hand, since the microphone and a widget-sending backend
 * are not wired up yet: jump the orb to any state (to watch its animations and
 * its move), and have Jarvis "reply" with a made-up widget (to see the rich
 * message path - the replies are marked Sample). Typing `/status` does the same
 * as the first widget button.
 *
 * Folded away by default: it is a workbench, not part of the assistant.
 */

const chip = `min-h-9 rounded-md border border-slate-300 bg-white px-2.5 text-[13px] font-medium text-slate-800 transition-[border-color,background-color,color,scale] duration-150 hover:border-slate-900 active:scale-[0.97] aria-pressed:border-slate-900 aria-pressed:bg-slate-900 aria-pressed:text-white ${focusRing}`;

export const TestBench = memo(function TestBench() {
  const mode = useOrb((state) => state.mode);
  const actions = useChatActions();

  return (
    <details className="group rounded-md border border-slate-200 bg-white">
      <summary
        className={`flex min-h-10 cursor-pointer list-none items-center gap-2 rounded-md px-3 text-[13px] font-semibold text-slate-800 [&::-webkit-details-marker]:hidden ${focusRing}`}
      >
        <FlaskConical className="size-4 shrink-0" aria-hidden="true" />
        <span className="flex-1">Test bench</span>
        <ChevronDown
          className="size-4 shrink-0 text-slate-600 transition-transform duration-150 group-open:rotate-180 motion-reduce:transition-none"
          aria-hidden="true"
        />
      </summary>

      <div className="space-y-3 border-t border-slate-200 px-3 py-3">
        <div role="group" aria-label="Orb state">
          <p className="text-xs font-medium text-slate-600">Orb state</p>
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            {ORB_MODES.map((candidate) => (
              <button
                key={candidate}
                type="button"
                onClick={() => actions.forceOrb(candidate)}
                aria-pressed={mode === candidate}
                className={chip}
              >
                {ORB_LABEL[candidate]}
              </button>
            ))}
          </div>
        </div>

        <div role="group" aria-label="Reply with a widget">
          <p className="text-xs font-medium text-slate-600">Reply with a widget</p>
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            {DEMO_COMMANDS.map((entry) => (
              <button key={entry.tool} type="button" onClick={() => actions.previewWidget(entry.tool)} className={chip}>
                {entry.label}
              </button>
            ))}
          </div>
        </div>
      </div>
    </details>
  );
});
