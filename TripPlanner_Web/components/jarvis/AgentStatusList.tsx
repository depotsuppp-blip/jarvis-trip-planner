"use client";

import { memo } from "react";
import { useChat } from "@/lib/jarvis/JarvisProvider";
import type { AgentState, AgentStatus } from "@/lib/jarvis/types";

/**
 * Which agents and tools Jarvis has, and how each is doing, in the sidebar.
 * `AgentStatusList` is the full list (a dot, a name, a word, and a line of why
 * when there is one); `AgentRail` is the same information as bare dots for the
 * collapsed sidebar, each still named for a screen reader.
 *
 * State is never colour alone: every state has its word, and the dots that
 * carry no information on their own (unknown, unavailable) are hollow.
 */

const STATE_WORD: Record<AgentState, string> = {
  unknown: "Not checked",
  idle: "Idle",
  working: "Working",
  ready: "Ready",
  offline: "Offline",
  unavailable: "Not connected",
};

// Go Green for a run that worked, Alert Red for offline, Ink for work in progress (it pulses);
// slate for everything that is merely not happening.
const DOT: Record<AgentState, string> = {
  unknown: "border border-slate-500",
  idle: "bg-slate-500",
  working: "bg-slate-900 motion-safe:animate-pulse",
  ready: "bg-emerald-700",
  offline: "bg-red-700",
  unavailable: "border border-slate-500",
};

function Dot({ state }: { state: AgentState }) {
  return <span aria-hidden="true" className={`block size-2 shrink-0 rounded-full ${DOT[state]}`} />;
}

function useAgents(): AgentStatus[] {
  return useChat((state) => state.agents);
}

export const AgentStatusList = memo(function AgentStatusList() {
  const agents = useAgents();

  return (
    <section aria-labelledby="jarvis-agents-heading">
      <h2 id="jarvis-agents-heading" className="px-2 text-[13px] font-semibold text-slate-800">
        Agents &amp; tools
      </h2>
      <ul className="mt-1">
        {agents.map((agent) => (
          <li key={agent.id} className="flex items-start gap-2.5 px-2 py-2">
            <div className="mt-[7px]">
              <Dot state={agent.state} />
            </div>
            <div className="min-w-0 flex-1">
              <p className="flex items-baseline justify-between gap-2 text-sm">
                <span className="truncate font-medium text-slate-900">{agent.name}</span>
                <span className="shrink-0 text-xs text-slate-600">{STATE_WORD[agent.state]}</span>
              </p>
              {agent.detail && <p className="mt-0.5 text-xs leading-4 text-slate-600">{agent.detail}</p>}
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
});

export const AgentRail = memo(function AgentRail() {
  const agents = useAgents();

  return (
    <ul aria-label="Agents and tools" className="flex flex-col items-center gap-3 py-2">
      {agents.map((agent) => (
        <li key={agent.id} title={`${agent.name}: ${STATE_WORD[agent.state]}`}>
          <span className="sr-only">{`${agent.name}: ${STATE_WORD[agent.state]}`}</span>
          <Dot state={agent.state} />
        </li>
      ))}
    </ul>
  );
});
