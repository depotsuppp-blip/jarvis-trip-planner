/**
 * The one primary call-to-action every /trip/* page ends on ("Lock &
 * Generate Plan", "Generate Itinerary"), pinned to the bottom of the
 * viewport rather than left to scroll away at the end of a long page - a
 * mobile web app opened inside LINE's in-app browser has no app-level
 * bottom nav to anchor a primary action to, so the page provides its own.
 *
 * `disabled` (with an optional `disabledHint` caption) is for gating an
 * action behind a role check - see the admin-only lock on the Poll page -
 * without hiding the button outright, so a non-admin sees why they can't
 * act instead of wondering where the button went.
 *
 * `variant` defaults to the original dark-glass look every other /trip/*
 * page still uses - only the Poll page's light-theme Bento redesign
 * passes "light", so Dashboard/Draft are unaffected.
 */
export function StickyActionButton({
  label,
  onClick,
  disabled = false,
  disabledHint,
  variant = "dark",
}: {
  label: string;
  onClick: () => void;
  disabled?: boolean;
  disabledHint?: string;
  variant?: "dark" | "light";
}) {
  if (variant === "light") {
    return (
      <div className="fixed inset-x-0 bottom-0 z-20 border-t border-slate-200 bg-white/90 px-4 pb-[max(0.75rem,env(safe-area-inset-bottom))] pt-3 backdrop-blur-xl">
        {disabled && disabledHint && (
          <p className="mx-auto mb-2 max-w-md text-center text-xs text-slate-500">
            {disabledHint}
          </p>
        )}
        <button
          type="button"
          onClick={onClick}
          disabled={disabled}
          className={`mx-auto flex w-full max-w-md items-center justify-center rounded-full px-6 py-4 text-base font-semibold transition active:scale-[0.98] disabled:active:scale-100 ${
            disabled
              ? "cursor-not-allowed border border-slate-200 bg-slate-100 text-slate-400"
              : "bg-rose-500 text-white shadow-lg shadow-rose-500/25 hover:bg-rose-600"
          }`}
        >
          {label}
        </button>
      </div>
    );
  }

  return (
    <div className="fixed inset-x-0 bottom-0 z-20 border-t border-white/10 bg-black/40 px-4 pb-[max(0.75rem,env(safe-area-inset-bottom))] pt-3 backdrop-blur-xl">
      {disabled && disabledHint && (
        <p className="mx-auto mb-2 max-w-md text-center text-xs text-zinc-500">
          {disabledHint}
        </p>
      )}
      <button
        type="button"
        onClick={onClick}
        disabled={disabled}
        className={`mx-auto flex w-full max-w-md items-center justify-center rounded-full px-6 py-4 text-base font-semibold transition active:scale-[0.98] disabled:active:scale-100 ${
          disabled
            ? "cursor-not-allowed border border-white/10 bg-white/5 text-zinc-500"
            : "bg-gradient-to-r from-slate-100 to-indigo-100 text-zinc-900 shadow-lg shadow-black/40"
        }`}
      >
        {label}
      </button>
    </div>
  );
}
