import type { Metadata } from "next";
import { JarvisHub } from "@/components/jarvis/JarvisHub";

export const metadata: Metadata = { title: "Jarvis" };

/**
 * The Jarvis hub (/jarvis). The page itself is a server component that only
 * mounts the client hub; the chat, the sessions and the orb live in
 * components/jarvis (the screen) and lib/jarvis (the state behind it).
 */
export default function JarvisPage() {
  return <JarvisHub />;
}
