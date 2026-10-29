"use client";

import Script from "next/script";
import { memo, useCallback, useEffect, useState } from "react";
import { useChat, useOrb, useShell } from "@/lib/jarvis/JarvisProvider";

/**
 * The Signal Orb (public/speaking-orb.js): a vanilla web component, <speaking-orb>,
 * that draws Jarvis as a sphere of WebGL particles - its shape and colour say
 * what Jarvis is doing.
 *
 * Kept away from the server: the script defines the element with `customElements`,
 * which does not exist there. It is loaded with next/script after the page is
 * interactive, so it never blocks the first render; until it arrives the tag is an
 * empty, unknown element, and the browser upgrades it in place when it does.
 *
 * Attributes go on through a ref, not as JSX props: once the element is defined,
 * React 19 assigns a custom element's props as PROPERTIES, and `particles` is a
 * getter with no setter on this element - assigning it throws. `state` has a
 * setter, so it could be a prop, but one path for both is simpler.
 *
 * It is made for a dark surface: its colours are added light (additive blending),
 * which all but vanishes on white. Put it on Ink.
 */

export type JarvisOrbState = "listening" | "thinking" | "searching" | "speaking" | "done";

/** Enough to read as a sphere at sidebar size, light enough for WebGL on any laptop. */
const PARTICLES = "6000";
const SCRIPT_SRC = "/speaking-orb.js";

declare module "react" {
  // eslint-disable-next-line @typescript-eslint/no-namespace
  namespace JSX {
    interface IntrinsicElements {
      /** The Signal Orb web component (public/speaking-orb.js). Set `particles` through a ref - see JarvisOrb. */
      "speaking-orb": React.DetailedHTMLProps<React.HTMLAttributes<HTMLElement>, HTMLElement> & {
        id?: string;
        state?: JarvisOrbState;
        particles?: string;
      };
    }
  }
}

export interface JarvisOrbProps {
  state: JarvisOrbState;
  id?: string;
  className?: string;
}

export const JarvisOrb = memo(function JarvisOrb({ state, id, className }: JarvisOrbProps) {
  const [node, setNode] = useState<HTMLElement | null>(null);
  const attach = useCallback((element: HTMLElement | null) => {
    element?.setAttribute("particles", PARTICLES);
    setNode(element);
  }, []);

  useEffect(() => {
    node?.setAttribute("state", state);
  }, [node, state]);

  return (
    <>
      <Script src={SCRIPT_SRC} strategy="afterInteractive" />
      <speaking-orb ref={attach} id={id} className={className} />
    </>
  );
});

// ------------------------------------------------------------------ the hub's orb

/** md: the width at which the sidebar is a column rather than a drawer (Sidebar.tsx). */
const DESKTOP = "(min-width: 768px)";

function useDesktop(): boolean {
  const [desktop, setDesktop] = useState(false);
  useEffect(() => {
    const query = window.matchMedia(DESKTOP);
    const update = () => setDesktop(query.matches);
    update();
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);
  return desktop;
}

/**
 * The orb's state, from the executive manager (the sidebar's "Jarvis" row): working
 * on a question is "thinking", a reply being read aloud is "speaking", and anything
 * else is "listening" - Jarvis waiting for the next thing to be asked.
 */
export function useManagerOrbState(): JarvisOrbState {
  const working = useChat((state) => state.agents.some((agent) => agent.id === "brain" && agent.state === "working"));
  const speaking = useOrb((state) => state.mode === "speaking");
  if (speaking) return "speaking";
  return working ? "thinking" : "listening";
}

/**
 * Jarvis's face at the top of the sidebar: the Signal Orb on an Ink tile, following
 * the manager. Mounted only while the full sidebar is on screen - the desktop column
 * when expanded, or the phone drawer when open - since a hidden canvas would keep
 * animating. Decorative for assistive tech: the "Jarvis" row under "Agents & tools"
 * says the same thing in words.
 */
export const ManagerOrb = memo(function ManagerOrb() {
  const collapsed = useShell((state) => state.collapsed);
  const drawerOpen = useShell((state) => state.drawerOpen);
  const desktop = useDesktop();
  const state = useManagerOrbState();
  const visible = desktop ? !collapsed : drawerOpen;

  return (
    <div
      aria-hidden="true"
      data-jarvis-orb={state}
      className="relative mx-3 mt-3 h-36 shrink-0 overflow-hidden rounded-md bg-slate-900"
    >
      {visible && (
        // The element is as tall as it is wide plus a caption line it never uses here;
        // this square shows the orb and hides the rest.
        <div className="absolute left-1/2 top-1/2 size-52 -translate-x-1/2 -translate-y-1/2 overflow-hidden">
          <JarvisOrb state={state} className="block w-full" />
        </div>
      )}
    </div>
  );
});
