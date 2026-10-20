/**
 * Class strings the hub's components share, so one control is never styled two
 * ways. The accent is Deep Signal Rose (rose-600, #ec003f): on this surface it
 * means focus, selection and the caret - never decoration.
 */

/** Keyboard focus: a 2px rose outline, offset from the control. */
export const focusRing =
  "outline-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-rose-600";

/** Keyboard focus drawn inside the control, for controls that touch an edge that clips outlines. */
export const focusRingInset =
  "outline-none focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-rose-600";

/** A quiet scrollbar in the hairline colour instead of the browser's grey slab. */
export const thinScroll = "[scrollbar-color:#cad5e2_transparent] [scrollbar-width:thin]";

/** The ghost icon button used in the bars: 40px square, a hairline on hover. */
export const iconButton = `grid size-10 shrink-0 place-items-center rounded-md text-slate-700 transition-colors duration-150 hover:bg-slate-100 active:scale-[0.97] ${focusRing}`;
