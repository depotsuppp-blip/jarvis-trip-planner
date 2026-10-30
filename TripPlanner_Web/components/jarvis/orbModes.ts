import type { OrbMode } from "@/lib/jarvis/types";

/**
 * How the hub's four states are spoken of. Shared by the top bar's status word,
 * the Signal Orb's caption in the sidebar (JarvisOrb.tsx) and the test bench.
 */

export const ORB_MODES: readonly OrbMode[] = ["idle", "listening", "thinking", "speaking"];

export const ORB_LABEL: Record<OrbMode, string> = {
  idle: "Idle",
  listening: "Listening",
  thinking: "Thinking",
  speaking: "Speaking",
};
