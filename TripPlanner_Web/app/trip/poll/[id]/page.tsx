"use client";

import { use, useEffect, useState, type FormEvent } from "react";
import liff from "@line/liff";
import { PageHeader } from "@/components/PageHeader";
import { StickyActionButton } from "@/components/StickyActionButton";
import { computeDateRangeLabel } from "@/lib/tripSummary";

interface PollVote {
  name: string;
  lineUserId: string;
  startDate: string;
  endDate: string;
  wishlist: string;
  vibes: string[];
  submittedAt: string;
}

interface LineProfile {
  userId: string;
  displayName: string;
  pictureUrl?: string;
}

const fieldClass =
  "mt-1.5 w-full rounded-2xl border border-white/10 bg-[#222222] px-4 py-3.5 text-base text-white placeholder:text-zinc-500 shadow-inner shadow-black/20 outline-none transition focus:border-white/25 focus:bg-[#262626] focus:ring-4 focus:ring-white/5";
const labelClass = "text-xs font-semibold uppercase tracking-wider text-zinc-400";
const WISHLIST_MAX_LENGTH = 150;

const VIBE_OPTIONS = [
  "🛍️ Shopping",
  "☕ Cafe Hopping",
  "📸 Photo Spots",
  "🥩 BBQ/Grill",
  "🏃‍♂️ Active/Sports",
  "🌳 Nature/Chill",
  "🍺 Nightlife",
];

// Local calendar day, not UTC - a UTC-based "today" can read as
// tomorrow or yesterday depending on the voter's timezone, which would
// wrongly block or allow dates right around midnight.
function todayISO() {
  return new Date().toLocaleDateString("en-CA");
}

