"use client";

import { useEffect, useImperativeHandle, useRef, type Ref } from "react";
import { MODE_FRAMES, paintFrame, resolvePreset, type OrbState } from "thinking-orbs/engine";

/**
 * The Jarvis orb, drawn at whatever pixel size it is asked for.
 *
 * <ThinkingOrb size> only takes its three tuned presets (64, 32 and 20): any
 * other number makes resolvePreset throw. So a 224px orb cannot be asked of
 * it, and the 64px one stretched with CSS scale() is painted into a 64px
 * buffer that the browser then enlarges - which is what made the orb soft and
 * blocky in the middle of the screen. This keeps the library's own design (the
 * 64px preset: same dots, same animation, same speed) but paints it with the
 * canvas transform scaled up, onto a buffer that really is `size` device
 * pixels across. Every dot is a crisp circle at any size, part-way through a
 * move included.
 *
 * It also owns the move between the page's two homes: place() says where the
 * orb sits and how big it is, and the position and the size glide together on
 * the page's own ease-out, with the buffer resized every frame, so the orb
 * still grows as it travels - the thing the old scale() transition did.
 */

// The preset the geometry comes from; every other size is this, scaled.
const DESIGN_PX = 64;
// The highest pixel ratio the buffer is made for: 224px at 3x is a 672px canvas, still cheap.
const MAX_DPR = 3;
const MOVE_MS = 700;
// What thinking-orbs itself paints, in animation seconds, for someone who prefers reduced motion.
const STILL_FRAME_SECONDS = 0.6;

export interface OrbPlacement {
  /** Left edge, CSS px from the left of the viewport. */
  x: number;
  /** Top edge, CSS px from the top of the viewport. */
  y: number;
  /** Width and height, CSS px. */
  size: number;
}

export interface OrbHandle {
  /**
   * Puts the orb's box at `to`. It glides there over MOVE_MS unless `animate`
   * is false or the person prefers reduced motion; the very first call just
   * puts it there.
   */
  place(to: OrbPlacement, options?: { animate?: boolean }): void;
}

// The curve CSS calls cubic-bezier(x1, y1, x2, y2): solved for x with Newton's
// method, falling back to bisection where the slope is too flat to trust.
function cubicBezier(x1: number, y1: number, x2: number, y2: number) {
  const cx = 3 * x1;
  const bx = 3 * (x2 - x1) - cx;
  const ax = 1 - cx - bx;
  const cy = 3 * y1;
  const by = 3 * (y2 - y1) - cy;
  const ay = 1 - cy - by;
  const x = (t: number) => ((ax * t + bx) * t + cx) * t;
  const y = (t: number) => ((ay * t + by) * t + cy) * t;
  const slope = (t: number) => (3 * ax * t + 2 * bx) * t + cx;

  return (progress: number) => {
    if (progress <= 0) return 0;
    if (progress >= 1) return 1;

    let t = progress;
    for (let i = 0; i < 8; i++) {
      const error = x(t) - progress;
      if (Math.abs(error) < 1e-6) return y(t);
      const rate = slope(t);
      if (Math.abs(rate) < 1e-6) break;
      t -= error / rate;
    }

    let low = 0;
    let high = 1;
    t = progress;
    for (let i = 0; i < 32; i++) {
      const error = x(t) - progress;
      if (Math.abs(error) < 1e-6) break;
      if (error > 0) high = t;
      else low = t;
      t = (low + high) / 2;
    }
    return y(t);
  };
}

// The strong ease-out the page already used for this move.
const easeOut = cubicBezier(0.23, 1, 0.32, 1);

function between(from: OrbPlacement, to: OrbPlacement, k: number): OrbPlacement {
  return {
    x: from.x + (to.x - from.x) * k,
    y: from.y + (to.y - from.y) * k,
    size: from.size + (to.size - from.size) * k,
  };
}

