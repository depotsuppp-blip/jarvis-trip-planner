"use client";

import { use, useEffect, useState, type FormEvent, type ReactNode } from "react";
import { useSession } from "next-auth/react";
import { MapPin } from "lucide-react";
import { LoginScreen } from "@/components/auth/LoginScreen";
import { PageHeader } from "@/components/PageHeader";
import { SwipeDeck, SwipeDeckSkeleton } from "@/components/trip/SwipeDeck";

// Mirrors the limit GET /api/places/deck enforces.
const MAX_DESTINATION_LENGTH = 100;

const fieldClass =
  "mt-1.5 w-full rounded-2xl border border-slate-200 bg-slate-50 px-4 py-3.5 text-base text-slate-900 placeholder:text-slate-400 shadow-inner shadow-slate-900/5 outline-none transition focus:border-blue-400 focus:bg-white focus:ring-4 focus:ring-blue-100";
const labelClass = "text-xs font-semibold uppercase tracking-wider text-slate-500";

function Shell({ subtitle, children }: { subtitle?: ReactNode; children: ReactNode }) {
  return (
    // A card in flight leaves the viewport, and a transformed box that is off
    // screen still counts as scrollable overflow - so clip it here, or the
    // page grows a horizontal scrollbar (and pans sideways on touch) for the
    // length of every swipe. clip, unlike hidden, keeps the header sticky.
    <div className="min-h-svh overflow-x-clip bg-slate-50">
      {/* The header keeps its second line (a non-breaking space when there is nothing to say yet), so it doesn't grow a row when the destination turns up. */}
      <PageHeader variant="light" title="Pick your places" subtitle={subtitle ?? " "} />
      <main className="mx-auto max-w-md px-4 py-6">{children}</main>
    </div>
  );
}

/**
 * The swipe step for one trip: /trip/swipe/<tripId>?destination=<place>.
 * Wraps components/trip/SwipeDeck.tsx with the sign-in gate and a way to
 * say where the deck should be drawn from.
 *
 * Unlike the poll and dashboard pages there is no ?admin= bypass: a swipe
 * belongs to a person (it's saved against the signed-in user), so everyone
 * - organizer included - signs in first. LoginScreen sends them straight
 * back here afterwards, ?destination= intact.
 *
 * `destination` comes from the URL because a trip doesn't have one on
 * record until its plan is locked (Poll has no such column - the plan's
 * destination is only inferred at lock time), so whatever links people
 * here has to say where. Opened without one, the page just asks.
 */
export default function TripSwipePage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  // Client Component page: params is a Promise even here, read with React's
  // use() rather than await since this component cannot be async.
  const { id } = use(params);
  const { status: sessionStatus } = useSession();

  // null until the URL has been read, so a link that already carries a
  // destination never flashes the prompt below; "" means there isn't one.
  const [destination, setDestination] = useState<string | null>(null);
  const [draft, setDraft] = useState("");

  useEffect(() => {
    // Read from window.location in a microtask, same as ?admin= on the poll
    // page, rather than useSearchParams() (which would need a Suspense
    // boundary just to avoid a static-prerender build error).
    queueMicrotask(() => {
      const fromUrl = new URLSearchParams(window.location.search).get("destination");
      setDestination(fromUrl?.trim().slice(0, MAX_DESTINATION_LENGTH) ?? "");
    });
  }, []);

  function chooseDestination(next: string) {
    setDestination(next);
    // Keep the URL in step, so a refresh (or a shared link) lands on the
    // same deck instead of asking again.
    window.history.replaceState(
      null,
      "",
      next ? `?destination=${encodeURIComponent(next)}` : window.location.pathname
    );
  }

  // "Change": back to the prompt with the current destination already typed in
  // (and selected), so a stray tap costs nothing and a typo is a small edit.
  function startChange() {
    setDraft(destination ?? "");
    chooseDestination("");
  }

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const next = draft.replace(/\s+/g, " ").trim().slice(0, MAX_DESTINATION_LENGTH);
    if (next) {
      chooseDestination(next);
    }
  }

  if (sessionStatus === "loading" || destination === null) {
    return (
      <Shell>
        <SwipeDeckSkeleton />
      </Shell>
    );
  }

  if (sessionStatus !== "authenticated") {
    return <LoginScreen />;
  }

  if (!destination) {
    return (
      <Shell>
        <section className="rounded-3xl border border-slate-200 bg-white p-6 shadow-sm">
          <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-blue-50 text-blue-600 ring-1 ring-blue-100">
            <MapPin className="h-6 w-6" aria-hidden="true" />
          </div>
          <h2 className="mt-4 text-2xl font-bold tracking-tight text-slate-900">
            Where are you headed?
          </h2>
          <p className="mt-1.5 text-sm leading-relaxed text-slate-500">
            Name a landmark, neighborhood or city and we&apos;ll deal you places to swipe through.
          </p>

          <form className="mt-5 space-y-4" onSubmit={handleSubmit}>
            <div>
              <label htmlFor="destination" className={labelClass}>
                Destination
              </label>
              <input
                id="destination"
                type="text"
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                onFocus={(event) => event.currentTarget.select()}
                placeholder="e.g. Siam Paragon"
                maxLength={MAX_DESTINATION_LENGTH}
                autoFocus
                className={fieldClass}
              />
            </div>
            <button
              type="submit"
              disabled={!draft.trim()}
              className="w-full rounded-full bg-rose-500 px-4 py-3.5 text-base font-semibold text-white shadow-md shadow-rose-500/20 outline-none transition hover:bg-rose-600 focus-visible:ring-4 focus-visible:ring-rose-200 active:scale-[0.98] disabled:opacity-50 disabled:active:scale-100"
            >
              Start swiping
            </button>
          </form>
        </section>
      </Shell>
    );
  }

  return (
    <Shell
      subtitle={
        <span className="flex items-center gap-1.5">
          <MapPin className="h-4 w-4 shrink-0 text-slate-400" aria-hidden="true" />
          <span className="min-w-0 truncate">Near {destination}</span>
          <button
            type="button"
            onClick={startChange}
            className="-my-3 shrink-0 py-3 pl-1 font-semibold text-blue-600 underline-offset-2 outline-none hover:underline focus-visible:underline"
          >
            Change
          </button>
        </span>
      }
    >
      <SwipeDeck
        key={destination}
        tripId={id}
        destination={destination}
        onChangeDestination={startChange}
      />
    </Shell>
  );
}
