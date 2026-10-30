"use client";

import Script from "next/script";
import { memo, useCallback, useEffect, useState } from "react";
import { useOrb, useShell } from "@/lib/jarvis/JarvisProvider";
import type { OrbMode } from "@/lib/jarvis/types";
import { ORB_LABEL } from "./orbModes";

/**
 * The Signal Orb (public/speaking-orb.js): a vanilla web component, <speaking-orb>,
 * that draws Jarvis as a sphere of WebGL particles - its shape and colour say
 * what Jarvis is doing. It is Jarvis's only face in the hub (ManagerOrb, below).
 *
 * Kept away from the server: the script defines the element with `customElements`,
 * which does not exist there. It is loaded with next/script after the page is
 * interactive, so it never blocks the first render; until it arrives the tag is an
 * empty, unknown element, and the browser upgrades it in place when it does.
 *
 * Attributes go on through a ref, not as JSX props: once the element is defined,
 * React 19 assigns a custom element's props as PROPERTIES, and `particles` is a
 * getter with no setter on this element - assigning it throws. One path for all.
 *
 * It is made of added light on a transparent canvas (its colours add up, they do not
 * cover), which all but vanishes on white - it needs a dark surface behind it.
 */

export type JarvisOrbState = "listening" | "thinking" | "searching" | "speaking" | "done";

/**
 * The script sizes each particle from the canvas size and the particle count: at sidebar
 * size its own defaults (6000-12000, for a full-screen orb) make dots under 2px that its
 * shader dims to a quarter, a dust that read as an empty black tile. Fewer, larger dots
 * at this size draw the sphere clearly (measured on an Intel Arc GPU and in SwiftShader).
 */
const PARTICLES = "2600";
const SCRIPT_SRC = "/speaking-orb.js";

declare module "react" {
  // eslint-disable-next-line @typescript-eslint/no-namespace
  namespace JSX {
    interface IntrinsicElements {
      /** The Signal Orb web component (public/speaking-orb.js). Set its attributes through a ref - see JarvisOrb. */
      "speaking-orb": React.DetailedHTMLProps<React.HTMLAttributes<HTMLElement>, HTMLElement> & {
        id?: string;
        state?: JarvisOrbState;
        particles?: string;
        level?: string;
      };
    }
  }
}

export interface JarvisOrbProps {
  state: JarvisOrbState;
  /** 0-1: how lively the "listening" surface ripples (the script's own `level`). */
  level?: number;
  id?: string;
  className?: string;
}

export const JarvisOrb = memo(function JarvisOrb({ state, level = 0.5, id, className }: JarvisOrbProps) {
  const [node, setNode] = useState<HTMLElement | null>(null);
  const attach = useCallback((element: HTMLElement | null) => {
    element?.setAttribute("particles", PARTICLES);
    setNode(element);
  }, []);

  useEffect(() => {
    node?.setAttribute("state", state);
  }, [node, state]);

  useEffect(() => {
    node?.setAttribute("level", String(level));
  }, [node, level]);

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

/** What the orb shows for one of the hub's states, and the word and dot under it. */
interface OrbView {
  state: JarvisOrbState;
  level: number;
  label: string;
  /** The dot beside the word: the orb's own colour for that state (speaking-orb.js `colors`). */
  dot: string;
}

const VIEWS: Record<OrbMode, OrbView> = {
  // Nothing in progress: the listening sphere, barely rippling, under a quiet grey dot.
  idle: { state: "listening", level: 0.15, label: ORB_LABEL.idle, dot: "bg-slate-500" },
  // Recording: the same sphere, its surface rippling hard.
  listening: { state: "listening", level: 1, label: ORB_LABEL.listening, dot: "bg-[#ffb969]" },
  thinking: { state: "thinking", level: 0.5, label: ORB_LABEL.thinking, dot: "bg-[#b297ff]" },
  speaking: { state: "speaking", level: 0.5, label: ORB_LABEL.speaking, dot: "bg-[#ff96d6]" },
};

/**
 * The hub's state as the orb shows it: exactly the turn in progress (lib/jarvis:
 * recording is "listening", waiting for the answer is "thinking", a reply read aloud
 * is "speaking"). Not the sidebar's "Jarvis" row: that stays "working" until a
 * cancelled request comes back from the server, and the face must stop thinking the
 * moment the person presses Cancel.
 */
export function useManagerOrbMode(): OrbMode {
  return useOrb((state) => state.mode);
}

/**
 * Jarvis's face at the top of the sidebar: the Signal Orb in a dark glass panel,
 * following every turn, with the state in words at its foot. Mounted only while the
 * full sidebar is on screen - the desktop column when expanded, or the phone drawer
 * when open - since a hidden canvas would keep animating; the panel itself always
 * keeps its place. Decorative for assistive tech: the top bar and the "Jarvis" row
 * under "Agents & tools" say the same thing in words.
 */
export const ManagerOrb = memo(function ManagerOrb() {
  const collapsed = useShell((state) => state.collapsed);
  const drawerOpen = useShell((state) => state.drawerOpen);
  const desktop = useDesktop();
  const mode = useManagerOrbMode();
  const view = VIEWS[mode];
  const visible = desktop ? !collapsed : drawerOpen;

  return (
    <div
      aria-hidden="true"
      data-jarvis-orb={view.state}
      data-jarvis-orb-mode={mode}
      className="relative mx-3 mt-3 h-[200px] shrink-0 overflow-hidden rounded-md bg-[radial-gradient(closest-side_at_50%_45%,#1a2438_0%,#0f172b_60%,#020618_100%)] shadow-[inset_0_0_0_1px_rgb(144_161_185/0.22),inset_0_1px_0_rgb(255_255_255/0.06)]"
    >
      {visible && (
        // The element is a square plus a caption line it never uses here: the square is
        // centred a little above the middle (the state line sits under it) and the rest is
        // clipped. 270px puts the sphere at about 150px across in the 200px panel.
        <div className="absolute left-1/2 top-[45%] size-[270px] -translate-x-1/2 -translate-y-1/2 overflow-hidden">
          <JarvisOrb state={view.state} level={view.level} className="block w-full" />
        </div>
      )}
      <p className="absolute bottom-2.5 left-3 flex items-center gap-1.5 text-[11px] font-semibold uppercase leading-none tracking-[0.08em]">
        <span className={`size-1.5 rounded-full ${view.dot}`} />
        <span className="text-slate-400">Jarvis</span>
        <span className="text-slate-300">{view.label}</span>
      </p>
    </div>
  );
});
