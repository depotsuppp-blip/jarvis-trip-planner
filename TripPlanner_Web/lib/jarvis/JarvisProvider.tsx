"use client";

import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { ChatController, type ChatActions } from "./chatController";
import { createShellStore, type ShellState, type ShellStore } from "./shell";
import { useStore } from "./store";
import type { ChatState, OrbState, SpeechState } from "./types";

/**
 * Hands the hub's two long-lived things to the components below it: the
 * controller (chat + orb state and every action) and the shell (layout state).
 *
 * The context value is created once and never changes, so reading it re-renders
 * nobody. Components get data through the selector hooks below, which subscribe
 * to one slice of a store - see store.ts for why.
 */

interface JarvisContextValue {
  controller: ChatController;
  shell: ShellStore;
}

const JarvisContext = createContext<JarvisContextValue | null>(null);

export function JarvisProvider({ children }: { children: ReactNode }) {
  const [value] = useState<JarvisContextValue>(() => ({
    controller: new ChatController(),
    shell: createShellStore(),
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

/** One slice of the voice-replies state (whether Jarvis's short replies are read aloud). */
export function useSpeech<S>(select: (state: SpeechState) => S): S {
  return useStore(useJarvis().controller.speech, select);
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
