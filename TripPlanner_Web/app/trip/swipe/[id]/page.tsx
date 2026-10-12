"use client";

import Link from "next/link";
import { use, useEffect, useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import { useSession } from "next-auth/react";
import { LoginScreen } from "@/components/auth/LoginScreen";
import { SwipeDeck, SwipeDeckSkeleton } from "@/components/trip/SwipeDeck";
import { hasCompletedTasteQuiz } from "@/lib/tasteProfile";

// The swipe step speaks the Modern Editorial dialect (see the plan view and
// sign-in): a white ground, 1px slate rules, ink text, no shadows. The bar is
// solid white with a hairline under it, like the plan view's.
function Shell({ children }: { children: ReactNode }) {
  return (
    // A card in flight leaves the viewport, and a transformed box that is off
    // screen still counts as scrollable overflow - so clip it here, or the
    // page grows a horizontal scrollbar (and pans sideways on touch) for the
    // length of every swipe. clip, unlike hidden, keeps the bar sticky.
    <div className="min-h-svh overflow-x-clip bg-white text-slate-900">
      <header className="sticky top-0 z-30 h-14 border-b border-slate-200 bg-white">
        {/* The rule runs edge to edge; the words line up with the column below. */}
        <div className="mx-auto flex h-full max-w-md flex-col justify-center px-4">
          <h1 className="text-base font-bold tracking-tight text-slate-900">Your travel taste</h1>
          <p className="text-[13px] leading-4 text-slate-600">Places from around the world</p>
        </div>
      </header>
      <main className="mx-auto max-w-md px-4 pb-8 pt-6">{children}</main>
    </div>
  );
}

/**
 * The swipe step: /trip/swipe/<tripId>. It is a taste quiz, not a trip
 * planner: the same 24 places from around the world for everyone (see
 * GET /api/places/deck), so it asks nothing about a destination and shows
 * nothing about one. The trip id in the URL only says which trip the person
 * came from - it is where their card-by-card answers are saved and where "Back
 * to trip" leads. A link from before this change that still carries
 * ?destination= is fine: the parameter is ignored.
 *
 * The quiz is once per person, not once per trip: finishing it saves the
 * answers as the person's Taste Profile and marks it done (POST
 * /api/taste-profile). Someone who has already done that is sent straight back
 * to the trip's poll instead of being asked again - the check runs before the
 * deck is requested, so they never spend a deck's worth of Google searches.
 *
 * Unlike the poll and dashboard pages there is no ?admin= bypass: a swipe
 * belongs to a person (it's saved against the signed-in user), so everyone
 * - organizer included - signs in first. LoginScreen sends them straight
 * back here afterwards.
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
  const router = useRouter();
  const pollPath = `/trip/poll/${encodeURIComponent(id)}`;

  // "checking" until GET /api/taste-profile has answered, then "done" (back to
  // the trip) or "open" (the deck).
  const [quiz, setQuiz] = useState<"checking" | "done" | "open">("checking");

  useEffect(() => {
    if (sessionStatus !== "authenticated") return;

    let cancelled = false;
    void hasCompletedTasteQuiz().then((completed) => {
      if (cancelled) return;
      setQuiz(completed ? "done" : "open");
      if (completed) router.replace(pollPath);
    });
    return () => {
      cancelled = true;
    };
  }, [sessionStatus, router, pollPath]);

  if (sessionStatus === "loading") {
    return (
      <Shell>
        <SwipeDeckSkeleton />
      </Shell>
    );
  }

  if (sessionStatus !== "authenticated") {
    return <LoginScreen />;
  }

  if (quiz === "checking") {
    return (
      <Shell>
        <SwipeDeckSkeleton />
      </Shell>
    );
  }

  if (quiz === "done") {
    return (
      <Shell>
        <div role="status" className="py-24 text-center">
          <p className="text-sm text-slate-600">You&apos;ve already finished this. Taking you back to the trip...</p>
          <Link
            href={pollPath}
            className="mt-4 inline-block rounded-md text-sm font-semibold text-slate-900 underline underline-offset-2 outline-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-rose-600"
          >
            Back to trip
          </Link>
        </div>
      </Shell>
    );
  }

  return (
    <Shell>
      <SwipeDeck tripId={id} />
    </Shell>
  );
}
