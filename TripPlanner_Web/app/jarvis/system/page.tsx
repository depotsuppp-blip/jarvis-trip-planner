import type { Metadata } from "next";
import { SystemView } from "@/components/jarvis/system/SystemView";

export const metadata: Metadata = { title: "Jarvis System Topology" };

/**
 * Jarvis's system topology (/jarvis/system), opened from the hub's sidebar: every agent, tool
 * server and built-in tool Jarvis has, with its live status and what to ask it. A server
 * component that only mounts the client view (components/jarvis/system); the 3D scene itself
 * loads in the browser only.
 */
export default function JarvisSystemPage() {
  return <SystemView />;
}
