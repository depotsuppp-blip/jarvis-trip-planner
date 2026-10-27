"use client";

import { Fragment, memo, useId } from "react";
import { useChat } from "@/lib/jarvis/JarvisProvider";
import type { AgentKind, AgentState, AgentStatus } from "@/lib/jarvis/types";

/**
 * Who Jarvis is made of, and how each part is doing, in the sidebar - in three
 * groups (lib/jarvis/tools.ts builds the rows):
 *
 *   Executive manager   Jarvis itself, and the model it is on
 *   Specialist agents   the agents it delegates to, each on its model
 *   Tools               Trip Planner, Voice and the tool servers
 *
 * `AgentStatusList` is the full list (a dot, a name, a word, a line of what it
 * does or why, and the model line); `AgentRail` is the same information as bare
 * dots for the collapsed sidebar, the groups apart by a hairline, each dot still
 * named for a screen reader. A group with no rows is not drawn.
 *
 * State is never colour alone: every state has its word, and the dots that
 * carry no information on their own (unknown, unavailable) are hollow.
 */

const STATE_WORD: Record<AgentState, string> = {
  unknown: "Not checked",
  idle: "Idle",
  working: "Working",
  ready: "Ready",
  degraded: "On fallback",
  offline: "Offline",
  unavailable: "Not connected",
};

// Go Green for working as intended, Alert Red for offline, Ink for work in progress (it
// pulses) and for running on a fallback (an ink ring: still answering, not as built);
// slate for everything that is merely not happening.
const DOT: Record<AgentState, string> = {
  unknown: "border border-slate-500",
  idle: "bg-slate-500",
  working: "bg-slate-900 motion-safe:animate-pulse",
  ready: "bg-emerald-700",
  degraded: "border-2 border-slate-900",
  offline: "bg-red-700",
  unavailable: "border border-slate-500",
};

const GROUPS: { title: string; kinds: AgentKind[] }[] = [
  { title: "Executive manager", kinds: ["brain"] },
  { title: "Specialist agents", kinds: ["agent"] },
  { title: "Tools", kinds: ["tool", "voice"] },
];

function Dot({ state }: { state: AgentState }) {
  return <span aria-hidden="true" className={`block size-2 shrink-0 rounded-full ${DOT[state]}`} />;
}

function useAgents(): AgentStatus[] {
  return useChat((state) => state.agents);
}

function grouped(agents: AgentStatus[]) {
  return GROUPS.map((group) => ({ ...group, rows: agents.filter((agent) => group.kinds.includes(agent.kind)) })).filter(
    (group) => group.rows.length > 0
  );
}

/** What a row says in one breath, for hover text and the rail's screen-reader label. */
function summary(agent: AgentStatus): string {
  return [`${agent.name}: ${STATE_WORD[agent.state]}`, agent.model, agent.routes && `Model order: ${agent.routes}`]
    .filter(Boolean)
    .join(". ");
}

function AgentRow({ agent }: { agent: AgentStatus }) {
  return (
    <li className="flex items-start gap-2.5 px-2 py-2" title={agent.routes ? `Model order: ${agent.routes}` : undefined}>
      <div className="mt-[7px]">
        <Dot state={agent.state} />
      </div>
      <div className="min-w-0 flex-1">
        <p className="flex items-baseline justify-between gap-2 text-sm">
          <span className="truncate font-medium text-slate-900">{agent.name}</span>
          <span className="shrink-0 text-xs text-slate-600">{STATE_WORD[agent.state]}</span>
        </p>
        {agent.detail && <p className="mt-0.5 text-xs leading-4 text-slate-600">{agent.detail}</p>}
        {agent.model && <p className="mt-0.5 text-xs leading-4 text-slate-800 [overflow-wrap:anywhere]">{agent.model}</p>}
      </div>
    </li>
  );
}

export const AgentStatusList = memo(function AgentStatusList() {
  const agents = useAgents();
  const id = useId();

  return (
    <section aria-labelledby={`${id}-heading`}>
      <h2 id={`${id}-heading`} className="px-2 text-[13px] font-semibold text-slate-800">
        Agents &amp; tools
      </h2>
      {grouped(agents).map((group, index) => (
        <div key={group.title} role="group" aria-labelledby={`${id}-${index}`}>
          <h3 id={`${id}-${index}`} className="mt-3 px-2 pb-1 text-xs font-medium text-slate-600">
            {group.title}
          </h3>
          <ul>
            {group.rows.map((agent) => (
              <AgentRow key={agent.id} agent={agent} />
            ))}
          </ul>
        </div>
      ))}
    </section>
  );
});

export const AgentRail = memo(function AgentRail() {
  const agents = useAgents();

  return (
    <div aria-label="Agents and tools" role="group" className="flex flex-col items-center gap-3 py-2">
      {grouped(agents).map((group, index) => (
        <Fragment key={group.title}>
          {index > 0 && <span aria-hidden="true" className="block h-px w-5 bg-slate-300" />}
          <ul aria-label={group.title} className="flex flex-col items-center gap-3">
            {group.rows.map((agent) => (
              <li key={agent.id} title={summary(agent)}>
                <span className="sr-only">{summary(agent)}</span>
                <Dot state={agent.state} />
              </li>
            ))}
          </ul>
        </Fragment>
      ))}
    </div>
  );
});
