"use client";

import { useLayoutEffect, useRef, type KeyboardEvent, type MouseEvent } from "react";
import type { DayGroup } from "@/lib/itineraryDays";

// The underline's slide: strong ease-out, short enough that a tab change
// never feels like it is waiting on the animation.
const SLIDE_MS = 180;
const EASE_OUT = "cubic-bezier(0.23, 1, 0.32, 1)";

export function dayTabId(idPrefix: string, day: number) {
  return `${idPrefix}-tab-${day}`;
}

/**
 * The day switcher: a standard tab list (one tab per day that has stops).
 * Arrow keys move and select, Home/End jump to the ends, and only the
 * selected tab is in the tab order - the panel it controls is
 * `${idPrefix}-panel` (rendered by components/trip/TimelineList.tsx).
 *
 * The selected tab carries its own rose underline, so the list reads
 * correctly with no script. When the selection changes by pointer or touch,
 * that underline slides from the old tab to the new one (a FLIP: measured
 * before the change, played from the old position to the new), as a
 * transform on the compositor. A keyboard change swaps instantly - someone
 * arrowing through days is not waiting for an animation - and reduced
 * motion skips the slide.
 */
export function DayTabs({
  groups,
  activeDay,
  onChange,
  idPrefix,
}: {
  groups: DayGroup[];
  activeDay: number;
  onChange: (day: number) => void;
  idPrefix: string;
}) {
  const tabRefs = useRef(new Map<number, HTMLButtonElement>());
  const indicatorRef = useRef<HTMLSpanElement | null>(null);
  // Where the underline sat before a pointer-driven change, in the tab
  // list's own coordinates (so a strip that scrolls sideways can't skew it).
  const slideFrom = useRef<{ left: number; width: number } | null>(null);

  useLayoutEffect(() => {
    const from = slideFrom.current;
    slideFrom.current = null;

    const active = tabRefs.current.get(activeDay);
    const indicator = indicatorRef.current;
    // Keeps the selected tab in view when there are more days than fit.
    active?.scrollIntoView({ block: "nearest", inline: "nearest" });

    if (!from || !active || !indicator) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

    const left = active.offsetLeft + indicator.offsetLeft;
    indicator.animate(
      [
        { transform: `translateX(${from.left - left}px) scaleX(${from.width / indicator.offsetWidth})` },
        { transform: "none" },
      ],
      { duration: SLIDE_MS, easing: EASE_OUT }
    );
  }, [activeDay]);

  function select(day: number, viaKeyboard: boolean) {
    if (day === activeDay) return;

    const current = tabRefs.current.get(activeDay);
    const indicator = indicatorRef.current;
    slideFrom.current =
      !viaKeyboard && current && indicator
        ? { left: current.offsetLeft + indicator.offsetLeft, width: indicator.offsetWidth }
        : null;
    onChange(day);
  }

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const days = groups.map((group) => group.day);
    const current = days.indexOf(activeDay);

    let next: number;
    switch (event.key) {
      case "ArrowRight":
        next = (current + 1) % days.length;
        break;
      case "ArrowLeft":
        next = (current - 1 + days.length) % days.length;
        break;
      case "Home":
        next = 0;
        break;
      case "End":
        next = days.length - 1;
        break;
      default:
        return;
    }

    event.preventDefault();
    select(days[next], true);
    tabRefs.current.get(days[next])?.focus();
  }

  // The rule sits on the outer box so it is exactly as wide as the column
  // beneath; the tab list inside is 8px wider each side, so the first label
  // lines up with the content while its tap area reaches past it.
  return (
    <div className="border-b border-slate-200">
      <div
        role="tablist"
        aria-label="Itinerary days"
        onKeyDown={handleKeyDown}
        className="relative -mx-2 flex gap-1 overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
      >
        {groups.map((group) => {
          const selected = group.day === activeDay;
          return (
            <button
              key={group.day}
              ref={(node) => {
                if (node) tabRefs.current.set(group.day, node);
                else tabRefs.current.delete(group.day);
              }}
              type="button"
              role="tab"
              id={dayTabId(idPrefix, group.day)}
              aria-selected={selected}
              aria-controls={`${idPrefix}-panel`}
              tabIndex={selected ? 0 : -1}
              // detail === 0 is a click made by the keyboard (Enter or Space).
              onClick={(event: MouseEvent<HTMLButtonElement>) => select(group.day, event.detail === 0)}
              className="relative flex h-11 shrink-0 items-center px-2 text-sm font-medium text-slate-500 outline-none transition-[color,scale] duration-150 ease-out hover:text-slate-900 focus-visible:text-slate-900 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-rose-600 aria-selected:text-slate-900 active:scale-[0.97]"
            >
              Day {group.day}
              {selected && (
                <span
                  ref={indicatorRef}
                  aria-hidden="true"
                  className="absolute inset-x-2 bottom-0 h-0.5 origin-left bg-rose-600"
                />
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}
