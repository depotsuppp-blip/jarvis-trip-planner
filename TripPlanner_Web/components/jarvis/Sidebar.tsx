"use client";

import { memo, useEffect, useRef } from "react";
import { PanelLeftClose, PanelLeftOpen, Plus, X } from "lucide-react";
import { useChatActions, useShell, useShellActions } from "@/lib/jarvis/JarvisProvider";
import { AccountRow } from "./AccountRow";
import { AgentRail, AgentStatusList } from "./AgentStatusList";
import { SessionList } from "./SessionList";
import { TestBench } from "./TestBench";
import { focusRing, iconButton, thinScroll } from "./styles";

/**
 * The left rail of the hub: sessions and history, which agents and tools are
 * doing what, and - folded away at the foot - the test bench and the account.
 *
 * Three shapes of one element:
 *   - desktop, expanded: a 288px column on Desk Mist, ruled from the conversation;
 *   - desktop, collapsed: a 56px rail (new session, an agent dot per agent), the
 *     choice remembered between visits;
 *   - phone: off-canvas. A drawer over the page with a scrim; the page behind it
 *     is inert, Escape or the scrim closes it, and focus goes in and comes back.
 *
 * It re-renders only for layout changes. What it lists comes from components that
 * subscribe to the chat store themselves.
 */
export const Sidebar = memo(function Sidebar() {
  const collapsed = useShell((state) => state.collapsed);
  const drawerOpen = useShell((state) => state.drawerOpen);
  const ready = useShell((state) => state.ready);
  const shell = useShellActions();
  const actions = useChatActions();
  const closeButton = useRef<HTMLButtonElement | null>(null);

  // The phone drawer: Escape closes it; focus moves in when it opens and back to what opened it when it closes.
  useEffect(() => {
    if (!drawerOpen) return;
    closeButton.current?.focus();

    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") shell.closeDrawer();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      shell.restoreFocus();
    };
  }, [drawerOpen, shell]);

  function startNewSession() {
    actions.newSession();
    shell.closeDrawer();
  }

  return (
    <>
      {/* Scrim behind the phone drawer. */}
      <div
        aria-hidden="true"
        onClick={shell.closeDrawer}
        className={`fixed inset-0 z-40 bg-slate-900/40 transition-opacity duration-200 motion-reduce:transition-none md:hidden ${
          drawerOpen ? "opacity-100" : "pointer-events-none opacity-0"
        }`}
      />

      <aside
        id="jarvis-sidebar"
        aria-label="Sessions and agents"
        className={`fixed inset-y-0 left-0 z-50 flex w-[min(20rem,85vw)] flex-col overflow-hidden border-r border-slate-200 bg-slate-50 motion-reduce:transition-none md:visible md:static md:z-auto md:shrink-0 md:translate-x-0 ${
          // Slide and width take 200ms; visibility flips at once when opening (focus can move in) and only after the slide-out when closing.
          ready ? "transition-[transform,width,visibility] ease-out [transition-duration:200ms,200ms,0s]" : ""
        } ${
          drawerOpen
            ? "visible translate-x-0"
            : "invisible -translate-x-full [transition-delay:0s,0s,200ms] md:[transition-delay:0s]"
        } ${collapsed ? "md:w-14" : "md:w-72"}`}
      >
        {/* Full panel: always on a phone, and on desktop unless collapsed. Fixed width, so it is revealed rather than squeezed while the column animates. */}
        <div className={`${collapsed ? "flex md:hidden" : "flex"} h-full w-full flex-col md:w-72`}>
          <div className="flex h-14 shrink-0 items-center justify-between border-b border-slate-200 pl-4 pr-2">
            <span className="text-[17px] font-bold tracking-tight text-slate-900">Jarvis</span>
            <button
              type="button"
              onClick={shell.toggleCollapsed}
              aria-label="Collapse sidebar"
              aria-controls="jarvis-sidebar"
              aria-expanded={!collapsed}
              className={`${iconButton} hidden md:grid`}
            >
              <PanelLeftClose className="size-5" aria-hidden="true" />
            </button>
            <button
              ref={closeButton}
              type="button"
              onClick={shell.closeDrawer}
              aria-label="Close sidebar"
              className={`${iconButton} md:hidden`}
            >
              <X className="size-5" aria-hidden="true" />
            </button>
          </div>

          <div className={`min-h-0 flex-1 overflow-y-auto px-3 py-4 ${thinScroll}`}>
            <button
              type="button"
              onClick={startNewSession}
              className={`flex min-h-10 w-full items-center gap-2 rounded-md border border-slate-300 bg-white px-3 text-sm font-semibold text-slate-900 transition-[border-color,scale] duration-150 hover:border-slate-900 active:scale-[0.98] ${focusRing}`}
            >
              <Plus className="size-4" aria-hidden="true" />
              New session
            </button>

            <div className="mt-6">
              <SessionList onPick={shell.closeDrawer} />
            </div>
            <div className="mt-6">
              <AgentStatusList />
            </div>
          </div>

          <div className="shrink-0 space-y-3 border-t border-slate-200 px-3 py-3">
            <TestBench />
            <AccountRow />
          </div>
        </div>

        {/* Rail: desktop, collapsed. */}
        <div className={`${collapsed ? "hidden md:flex" : "hidden"} h-full w-14 flex-col items-center`}>
          <div className="flex h-14 w-full shrink-0 items-center justify-center border-b border-slate-200">
            <button
              type="button"
              onClick={shell.toggleCollapsed}
              aria-label="Expand sidebar"
              aria-controls="jarvis-sidebar"
              aria-expanded={!collapsed}
              className={iconButton}
            >
              <PanelLeftOpen className="size-5" aria-hidden="true" />
            </button>
          </div>

          <div className="flex min-h-0 flex-1 flex-col items-center gap-3 py-4">
            <button
              type="button"
              onClick={actions.newSession}
              aria-label="New session"
              title="New session"
              className={`grid size-10 place-items-center rounded-md border border-slate-300 bg-white text-slate-900 transition-[border-color,scale] duration-150 hover:border-slate-900 active:scale-[0.97] ${focusRing}`}
            >
              <Plus className="size-5" aria-hidden="true" />
            </button>
            <AgentRail />
          </div>

          <div className="shrink-0 py-3">
            <AccountRow compact />
          </div>
        </div>
      </aside>
    </>
  );
});
