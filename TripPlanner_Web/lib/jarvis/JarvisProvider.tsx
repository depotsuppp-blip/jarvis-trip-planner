"use client";

import { createContext, createRef, useContext, useEffect, useState, type ReactNode, type RefObject } from "react";
import { ChatController, type ChatActions } from "./chatController";
import { createShellStore, type ShellState, type ShellStore } from "./shell";
import { useStore } from "./store";
import type { ChatState, OrbState } from "./types";

/**
 * Hands the hub's three long-lived things to the components below it: the
 * controller (chat + orb state and every action), the shell (layout state) and
 * the two DOM anchors the orb is placed from.
 *
 * The context value is created once and never changes, so reading it re-renders
 * nobody. Components get data through the selector hooks below, which subscribe
 * to one slice of a store - see store.ts for why.
 */

interface JarvisContextValue {
  controller: ChatController;
  shell: ShellStore;
  /**
   * Where the orb sits when idle: `slot` is the empty box inside the composer that
   * the small orb floats over. Set by the Composer, read by OrbStage - a ref, never
   * state, so moving it re-renders nothing. (The stage the orb centres itself on is
   * found from the slot, by `data-jarvis-stage`: see OrbStage.)
   */
  anchors: { slot: RefObject<HTMLDivElement | null> };
}

const JarvisContext = createContext<JarvisContextValue | null>(null);

export function JarvisProvider({ children }: { children: ReactNode }) {
  const [value] = useState<JarvisContextValue>(() => ({
    controller: new ChatController(),
    shell: createShellStore(),
    anchors: { slot: createRef<HTMLDivElement>() },
  }));

  useEffect(() => {
    value.controller.start();
    return () => value.controller.dispose();
  }, [value]);

  return <JarvisContext value={value}>{children}</JarvisContext>;
}

function useJarvis(): JarvisContextValue {
  const value = useContext(JarvisContext);
  if (!value) throw new Error("useJarvis must be used inside <JarvisProvider>.");
  return value;
}

/** One slice of the chat state. The selector must return a stable value - see store.ts. */
export function useChat<S>(select: (state: ChatState) => S): S {
  return useStore(useJarvis().controller.chat, select);
}

/** One slice of the orb state (its mode, whether voice mode is on). */
export function useOrb<S>(select: (state: OrbState) => S): S {
  return useStore(useJarvis().controller.orb, select);
}

/** One slice of the layout state (sidebar collapsed, drawer open). */
export function useShell<S>(select: (state: ShellState) => S): S {
  return useStore(useJarvis().shell, select);
}

/** The things a component can ask the chat to do. Stable: never causes a re-render. */
export function useChatActions(): ChatActions {
  return useJarvis().controller.actions;
}

/** The shell's own actions (collapse, open and close the drawer). Stable. */
export function useShellActions(): ShellStore {
  return useJarvis().shell;
}

/** The orb's home anchor; read its `.current` only in effects and event handlers. */
export function useOrbAnchors(): JarvisContextValue["anchors"] {
  return useJarvis().anchors;
}
