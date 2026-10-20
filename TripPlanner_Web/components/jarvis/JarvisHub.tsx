"use client";

import { useEffect } from "react";
import { JarvisProvider, useShell, useShellActions } from "@/lib/jarvis/JarvisProvider";
import { ChatFeed } from "./ChatFeed";
import { Composer } from "./Composer";
import { OrbStage } from "./OrbStage";
import { Sidebar } from "./Sidebar";
import { TopBar } from "./TopBar";

/**
 * The Jarvis hub: an agentic dashboard in the Modern Editorial dialect - a
 * sidebar for sessions and agents beside one conversation column, with the orb
 * and the composer anchored at the bottom centre of that column.
 *
 *   JarvisProvider   chat + orb state and every action (lib/jarvis), outside React
 *   Sidebar          sessions, agents and tools, test bench, account
 *   stage
 *     TopBar         session title, what Jarvis is doing
 *     ChatFeed       the conversation: text and widgets (DynamicRenderer)
 *     Composer       the dock: orb home, field, Send, Talk
 *     OrbStage       the orb, its frost and caption
 *
 * This file only lays them out and owns nothing that changes while someone chats:
 * each part subscribes to the slice it draws, so a reply, a keystroke or an orb
 * state change re-renders that part and no other.
 */
export function JarvisHub() {
  return (
    <JarvisProvider>
      <HubLayout />
    </JarvisProvider>
  );
}

function HubLayout() {
  const drawerOpen = useShell((state) => state.drawerOpen);
  const shell = useShellActions();

  // Apply the remembered sidebar choice after the first paint.
  useEffect(() => shell.hydrate(), [shell]);

  // The drawer is a phone thing: when the window grows past the breakpoint it closes, so the page behind it is never left inert on a desktop.
  useEffect(() => {
    const desktop = window.matchMedia("(min-width: 768px)");
    const onChange = () => {
      if (desktop.matches) shell.closeDrawer();
    };
    desktop.addEventListener("change", onChange);
    return () => desktop.removeEventListener("change", onChange);
  }, [shell]);

  return (
    // The outer wrapper only carries the selection colours (white on Deep Signal Rose); ::selection reaches every descendant.
    <div className="selection:bg-rose-600 selection:text-white">
      <div className="flex h-svh overflow-hidden bg-white text-slate-900">
        <Sidebar />

        {/* The stage (OrbStage finds it by this attribute). No transform, filter or contain here: the orb is `fixed` and must stay positioned from the window. */}
        <div data-jarvis-stage="" inert={drawerOpen} className="relative isolate flex min-w-0 flex-1 flex-col">
          <TopBar />
          <ChatFeed />
          <Composer />
          <OrbStage />
        </div>
      </div>
    </div>
  );
}
