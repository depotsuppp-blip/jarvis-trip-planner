/**
 * Sticky page header: title + trip id, shared by every /trip/* page.
 *
 * Sticky rather than static so the trip id - the thing a friend forwarded
 * this link to confirm - stays visible while scrolling a long vote list or
 * a long draft textarea on a small screen.
 *
 * The profile circle is a static placeholder (no auth/profile data exists
 * yet) - it'll be swapped for the user's LINE profile picture once this
 * app is wired up to LINE login.
 *
 * `variant` defaults to the original dark-glass look every other /trip/*
 * page still uses - only the Poll page's light-theme Bento redesign
 * passes "light", so Dashboard/Draft are unaffected.
 */
export function PageHeader({
  title,
  tripId,
  variant = "dark",
}: {
  title: string;
  tripId: string;
  variant?: "dark" | "light";
}) {
  if (variant === "light") {
    return (
      <header className="sticky top-0 z-10 flex items-start justify-between border-b border-slate-200 bg-white/80 px-4 py-4 backdrop-blur-xl">
        <div className="min-w-0">
          <h1 className="text-lg font-semibold tracking-tight text-slate-900">{title}</h1>
          <p className="mt-0.5 text-sm text-slate-500">
            Trip ID: <span className="font-mono text-slate-600">{tripId}</span>
          </p>
        </div>

        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-slate-200 bg-slate-50">
          <svg viewBox="0 0 24 24" fill="none" className="h-5 w-5 text-slate-400" aria-hidden="true">
            <path
              d="M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4Z"
              stroke="currentColor"
              strokeWidth="1.5"
            />
            <path
              d="M4.5 20c0-3.31 3.36-6 7.5-6s7.5 2.69 7.5 6"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
            />
          </svg>
        </div>
      </header>
    );
  }

  return (
    <header className="sticky top-0 z-10 flex items-start justify-between border-b border-white/10 bg-black/30 px-4 py-4 backdrop-blur-xl">
      <div className="min-w-0">
        <h1 className="text-lg font-semibold tracking-tight text-white">{title}</h1>
        <p className="mt-0.5 text-sm text-zinc-400">
          Trip ID: <span className="font-mono text-zinc-300">{tripId}</span>
        </p>
      </div>

      <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-gray-600 bg-gray-800">
        <svg viewBox="0 0 24 24" fill="none" className="h-5 w-5 text-gray-400" aria-hidden="true">
          <path
            d="M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4Z"
            stroke="currentColor"
            strokeWidth="1.5"
          />
          <path
            d="M4.5 20c0-3.31 3.36-6 7.5-6s7.5 2.69 7.5 6"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
          />
        </svg>
      </div>
    </header>
  );
}