// Fits the canvas to `at` and returns its buffer width in pixels. The box is
// rounded to whole device pixels, in size and in position, so the browser
// never has to resample the buffer.
function layout(canvas: HTMLCanvasElement, at: OrbPlacement) {
  const dpr = Math.min(MAX_DPR, window.devicePixelRatio || 1);
  const buffer = Math.max(1, Math.round(at.size * dpr));
  if (canvas.width !== buffer) {
    canvas.width = buffer;
    canvas.height = buffer;
  }
  canvas.style.width = `${buffer / dpr}px`;
  canvas.style.height = `${buffer / dpr}px`;
  canvas.style.transform = `translate(${Math.round(at.x * dpr) / dpr}px, ${Math.round(at.y * dpr) / dpr}px)`;
  return buffer;
}

export function Orb({
  state,
  theme = "light",
  className,
  "aria-label": ariaLabel = "Jarvis",
  ref,
}: {
  state: OrbState;
  /** Pinned, like thinking-orbs' own `theme`: `light` is dark ink for a white page. */
  theme?: "light" | "dark";
  /** Where the orb sits in the page (for example `fixed left-0 top-0`); its box is set here. */
  className?: string;
  "aria-label"?: string;
  ref?: Ref<OrbHandle>;
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  // Where the orb is and the move it is part-way through, if any. Refs rather
  // than state because they change on every frame of a move.
  const at = useRef<OrbPlacement | null>(null);
  const move = useRef<{ from: OrbPlacement; to: OrbPlacement; startedAt: number } | null>(null);
  // Set by the effect below: paints one frame now, so a move that doesn't
  // glide shows at once instead of on the next frame (or, under reduced
  // motion, when nothing else is drawing at all).
  const repaint = useRef<(() => void) | null>(null);
  const reducedMotion = useRef(false);

  useImperativeHandle(
    ref,
    () => ({
      place(to, options) {
        const canvas = canvasRef.current;
        if (!canvas) return;

        const from = at.current;
        if (from && options?.animate !== false && !reducedMotion.current) {
          move.current = { from, to, startedAt: performance.now() };
          return;
        }

        at.current = to;
        move.current = null;
        layout(canvas, to);
        repaint.current?.();
      },
    }),
    []
  );

  useEffect(() => {
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;

    const { mode, speed, opts } = resolvePreset(state, DESIGN_PX);
    const geometry = MODE_FRAMES[mode];
    const dark = theme === "dark";
    const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
    let raf = 0;

    // One frame at animation time `seconds`: fit the canvas to where the orb
    // is, then paint the 64px design scaled to fill its buffer.
    const draw = (seconds: number) => {
      const here = at.current;
      if (!here) return;
      const buffer = layout(canvas, here);
      ctx.setTransform(buffer / DESIGN_PX, 0, 0, buffer / DESIGN_PX, 0, 0);
      ctx.clearRect(0, 0, DESIGN_PX, DESIGN_PX);
      paintFrame(ctx, geometry(DESIGN_PX, seconds, opts), dark);
    };

    const loop = (now: number) => {
      const current = move.current;
      if (current) {
        const progress = Math.min(1, (now - current.startedAt) / MOVE_MS);
        at.current = progress === 1 ? current.to : between(current.from, current.to, easeOut(progress));
        if (progress === 1) move.current = null;
      }
      draw((now / 1000) * speed);
      raf = requestAnimationFrame(loop);
    };

    repaint.current = () => draw(motion.matches ? STILL_FRAME_SECONDS : (performance.now() / 1000) * speed);

    // Reduced motion: one still frame and no loop, and a move in progress lands where it was going.
    const sync = () => {
      cancelAnimationFrame(raf);
      reducedMotion.current = motion.matches;
      if (motion.matches) {
        if (move.current) {
          at.current = move.current.to;
          move.current = null;
        }
        draw(STILL_FRAME_SECONDS);
      } else {
        draw((performance.now() / 1000) * speed);
        raf = requestAnimationFrame(loop);
      }
    };

    sync();
    motion.addEventListener("change", sync);
    return () => {
      cancelAnimationFrame(raf);
      motion.removeEventListener("change", sync);
      repaint.current = null;
    };
  }, [state, theme]);

  // Zero-sized until place() gives it a box, so it never shows at a default 300x150.
  return (
    <canvas
      ref={canvasRef}
      role="img"
      aria-label={ariaLabel}
      className={className}
      style={{ display: "block", width: 0, height: 0 }}
    />
  );
}
