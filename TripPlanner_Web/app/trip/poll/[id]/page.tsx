"use client";

import { use, useEffect, useState, type FormEvent } from "react";
import { ensureLiffInit, liff } from "@/lib/liff";
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

interface TravelLeg {
  durationMinutes: number;
  distanceMeters: number;
}

interface ItineraryStop {
  slotType: "activity" | "meal";
  text: string;
  // From the PRECEDING stop in this same day's array - null for a day's
  // first stop, or wherever Stage 2.5 (app/api/trigger-jarvis/route.ts)
  // had no route data (no coordinates for one of the two stops, or the
  // Routes API call for that day failed). A future-dated, historical-
  // pattern estimate, not live traffic - see the "~" in how this
  // renders below.
  travelFromPrevious: TravelLeg | null;
}

interface ItineraryDay {
  day: number;
  summary: string;
  stops: ItineraryStop[];
}

interface Itinerary {
  destination: string;
  days: ItineraryDay[];
  notes: string;
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

// Rounded, never 0 - "~0 min" would read as broken rather than "very
// close by". The "(estimate)" wording sits next to this at the call
// site, not baked in here, since this also feeds the title tooltip.
function formatTravelMinutes(minutes: number): string {
  return `~${Math.max(1, Math.round(minutes))} min`;
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

  // Presence of ?admin=<token> is what shows the "Lock & Generate Plan"
  // button at all - see handleLockAndGenerate, which sends this same raw
  // value to POST /api/trigger-jarvis. This is UX only, not a security
  // check: the route itself independently verifies the token server-side
  // (lib/adminToken.ts's verifyAdminToken) before doing anything, so a
  // guessed or missing token is rejected there regardless of what this
  // page renders. Only the trip creator ever receives a link with this
  // param - see plugins/trip_planner.py's _run_consensus_poll, which
  // sends it in a separate, private LINE message from the public voting
  // link. Read directly from window.location rather than
  // next/navigation's useSearchParams(), which would require wrapping
  // this whole page in a Suspense boundary purely to avoid a
  // static-prerender build error - not worth it for a value that's only
  // ever read once, client-side, on mount.
  const [adminToken, setAdminToken] = useState("");
  const isAdmin = Boolean(adminToken);

  const [votes, setVotes] = useState<PollVote[]>([]);
  const [isLoadingVotes, setIsLoadingVotes] = useState(true);

  // LINE identity is now a silent, best-effort enhancement, never a
  // gate - see the initLiff effect below. `ready` only ever decides
  // whether the name field shows a silent LINE prefill or stays empty
  // for manual entry; it never blocks the form itself.
  const [displayName, setDisplayName] = useState("");
  const [idToken, setIdToken] = useState("");
  const [ready, setReady] = useState(false);

  // Whether this device has already voted anonymously, purely to relabel
  // the submit button to "Update your vote" - see handleSubmit, which
  // lets a resubmission through either way. Not a security control: a
  // different browser, device, or cleared site data votes again freely.
  const [hasVotedOnThisDevice, setHasVotedOnThisDevice] = useState(false);

  // Stable per-(device, trip) identity for an anonymous voter, generated
  // once and persisted in localStorage - see handleSubmit and
  // lib/store.ts's addPollVote. This, not the typed name, is what the
  // server dedups a resubmission against: two different people can type
  // the same name, but this id is (for practical purposes) never shared.
  const [anonId, setAnonId] = useState("");

  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [wishlist, setWishlist] = useState("");
  const [vibes, setVibes] = useState<string[]>([]);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState("");

  const [isLocking, setIsLocking] = useState(false);
  const [lockError, setLockError] = useState("");
  const [generatedPlan, setGeneratedPlan] = useState<Itinerary | null>(null);

  function toggleVibe(vibe: string) {
    setVibes((prev) =>
      prev.includes(vibe) ? prev.filter((v) => v !== vibe) : [...prev, vibe]
    );
  }

  useEffect(() => {
    let cancelled = false;

    async function initLiff() {
      // ensureLiffInit() wraps liff.init() and never throws - it
      // returns false on any failure (missing NEXT_PUBLIC_LIFF_ID,
      // strict browser tracking prevention blocking the storage LIFF
      // needs, etc.) - see lib/liff.ts. This deliberately NEVER calls
      // liff.login(): the mandatory login redirect used to gate voting
      // entirely, and real testing showed it repeatedly breaking (wrong
      // redirectUri landing users back on "/" stuck on "Logging in...").
      // LINE identity is now a silent, best-effort attachment only - if
      // isLoggedIn() already happens to be true (typically LINE's own
      // in-app browser), the name field is prefilled and the vote is
      // sent with a verified id token; otherwise voting proceeds with a
      // manually-typed name and no token at all. Either way this never
      // blocks the form - see `ready` below, which only ever decides
      // between a prefilled and an empty name field.
      const ok = await ensureLiffInit();
      if (!cancelled && ok && liff.isLoggedIn()) {
        try {
          const profile = await liff.getProfile();
          if (!cancelled) {
            setDisplayName(profile.displayName);
            setIdToken(liff.getIDToken() || "");
          }
        } catch (e) {
          console.warn("LIFF profile fetch failed, falling back to manual name entry", e);
        }
      }
      if (!cancelled) setReady(true);
    }

    initLiff();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    // Wrapped in a microtask (rather than reading localStorage directly
    // in the effect body) so this reads as "synchronize with an
    // external system," not "derive state during render" - matches
    // loadVotes below, which defers its setState calls the same way by
    // virtue of being async.
    queueMicrotask(() => {
      try {
        setAdminToken(new URLSearchParams(window.location.search).get("admin") || "");
      } catch {
        // No admin param, or an unparseable query string - either way
        // this trip's poll page just shows no "Lock & Generate Plan"
        // button, same as an ordinary voter link.
      }

      try {
        setHasVotedOnThisDevice(localStorage.getItem(`voted:${id}`) === "1");

        const anonIdKey = `anonVoterId:${id}`;
        let storedAnonId = localStorage.getItem(anonIdKey);
        if (!storedAnonId) {
          storedAnonId = crypto.randomUUID();
          localStorage.setItem(anonIdKey, storedAnonId);
        }
        setAnonId(storedAnonId);
      } catch {
        // localStorage can be unavailable (private browsing, blocked
        // site data) - hasVotedOnThisDevice is only a UI label, not a
        // security control, so failing open costs nothing there. An
        // empty anonId just means this submission won't dedup against a
        // prior one server-side (see lib/store.ts's addPollVote).
      }
    });
  }, [id]);

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

    const trimmedName = displayName.trim();
    if (!trimmedName) {
      setError("Please enter your name to vote.");
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

    // Fetched fresh here rather than trusting the `idToken` state value
    // cached since page load - diagnosis confirmed this call itself
    // never throws, but re-checking at the actual moment of use is
    // cheap insurance against a token that's gone stale while the form
    // was open, and keeps the request's real identity in one place.
    let liffIdToken = "";
    try {
      if (liff.isLoggedIn()) {
        liffIdToken = liff.getIDToken() || "";
      }
    } catch (tokenError) {
      console.error("liff.getIDToken() failed at submit time:", tokenError);
      setError("Something went wrong submitting your vote. Please try again.");
      setIsSubmitting(false);
      return;
    }

    try {
      const headers: Record<string, string> = { "Content-Type": "application/json" };
      if (liffIdToken) {
        // A verified LINE session was silently attached - send it so
        // the server trusts claims.sub, not just the typed name (see
        // app/api/poll/[id]/route.ts's POST handler). Omitted entirely
        // when there is no token: the server treats a request with NO
        // Authorization header as an intentional anonymous vote, not an
        // error.
        headers.Authorization = `Bearer ${liffIdToken}`;
      }

      const response = await fetch(`/api/poll/${id}`, {
        method: "POST",
        headers,
        body: JSON.stringify({
          name: trimmedName,
          startDate,
          endDate,
          wishlist: wishlist.trim(),
          vibes,
          // Only meaningful without a verified LINE session - the server
          // ignores this field once a token is present, since lineUserId
          // is the dedup key then instead (see lib/store.ts's
          // addPollVote).
          ...(liffIdToken ? {} : { anonId }),
        }),
      });

      if (!response.ok) {
        const data = await response.json().catch(() => null);
        console.error(
          `Vote submission failed with HTTP ${response.status}:`,
          data?.error ?? "(no error body)"
        );
        throw new Error("Vote submission failed.");
      }

      const data = await response.json();
      setVotes(Array.isArray(data.votes) ? data.votes : []);

      if (!liffIdToken) {
        try {
          localStorage.setItem(`voted:${id}`, "1");
        } catch {
          // The vote itself already succeeded server-side either way.
        }
        setHasVotedOnThisDevice(true);
      }

      setStartDate("");
      setEndDate("");
      setWishlist("");
      setVibes([]);
    } catch (error) {
      console.error("Vote submission failed:", error);
      setError("Something went wrong submitting your vote. Please try again.");
    } finally {
      setIsSubmitting(false);
    }
  }

  async function handleLockAndGenerate() {
    setLockError("");
    setIsLocking(true);
    try {
      // admin_token is the actual authorization mechanism now - see
      // app/api/trigger-jarvis/route.ts's docstring. No LINE identity is
      // sent or checked on this path at all any more.
      const response = await fetch("/api/trigger-jarvis", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ trip_id: id, admin_token: adminToken }),
      });

