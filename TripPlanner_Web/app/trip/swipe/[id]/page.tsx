"use client";

import Link from "next/link";
import { use, useEffect, useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import { useSession } from "next-auth/react";
import { LoginScreen } from "@/components/auth/LoginScreen";
import { PageHeader } from "@/components/PageHeader";
import { SwipeDeck, SwipeDeckSkeleton } from "@/components/trip/SwipeDeck";
import { hasCompletedTasteQuiz } from "@/lib/tasteProfile";

// The swipe step is the playful part of the app: a light slate-50 ground, big
// rounded cards with soft shadows and round buttons. (The plan view and the
// sign-in screen speak the stricter Modern Editorial dialect; this page
// deliberately does not - a taste quiz that feels like a form gets abandoned.)
function Shell({ children }: { children: ReactNode }) {
  return (
    // A card in flight leaves the viewport, and a transformed box that is off
    // screen still counts as scrollable overflow - so clip it here, or the
    // page grows a horizontal scrollbar (and pans sideways on touch) for the
    // length of every swipe. clip, unlike hidden, keeps the header sticky.
    <div className="min-h-svh overflow-x-clip bg-slate-50">
      <PageHeader variant="light" title="Your travel taste" subtitle="Places from around the world" />
      <main className="mx-auto max-w-md px-4 py-6">{children}</main>
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
            className="mt-4 inline-block rounded-full px-3 py-2 text-sm font-semibold text-rose-600 underline underline-offset-2 outline-none hover:text-rose-700 focus-visible:ring-4 focus-visible:ring-rose-200"
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
