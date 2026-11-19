import { focusRing } from "../styles";

/**
 * The Memory page's dark dialect (the Solar System): obsidian black (#06070B, the 3D scene's background) under glass panels.
 * Class strings shared by the page and its panel, so one control is never styled two ways.
 * Text on the glass, measured: Star Ink #f1f5fb 17.2:1, Star Body #c9d3e3 12.5:1, Star Muted
 * #a3b0c6 8.6:1, Flare #ff8095 (errors) 7.9:1, Aurora #4ade80 (saved) 10.8:1. The keyboard
 * focus ring is the hub's own Deep Signal Rose (4.2:1 on the glass).
 */

/** Frosted dark glass over the starfield: panels, the top bar, the legend. */
export const glass =
  "border border-white/10 bg-[#0d1326]/80 shadow-[0_12px_40px_rgba(0,0,0,0.45)] backdrop-blur-xl";

/** A quiet scrollbar for the dark panels. */
export const darkScroll = "[scrollbar-color:rgba(148,163,184,0.35)_transparent] [scrollbar-width:thin]";

/** 40px ghost icon button on glass. */
export const glassIcon = `grid size-10 shrink-0 place-items-center rounded-lg text-[#c9d3e3] transition-[background-color,color,scale] duration-150 hover:bg-white/[0.08] hover:text-white active:scale-[0.97] ${focusRing}`;

/** 40px outlined button on glass (Fit to view, Edit, Cancel). */
export const glassButton = `inline-flex h-10 shrink-0 items-center justify-center gap-2 rounded-lg border border-white/[0.14] bg-white/[0.04] px-3 text-sm font-semibold text-[#f1f5fb] transition-[background-color,border-color,scale] duration-150 hover:border-white/25 hover:bg-white/[0.09] active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`;

/** The one filled button in a form (Save): starlight on the glass. */
export const starButton = `inline-flex h-10 items-center justify-center gap-2 rounded-lg bg-[#f1f5fb] px-4 text-sm font-semibold text-[#0b1122] transition-[background-color,scale] duration-150 hover:bg-white active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50 ${focusRing}`;

/** A destructive ghost button (Delete), and the filled one that confirms it. */
export const flareGhost = `inline-flex h-10 items-center justify-center gap-2 rounded-lg px-3 text-sm font-semibold text-[#ff8095] transition-[background-color,scale] duration-150 hover:bg-[#ff5c7a]/10 active:scale-[0.98] disabled:cursor-wait disabled:opacity-60 ${focusRing}`;
export const flareButton = `inline-flex h-10 items-center justify-center gap-2 rounded-lg bg-[#e11d48] px-4 text-sm font-semibold text-white transition-[background-color,scale] duration-150 hover:bg-[#be123c] active:scale-[0.98] disabled:cursor-wait disabled:opacity-60 ${focusRing}`;

/** Text fields and choices on glass (the width is the caller's: w-full in a form, a share of a row in the top bar). */
export const glassField = `block rounded-lg border border-white/[0.14] bg-[#060913]/70 px-3 text-[15px] text-[#f1f5fb] placeholder:text-[#8b98ae] transition-colors duration-150 hover:border-white/25 focus-visible:border-white/30 disabled:cursor-not-allowed disabled:opacity-60 ${focusRing}`;

/** An 11px tracked caption over a section ("Graph analytics"). */
export const caption = "text-[11px] font-semibold uppercase tracking-[0.08em] text-[#a3b0c6]";
