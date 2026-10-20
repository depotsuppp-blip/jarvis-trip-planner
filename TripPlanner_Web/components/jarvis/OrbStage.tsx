"use client";

import { memo, useEffect, useLayoutEffect, useRef, useState } from "react";
import { ArrowUp, X } from "lucide-react";
import { useChatActions, useOrb, useOrbAnchors } from "@/lib/jarvis/JarvisProvider";
import { isFocalMode } from "@/lib/jarvis/types";
import { Orb, type OrbHandle } from "./Orb";
import { ORB_ANIMATION, ORB_FOCAL_PX, ORB_LABEL, ORB_SMALL_PX } from "./orbModes";
import { focusRing } from "./styles";

/**
 * Everything about the orb, and nothing else: the orb itself, the frost that
 * dims the conversation behind it, and the caption and buttons under it while
 * Jarvis is being listened to (a live "Recording" clock, Send, Cancel) or is
 * thinking (End).
 *
 * It subscribes to the orb store only. A reply, a new session, a typed letter
 * re-render other things; none of them reach this component, so the canvas
 * animation is never competing with React for the main thread.
 *
 * One orb with two homes: the small one over the slot in the composer, and the
 * middle of the stage (the main column, not the whole window - the sidebar is
 * not part of the stage). `place()` glides it between them; a resize, or the
 * sidebar sliding open, just moves it. It is positioned by handing a rectangle
 * straight to the orb (see Orb.tsx), never through state, so none of that
 * re-renders anything.
 *
 * Must be rendered inside the stage element (`position: relative`), which is what
 * the frost and the caption are laid over.
 */
export const OrbStage = memo(function OrbStage() {
  const mode = useOrb((state) => state.mode);
  const actions = useChatActions();
  const { slot } = useOrbAnchors();
  const orb = useRef<OrbHandle | null>(null);
  const focal = isFocalMode(mode);

  useLayoutEffect(() => {
    // The stage is found from the slot, not from a ref of its own: this effect runs before an
    // ANCESTOR element's ref is attached (React attaches a host ref after its children's layout
    // effects), but the slot - an earlier sibling - is already in place.
    const home = slot.current;
    const area = home?.closest<HTMLElement>("[data-jarvis-stage]") ?? null;

    const place = (animate: boolean) => {
      if (!orb.current || !home || !area) return;

      const px = focal ? ORB_FOCAL_PX : ORB_SMALL_PX;
      const target = (focal ? area : home).getBoundingClientRect();
      const x = target.left + (target.width - px) / 2;
      // Centre stage sits a little above the middle, so the caption and End fit under it.
      const y = focal ? target.top + target.height / 2 - px / 2 - 24 : target.top + (target.height - px) / 2;
      orb.current.place({ x, y, size: px }, { animate });
    };

    place(true);

    // The stage changes size while the sidebar slides and when the window resizes. The first
    // callback is the observer announcing what is already there: ignoring it keeps the glide
    // that place(true) just started.
    let announced = true;
    const observer = new ResizeObserver(() => {
      if (announced) {
        announced = false;
        return;
      }
      place(false);
    });
    if (home) observer.observe(home);
    if (area) observer.observe(area);

    const onResize = () => place(false);
    window.addEventListener("resize", onResize);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", onResize);
    };
  }, [focal, slot]);

  useEffect(() => {
    if (!focal) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") actions.endVoice();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [focal, actions]);

  return (
    <>
      {/* Frosts the conversation behind the centred orb. */}
      <div
        aria-hidden="true"
        className={`absolute inset-0 z-20 bg-white/60 backdrop-blur-md transition-opacity duration-500 ease-out motion-reduce:transition-none ${
          focal ? "opacity-100" : "pointer-events-none opacity-0"
        }`}
      />

      {/* Caption and exit, under the centred orb. */}
      {focal && (
        <div
          className="pointer-events-none absolute inset-x-0 top-1/2 z-40 flex flex-col items-center"
          style={{ marginTop: ORB_FOCAL_PX / 2 - 24 }}
        >
          <p className="text-lg font-semibold tracking-tight text-slate-900">{ORB_LABEL[mode]}…</p>
          {mode === "listening" && <RecordingClock />}
          <div className="mt-4 flex items-center gap-2">
            {mode === "listening" && (
              // Focus lands here when recording starts: the Talk button is under the frost.
              <button
                type="button"
                autoFocus
                onClick={() => void actions.finishVoice()}
                className={`pointer-events-auto inline-flex min-h-11 items-center gap-2 rounded-md bg-slate-900 px-4 text-sm font-medium text-white transition-[background-color,scale] duration-150 hover:bg-slate-700 active:scale-[0.97] ${focusRing}`}
              >
                <ArrowUp className="size-4" aria-hidden="true" />
                Send
              </button>
            )}
            <button
              type="button"
              onClick={actions.endVoice}
              className={`pointer-events-auto inline-flex min-h-11 items-center gap-2 rounded-md border border-slate-300 bg-white px-4 text-sm font-medium text-slate-800 transition-[border-color,scale] duration-150 hover:border-slate-900 active:scale-[0.97] ${focusRing}`}
            >
              <X className="size-4" aria-hidden="true" />
              {mode === "listening" ? "Cancel" : "End"}
            </button>
          </div>
        </div>
      )}

      {/* One orb, two homes. Pinned light: this page is white. */}
      <Orb
        ref={orb}
        state={ORB_ANIMATION[mode]}
        theme="light"
        aria-label={`Jarvis is ${ORB_LABEL[mode].toLowerCase()}`}
        className="pointer-events-none fixed left-0 top-0 z-30"
      />
    </>
  );
});

/**
 * "Recording 0:07", with an ink dot that pulses like the sidebar's "working" dot
 * (still, under reduced motion; rose is for focus here, red for errors). It counts
 * from the moment it appears - which is the moment the orb starts listening - and
 * ticks in its own state, so a second passing re-renders this line and nothing else.
 */
function RecordingClock() {
  const [elapsedMs, setElapsedMs] = useState(0);

  useEffect(() => {
    const startedAt = Date.now();
    const timer = window.setInterval(() => setElapsedMs(Date.now() - startedAt), 250);
    return () => window.clearInterval(timer);
  }, []);

  const seconds = Math.floor(elapsedMs / 1000);
  return (
    <p className="mt-1 inline-flex items-center gap-2 text-sm tabular-nums text-slate-600">
      <span aria-hidden="true" className="relative grid size-2.5 place-items-center">
        <span className="absolute size-full rounded-full bg-slate-900/30 motion-safe:animate-ping" />
        <span className="size-2.5 rounded-full bg-slate-900" />
      </span>
      Recording {Math.floor(seconds / 60)}:{String(seconds % 60).padStart(2, "0")}
    </p>
  );
}
