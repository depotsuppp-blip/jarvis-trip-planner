import type { OrbMode } from "@/lib/jarvis/types";

/**
 * How the orb's four states are spoken of and drawn. Shared by the orb stage,
 * the top bar's status word and the test bench.
 */

export const ORB_MODES: readonly OrbMode[] = ["idle", "listening", "thinking", "speaking"];

export const ORB_LABEL: Record<OrbMode, string> = {
  idle: "Idle",
  listening: "Listening",
  thinking: "Thinking",
  speaking: "Speaking",
};

// Our four states -> thinking-orbs' nine animations.
export const ORB_ANIMATION = {
  idle: "breathing",
  listening: "listening",
  thinking: "working",
  speaking: "composing",
} as const;

// One orb that travels between its homes instead of two that swap. It is drawn
// at its real pixel size in each home, never a small orb enlarged with CSS
// scale() (which blurs it) - see Orb.tsx.
export const ORB_SMALL_PX = 36;
export const ORB_FOCAL_PX = 224;