function BoardingPass({ id, votes }: { id: string; votes: PollVote[] }) {
  const dateLabel = computeDateRangeLabel(votes);
  const crewLabel = votes.length === 0 ? "Just you" : `${votes.length} joined`;

  const barcodeWidths = Array.from({ length: 32 }, (_, i) => {
    const code = id.charCodeAt(i % Math.max(id.length, 1)) || 1;
    return 2 + (code % 4);
  });

  return (
    <div className="relative rounded-3xl bg-gradient-to-br from-indigo-500 via-violet-600 to-sky-600 p-[1px] shadow-2xl shadow-indigo-950/50">
      <div className="relative overflow-hidden rounded-3xl bg-[#1A1A1A]">
        <div className="flex items-start justify-between px-5 pt-5">
          <div>
            <p className="text-[11px] font-semibold uppercase tracking-[0.2em] text-indigo-200/70">
              Digital Boarding Pass
            </p>
            <p className="mt-1 text-xl font-bold text-white">Trip Poll</p>
          </div>
          <span className="shrink-0 rounded-full border border-emerald-500/20 bg-emerald-500/10 px-3 py-1 text-[11px] font-semibold uppercase tracking-wide text-emerald-400/90">
            Poll Open
          </span>
        </div>

        <div className="mt-5 flex items-center gap-3 px-5">
          <div className="min-w-0 flex-1">
            <p className="text-[11px] uppercase tracking-widest text-zinc-400">Crew</p>
            <p className="truncate text-2xl font-bold text-white">{crewLabel}</p>
          </div>
          <div className="flex w-10 shrink-0 flex-col items-center text-indigo-300">
            <span className="text-lg leading-none">&#9992;</span>
            <div className="mt-1 h-px w-full border-t border-dashed border-white/25" />
          </div>
          <div className="min-w-0 flex-1 text-right">
            <p className="text-[11px] uppercase tracking-widest text-zinc-400">Trip code</p>
            <p className="truncate font-mono text-2xl font-bold text-white">
              {id.toUpperCase()}
            </p>
          </div>
        </div>

        <div className="relative my-5">
          <div className="absolute -left-[10px] top-1/2 h-5 w-5 -translate-y-1/2 rounded-full bg-[#0d0d0d]" />
          <div className="absolute -right-[10px] top-1/2 h-5 w-5 -translate-y-1/2 rounded-full bg-[#0d0d0d]" />
          <div className="border-t border-dashed border-white/15" />
        </div>

        <div className="grid grid-cols-2 gap-2 px-5 pb-5 text-center">
          <div>
            <p className="text-[10px] uppercase tracking-widest text-zinc-500">Dates</p>
            <p className="mt-1 text-sm font-semibold text-white">{dateLabel}</p>
          </div>
          <div>
            <p className="text-[10px] uppercase tracking-widest text-zinc-500">Status</p>
            <p className="mt-1 text-sm font-semibold text-white">Awaiting lock-in</p>
          </div>
        </div>

        <div className="flex h-8 items-end gap-[3px] overflow-hidden px-5 pb-5">
          {barcodeWidths.map((w, i) => (
            <span
              key={i}
              className="h-full bg-white/25"
              style={{ width: `${w}px` }}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

export default function TripPollPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  // Client Component page: params is a Promise even here, read with React's
  // use() rather than await since this component cannot be async.
  const { id } = use(params);

  const [votes, setVotes] = useState<PollVote[]>([]);
  const [isLoadingVotes, setIsLoadingVotes] = useState(true);

  const [lineProfile, setLineProfile] = useState<LineProfile | null>(null);
  const [liffError, setLiffError] = useState("");

  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [wishlist, setWishlist] = useState("");
  const [vibes, setVibes] = useState<string[]>([]);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState("");

  // TODO: Check if user is Admin - replace with a real organizer check
  // (e.g. the trip creator's LINE user id) once auth exists. The toggle
  // below only exists so the gated state is demonstrable without one.
  const [isAdmin, setIsAdmin] = useState(true);
  const [isLocking, setIsLocking] = useState(false);
  const [lockError, setLockError] = useState("");

  function toggleVibe(vibe: string) {
    setVibes((prev) =>
      prev.includes(vibe) ? prev.filter((v) => v !== vibe) : [...prev, vibe]
    );
  }

  useEffect(() => {
    let cancelled = false;

    async function initLiff() {
      const liffId = process.env.NEXT_PUBLIC_LIFF_ID;
      if (!liffId) {
        if (!cancelled) setLiffError("LINE login isn't configured for this app.");
        return;
      }

      try {
        await liff.init({ liffId });
        if (!liff.isLoggedIn()) {
          // Redirects to LINE's login screen and back - nothing after
          // this line runs in the current page load.
          liff.login();
          return;
        }
        const profile = await liff.getProfile();
        if (!cancelled) {
          setLineProfile({
            userId: profile.userId,
            displayName: profile.displayName,
            pictureUrl: profile.pictureUrl,
          });
        }
      } catch {
        if (!cancelled) {
          setLiffError("Couldn't sign you in with LINE. Please reopen this link from LINE.");
        }
      }
    }

    initLiff();
    return () => {
      cancelled = true;
    };
  }, []);

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
        // Leave the list empty - the form below still works either way.
      } finally {
        if (!cancelled) {
          setIsLoadingVotes(false);
        }
      }
    }

    loadVotes();
    return () => {
      cancelled = true;
    };
  }, [id]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");

    if (!lineProfile) {
      setError("Please wait for LINE sign-in to finish before voting.");
      return;
    }

    if (startDate && startDate < todayISO()) {
      setError("The \"From\" date can't be in the past.");
      return;
    }

    if (startDate && endDate && endDate < startDate) {
      setError("The \"To\" date can't be earlier than the \"From\" date.");
      return;
    }

    setIsSubmitting(true);
    try {
      // The trusted user id now comes from server-side verification of
      // this token (see app/api/poll/[id]/route.ts's POST handler) - the
      // client no longer sends lineProfile.userId at all.
      const idToken = liff.getIDToken();
      if (!idToken) {
        throw new Error("LINE ID token unavailable.");
      }

      const response = await fetch(`/api/poll/${id}`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${idToken}`,
        },
        body: JSON.stringify({
          name: lineProfile.displayName,
          startDate,
          endDate,
          wishlist: wishlist.trim(),
          vibes,
        }),
      });

      if (!response.ok) {
        throw new Error("Vote submission failed.");
      }

      const data = await response.json();
      setVotes(Array.isArray(data.votes) ? data.votes : []);
      setStartDate("");
      setEndDate("");
      setWishlist("");
      setVibes([]);
    } catch {
      setError("Something went wrong submitting your vote. Please try again.");
    } finally {
      setIsSubmitting(false);
    }
  }

  async function handleLockAndGenerate() {
    setLockError("");
    setIsLocking(true);
    try {
      const response = await fetch("/api/trigger-jarvis", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ trip_id: id }),
      });

      if (!response.ok) {
        throw new Error("Trigger request failed.");
      }
    } catch {
      setLockError("Couldn't start plan generation. Please try again.");
    } finally {
      setIsLocking(false);
    }
  }

  return (
    <div className="min-h-screen pb-32">
      <PageHeader title="Trip Poll" tripId={id} />

      <main className="mx-auto max-w-md space-y-6 px-4 py-6">
        <button
          type="button"
          onClick={() => setIsAdmin((v) => !v)}
          className="text-xs text-zinc-500 underline decoration-dotted underline-offset-2"
        >
          Viewing as: {isAdmin ? "Admin" : "Guest"} (demo toggle)
        </button>

        {lockError && <p className="text-sm text-red-400">{lockError}</p>}

        <BoardingPass id={id} votes={votes} />

        <section className="rounded-3xl border border-white/10 bg-[#1A1A1A] p-5 shadow-2xl shadow-black/40">
          <h2 className="text-base font-semibold text-white">
            Add your availability
          </h2>
          <p className="mt-1 text-sm text-zinc-400">
            Let the group know when you&apos;re free and what you&apos;re hoping for.
          </p>

          <form className="mt-4 space-y-4" onSubmit={handleSubmit}>
            <div>
              <p className={labelClass}>Your name</p>
              <div className={`${fieldClass} flex items-center gap-3`}>
                {lineProfile ? (
                  <>
                    {lineProfile.pictureUrl ? (
                      // eslint-disable-next-line @next/next/no-img-element -- LINE profile pictures come from an arbitrary CDN host per user, not worth configuring next/image remote patterns for.
                      <img
                        src={lineProfile.pictureUrl}
                        alt=""
                        className="h-8 w-8 shrink-0 rounded-full object-cover"
                      />
                    ) : (
                      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-white/10 text-xs font-semibold text-zinc-300">
                        {lineProfile.displayName.trim().charAt(0).toUpperCase() || "?"}
                      </span>
                    )}
                    <span className="truncate">{lineProfile.displayName}</span>
                  </>
                ) : (
                  <span className="text-zinc-500">
                    {liffError || "Signing you in with LINE..."}
                  </span>
                )}
              </div>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div>
                <label htmlFor="startDate" className={labelClass}>
                  From
                </label>
                <input
                  id="startDate"
                  type="date"
                  value={startDate}
                  min={todayISO()}
                  onChange={(event) => setStartDate(event.target.value)}
                  className={`${fieldClass} [color-scheme:dark]`}
                />
              </div>
              <div>
                <label htmlFor="endDate" className={labelClass}>
                  To
                </label>
                <input
                  id="endDate"
                  type="date"
                  value={endDate}
                  min={startDate || todayISO()}
                  onChange={(event) => setEndDate(event.target.value)}
                  className={`${fieldClass} [color-scheme:dark]`}
                />
              </div>
            </div>

            <div className="space-y-2">
              <label htmlFor="wishlist" className={labelClass}>
                Specific Places (Optional)
              </label>
              <textarea
                id="wishlist"
                value={wishlist}
                onChange={(event) => setWishlist(event.target.value)}
                placeholder="Cafe, art gallery, chill vibes..."
                rows={3}
                maxLength={WISHLIST_MAX_LENGTH}
                className={`${fieldClass} resize-none`}
              />
              <p className="text-right text-xs text-zinc-500">
                {wishlist.length}/{WISHLIST_MAX_LENGTH}
              </p>
            </div>

            <div className="space-y-2 pt-1">
              <p className={labelClass}>Or choose the vibes you want</p>
              <div className="flex flex-wrap gap-2">
                {VIBE_OPTIONS.map((vibe) => {
                  const isSelected = vibes.includes(vibe);
                  return (
                    <button
                      key={vibe}
                      type="button"
                      onClick={() => toggleVibe(vibe)}
                      aria-pressed={isSelected}
                      className={`inline-flex items-center gap-1.5 rounded-full border px-4 py-2.5 text-sm font-medium transition active:scale-[0.97] ${
                        isSelected
                          ? "border-indigo-500 bg-indigo-500 text-white shadow-md shadow-indigo-500/25"
                          : "border-white/10 bg-[#1A1A1A] text-zinc-300"
                      }`}
                    >
                      {isSelected && <span aria-hidden="true">✓</span>}
                      {vibe}
                    </button>
                  );
                })}
              </div>
            </div>

            {error && <p className="text-sm text-red-400">{error}</p>}

            <button
              type="submit"
              disabled={isSubmitting}
              className="w-full rounded-full bg-zinc-200 px-4 py-3.5 text-base font-semibold text-zinc-900 shadow-md shadow-black/30 transition active:scale-[0.98] disabled:opacity-50"
            >
              {isSubmitting ? "Submitting..." : "Submit my vote"}
            </button>
          </form>
        </section>

        <section className="rounded-3xl border border-white/10 bg-[#1A1A1A] p-5 shadow-2xl shadow-black/40">
          <h2 className="text-base font-semibold text-white">
            Current votes ({votes.length})
          </h2>

          {isLoadingVotes ? (
            <p className="mt-3 text-sm text-zinc-400">Loading...</p>
          ) : votes.length === 0 ? (
            <p className="mt-3 text-sm text-zinc-400">
              No one has voted yet - be the first!
            </p>
          ) : (
            <ul className="mt-3 space-y-3">
              {votes.map((vote, index) => (
                <li
                  key={`${vote.name}-${vote.submittedAt}-${index}`}
                  className="rounded-2xl border border-white/10 bg-[#141414] p-4"
                >
                  <p className="font-medium text-white">{vote.name}</p>
                  {(vote.startDate || vote.endDate) && (
                    <p className="text-sm text-zinc-400">
                      {vote.startDate || "?"} &rarr; {vote.endDate || "?"}
                    </p>
                  )}
                  {vote.wishlist && (
                    <p className="mt-1 text-sm text-zinc-300">{vote.wishlist}</p>
                  )}
                  {Array.isArray(vote.vibes) && vote.vibes.length > 0 && (
                    <div className="mt-2 flex flex-wrap gap-1.5">
                      {vote.vibes.map((vibe) => (
                        <span
                          key={vibe}
                          className="rounded-full border border-indigo-500/20 bg-indigo-500/10 px-2.5 py-1 text-xs text-indigo-300"
                        >
                          {vibe}
                        </span>
                      ))}
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
        </section>
      </main>

      <StickyActionButton
        label={isLocking ? "Processing..." : "Lock & Generate Plan"}
        onClick={handleLockAndGenerate}
        disabled={!isAdmin || isLocking}
        disabledHint={!isAdmin ? "Only the trip organizer can lock the plan." : undefined}
      />
    </div>
  );
}
