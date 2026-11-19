import type { Metadata } from "next";
import { MemoryView } from "@/components/jarvis/memory/MemoryView";

export const metadata: Metadata = { title: "Jarvis Memory" };

/**
 * Jarvis's memory (/jarvis/memory), opened from the hub's sidebar: the knowledge graph
 * Jarvis keeps about the signed-in person, to search, inspect and correct. A server
 * component that only mounts the client view (components/jarvis/memory); the graph
 * canvas itself loads in the browser only.
 */
export default function JarvisMemoryPage() {
  return <MemoryView />;
}
