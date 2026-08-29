"use client";

import { use, useEffect, useState } from "react";
import { PageHeader } from "@/components/PageHeader";
import { computeDateRangeLabel, tallyVibes } from "@/lib/tripSummary";

interface PollVote {
  name: string;
  startDate: string;
  endDate: string;
  wishlist: string;
  vibes: string[];
  submittedAt: string;
}

const cardClass =
  "rounded-3xl border border-white/10 bg-[#1A1A1A] p-4 shadow-2xl shadow-black/40";

export default function TripDashboardPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  // Client Component page: params is a Promise even here, read with React's
  // use() rather than await since this component cannot be async.
  const { id } = use(params);

  const [votes, setVotes] = useState<PollVote[]>([]);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;

    async function loadVotes() {
      try {
        const response = await fetch(`/api/poll/${id}`, { cache: "no-store" });
        const data = await response.json();
        if (!cancelled) {
          setVotes(Array.isArray(data.votes) ? data.votes : []);
        }
      } catch {
        // Leave the dashboard on its empty state - still readable either way.
      } finally {
        if (!cancelled) {
          setIsLoading(false);
        }
      }
    }

    loadVotes();
    return () => {
      cancelled = true;
    };
  }, [id]);

  const dateLabel = computeDateRangeLabel(votes);
  const topVibes = tallyVibes(votes);
  const maxVibeCount = topVibes[0]?.count ?? 0;

  const wishlistEntries = votes.filter((vote) => vote.wishlist.trim().length > 0);

  return (
    <div className="min-h-screen pb-10">
      <PageHeader title="Trip Dashboard" tripId={id} />

      <main className="mx-auto max-w-md px-4 py-6">
        {isLoading ? (
          <p className="text-sm text-zinc-400">Loading trip summary...</p>
        ) : (
          <div className="grid grid-cols-2 gap-4">
            {/* Overview - hero card */}
            <section className={`col-span-2 ${cardClass}`}>
              <div className="flex items-center justify-between">
                <p className="text-xs font-semibold uppercase tracking-wider text-zinc-400">
                  Overview
                </p>
                <span className="rounded-full border border-emerald-500/20 bg-emerald-500/10 px-3 py-1 text-[11px] font-semibold uppercase tracking-wide text-emerald-400/90">
                  Poll Open
                </span>
              </div>

              <div className="mt-4 flex items-end justify-between gap-4">
                <div className="min-w-0">
                  <p className="text-[11px] uppercase tracking-widest text-zinc-500">
                    Trip code
                  </p>
                  <p className="truncate font-mono text-lg font-bold text-white">
                    {id.toUpperCase()}
                  </p>

                  <p className="mt-3 text-[11px] uppercase tracking-widest text-zinc-500">
                    Dates
                  </p>
                  <p className="text-lg font-bold text-white">{dateLabel}</p>
                </div>

                <div className="shrink-0 text-right">
                  <p className="text-5xl font-extrabold text-indigo-300">
                    {votes.length}
                  </p>
                  <p className="text-xs uppercase tracking-widest text-zinc-500">
                    Total Joined
                  </p>
                </div>
              </div>
            </section>

            {/* Top Vibes */}
            <section className={cardClass}>
              <p className="text-xs font-semibold uppercase tracking-wider text-zinc-400">
                Top Vibes
              </p>
              {topVibes.length === 0 ? (
                <p className="mt-3 text-sm text-zinc-500">No vibes picked yet.</p>
              ) : (
                <ul className="mt-3 space-y-2.5">
                  {topVibes.slice(0, 5).map(({ vibe, count }) => (
                    <li key={vibe}>
                      <div className="flex items-center justify-between gap-2 text-xs">
                        <span className="truncate text-zinc-200">{vibe}</span>
                        <span className="shrink-0 font-semibold text-white">
                          {count}
                        </span>
                      </div>
                      <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-white/5">
                        <div
                          className="h-full rounded-full bg-indigo-500"
                          style={{ width: `${(count / maxVibeCount) * 100}%` }}
                        />
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </section>

            {/* Voter List */}
            <section className={cardClass}>
              <p className="text-xs font-semibold uppercase tracking-wider text-zinc-400">
                Voters
              </p>
              {votes.length === 0 ? (
                <p className="mt-3 text-sm text-zinc-500">No one yet.</p>
              ) : (
                <ul className="mt-3 space-y-2.5">
                  {votes.map((vote, index) => (
                    <li
                      key={`${vote.name}-${vote.submittedAt}-${index}`}
                      className="flex items-center gap-2"
                    >
                      <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-white/10 text-[11px] font-semibold text-zinc-200">
                        {vote.name.trim().charAt(0).toUpperCase() || "?"}
                      </span>
                      <span className="truncate text-sm text-zinc-200">
                        {vote.name}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </section>

            {/* Wishlist Summary */}
            <section className={`col-span-2 ${cardClass}`}>
              <p className="text-xs font-semibold uppercase tracking-wider text-zinc-400">
                Wishlist Summary
              </p>
              {wishlistEntries.length === 0 ? (
                <p className="mt-3 text-sm text-zinc-500">
                  No specific places suggested yet.
                </p>
              ) : (
                <ul className="mt-3 space-y-3">
                  {wishlistEntries.map((vote, index) => (
                    <li
                      key={`${vote.name}-${vote.submittedAt}-${index}`}
                      className="rounded-2xl border border-white/10 bg-[#141414] p-3"
                    >
                      <p className="text-sm text-zinc-200">{vote.wishlist}</p>
                      <p className="mt-1 text-xs text-zinc-500">— {vote.name}</p>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </div>
        )}
      </main>
    </div>
  );
}
