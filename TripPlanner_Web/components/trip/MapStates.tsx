import type { ReactNode } from "react";
import { MapPinOff } from "lucide-react";

/**
 * The route pane when there is no map to show: no browser key, a plan with no
 * stored coordinates, or Google refusing the key. Said plainly, in the pane's
 * own centre, so the stops on the left still read as the whole plan.
 */
export function MapMessage({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="flex size-full flex-col items-center justify-center gap-3 px-8 text-center">
      <span className="flex size-10 items-center justify-center rounded-full bg-white text-slate-500 ring-1 ring-slate-200">
        <MapPinOff className="size-[18px]" aria-hidden="true" />
      </span>
      <div className="max-w-xs">
        <p className="text-sm font-semibold text-slate-900">{title}</p>
        {children && <p className="mt-1 text-[13px] leading-5 text-slate-600">{children}</p>}
      </div>
    </div>
  );
}

/** Held in the pane while Google's script and the map's own chunk come in. */
export function MapLoading() {
  return (
    <div role="status" className="flex size-full items-center justify-center text-[13px] text-slate-600">
      <span className="flex items-center gap-2">
        <span aria-hidden="true" className="size-2 rounded-full bg-rose-600 motion-safe:animate-pulse" />
        Loading map
      </span>
    </div>
  );
}