      const data = await response.json().catch(() => null);

      if (!response.ok) {
        throw new Error(
          typeof data?.error === "string" ? data.error : "Trigger request failed."
        );
      }

      if (data?.itinerary) {
        setGeneratedPlan(data.itinerary as Itinerary);
      }
    } catch (error) {
      setLockError(
        error instanceof Error && error.message
          ? error.message
          : "Couldn't start plan generation. Please try again."
      );
    } finally {
      setIsLocking(false);
    }
  }

  return (
    <div className="min-h-screen pb-32">
      <PageHeader title="Trip Poll" tripId={id} />

      <main className="mx-auto max-w-md space-y-6 px-4 py-6">
        {lockError && <p className="text-sm text-red-400">{lockError}</p>}

        <BoardingPass id={id} votes={votes} />

        {generatedPlan && (
          <section className="rounded-3xl border border-emerald-500/20 bg-[#1A1A1A] p-5 shadow-2xl shadow-black/40">
            <div className="flex items-center justify-between gap-3">
              <h2 className="text-base font-semibold text-white">
                Plan ready: {generatedPlan.destination}
              </h2>
              <span className="shrink-0 rounded-full border border-emerald-500/20 bg-emerald-500/10 px-3 py-1 text-[11px] font-semibold uppercase tracking-wide text-emerald-400/90">
                Locked
              </span>
            </div>

            <ul className="mt-4 space-y-4">
              {generatedPlan.days.map((day) => (
                <li key={day.day} className="rounded-2xl border border-white/10 bg-[#141414] p-4">
                  <p className="text-sm font-semibold text-white">
                    Day {day.day} - {day.summary}
                  </p>
                  <ul className="mt-2 space-y-1.5">
                    {day.stops.map((stop, i) => (
                      <li key={i}>
                        {stop.travelFromPrevious && (
                          <p
                            className="flex items-center gap-1 pl-1 text-[11px] text-zinc-500"
                            title="Estimated drive time for this future date, based on typical traffic patterns - not live traffic conditions."
                          >
                            <span aria-hidden="true">🚗</span>
                            {formatTravelMinutes(stop.travelFromPrevious.durationMinutes)} (estimate)
                          </p>
                        )}
                        <p className="text-xs text-zinc-400">
                          <span className="text-zinc-500" aria-hidden="true">
                            {stop.slotType === "meal" ? "🍴 " : "📍 "}
                          </span>
                          {stop.text}
                        </p>
                      </li>
                    ))}
                  </ul>
                </li>
              ))}
            </ul>

            {generatedPlan.notes && (
              <p className="mt-4 text-sm text-zinc-300">{generatedPlan.notes}</p>
            )}
          </section>
        )}

        <section className="rounded-3xl border border-white/10 bg-[#1A1A1A] p-5 shadow-2xl shadow-black/40">
          <h2 className="text-base font-semibold text-white">
            Add your availability
          </h2>
          <p className="mt-1 text-sm text-zinc-400">
            Let the group know when you&apos;re free and what you&apos;re hoping for.
          </p>

          <form className="mt-4 space-y-4" onSubmit={handleSubmit}>
            <div>
              <label htmlFor="voterName" className={labelClass}>
                Your name
              </label>
              <input
                id="voterName"
                type="text"
                value={displayName}
                onChange={(event) => setDisplayName(event.target.value)}
                placeholder={ready ? "Your name" : "Checking LINE sign-in..."}
                maxLength={100}
                className={fieldClass}
              />
              {idToken && (
                <p className="mt-1.5 text-xs text-emerald-400/80">
                  Signed in with LINE - your vote is linked to your account.
                </p>
              )}
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

            {!idToken && hasVotedOnThisDevice && (
              <p className="text-sm text-zinc-400">
                You&apos;ve already voted on this device - submitting again
                will update your entry.
              </p>
            )}

            <button
              type="submit"
              disabled={isSubmitting}
              className="w-full rounded-full bg-zinc-200 px-4 py-3.5 text-base font-semibold text-zinc-900 shadow-md shadow-black/30 transition active:scale-[0.98] disabled:opacity-50"
            >
              {isSubmitting
                ? "Submitting..."
                : !idToken && hasVotedOnThisDevice
                  ? "Update your vote"
                  : "Submit my vote"}
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
