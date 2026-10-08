"use client";

import Link from "next/link";
import {
  useEffect,
  useEffectEvent,
  useLayoutEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
  type RefObject,
} from "react";
import {
  ArrowLeft,
  ArrowRight,
  Check,
  CircleAlert,
  Delete,
  Heart,
  ImageOff,
  Lock,
  MapPin,
  RefreshCw,
  Star,
  Undo2,
  X,
} from "lucide-react";
import type { DeckPlace, SwipeChoice } from "@/lib/tripTypes";

// How far (px) a card has to be dragged for letting go to commit a swipe...
const SWIPE_DISTANCE_PX = 110;
// ...or, for a quick flick, how short the drag may be and how fast (px/ms) it
// must be going.
const FLICK_DISTANCE_PX = 40;
const FLICK_VELOCITY_PX_PER_MS = 0.55;
// The fly-off animation; the swipe is committed to the deck when it ends.
// With reduced motion the flight becomes a short fade in place.
const EXIT_MS = 240;
const REDUCED_MOTION_MS = 120;
// A card laid back by Undo flies in from the side it left.
const UNDO_MS = 320;
// Cards in the stack: three you can see, plus one fully transparent extra so
// its photo has already loaded by the time it's revealed - which also keeps
// the next three photos warm while you decide on the first.
const VISIBLE_CARDS = 3;
const RENDERED_CARDS = VISIBLE_CARDS + 1;
const MAX_TILT_DEG = 14;

// The deck is three Google searches, usually a second or two. Past
// SLOW_LOAD_MS the skeleton says so; past DECK_TIMEOUT_MS we stop waiting and
// offer a retry rather than leave a skeleton up forever.
const SLOW_LOAD_MS = 6000;
const DECK_TIMEOUT_MS = 20000;
// The same rule for the two small requests: an answer that never comes must
// not hold the deck on its skeleton or a save on its spinner.
const SAVED_CHOICES_TIMEOUT_MS = 10000;
const SAVE_TIMEOUT_MS = 15000;

// First-run nudge: the first card of someone who has never swiped tips toward
// "like" once, then settles. Remembered per browser.
const NUDGE_STORAGE_KEY = "tripPlanner.swipeNudgeSeen";
const NUDGE_DELAY_MS = 700;
const NUDGE_MS = 1100;

// Liked places listed on the finish card before "+N more".
const LIKED_ROWS_SHOWN = 4;

// The card stack is sized from the viewport so the Undo/Pass/Like buttons
// below it stay on screen even on a short phone: everything else on the page
// (sticky header, counters, buttons, hint, attribution, page padding) is
// 23.05rem, so the card gets what's left, up to 32rem. Shared by the real stack
// and the skeleton so nothing jumps when the deck finishes loading.
const STACK_HEIGHT_CLASS = "h-[clamp(16rem,calc(100svh-23.05rem),32rem)]";
// While the "picks weren't saved" banner is showing it takes 4rem more, so
// the card gives that back rather than pushing the buttons off screen.
const STACK_HEIGHT_WITH_BANNER_CLASS = "h-[clamp(12rem,calc(100svh-27.05rem),32rem)]";

const compactCount = new Intl.NumberFormat("en", { notation: "compact" });

function clamp(value: number, min: number, max: number) {
  return Math.min(Math.max(value, min), max);
}

// One transform shape for every card in every state (resting, dragged,
// flying off, stacked behind) - transitions only interpolate smoothly
// between transforms made of the same functions in the same order.
function cardTransform(x: string, y: string, rotateDeg: number, scale: number) {
  return `translate(${x}, ${y}) rotate(${rotateDeg}deg) scale(${scale})`;
}

// ---------------------------------------------------------------------
// Preferences and one-time flags
// ---------------------------------------------------------------------

function subscribeToReducedMotion(onChange: () => void) {
  const query = window.matchMedia("(prefers-reduced-motion: reduce)");
  query.addEventListener("change", onChange);
  return () => query.removeEventListener("change", onChange);
}

function usePrefersReducedMotion() {
  return useSyncExternalStore(
    subscribeToReducedMotion,
    () => window.matchMedia("(prefers-reduced-motion: reduce)").matches,
    () => false
  );
}

// localStorage can be missing or throw (private windows, blocked site data),
// so a browser that can't remember is treated as "already seen" - better to
// skip the nudge than to replay it on every visit.
function hasSeenNudge() {
  try {
    return window.localStorage.getItem(NUDGE_STORAGE_KEY) === "1";
  } catch {
    return true;
  }
}

function markNudgeSeen() {
  try {
    window.localStorage.setItem(NUDGE_STORAGE_KEY, "1");
  } catch {
    // Nothing to do: the nudge just isn't remembered.
  }
}

// ---------------------------------------------------------------------
// Data access
// ---------------------------------------------------------------------

// One JSON request with a deadline that covers the body as well as the
// headers: past `timeoutMs` the request is aborted and this throws an
// AbortError, so nothing on this page can wait on the network forever.
async function requestJson(
  url: string,
  timeoutMs: number,
  init?: RequestInit
): Promise<{ ok: boolean; status: number; data: unknown }> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { cache: "no-store", ...init, signal: controller.signal });
    const data: unknown = await response.json().catch(() => null);
    // An abort while the body was still arriving shows up above as a failed parse.
    if (controller.signal.aborted) throw new DOMException("The request timed out.", "AbortError");
    return { ok: response.ok, status: response.status, data };
  } finally {
    window.clearTimeout(timer);
  }
}

async function loadDeck(destination: string): Promise<DeckPlace[]> {
  let result: Awaited<ReturnType<typeof requestJson>>;
  try {
    result = await requestJson(
      `/api/places/deck?destination=${encodeURIComponent(destination)}`,
      DECK_TIMEOUT_MS
    );
  } catch (error) {
    throw new Error(
      error instanceof DOMException && error.name === "AbortError"
        ? "Finding places is taking longer than usual. Please try again."
        : navigator.onLine === false
          ? "You seem to be offline. Check your connection and try again."
          : "We couldn't load places right now. Please try again."
    );
  }

  if (!result.ok) {
    throw new Error(
      result.status === 429
        ? "You're loading places a little too fast - give it a minute and try again."
        : result.status === 401
          ? "Please sign in again to see places."
          : "We couldn't load places right now. Please try again."
    );
  }

  const places = (result.data as { places?: unknown } | null)?.places;
  return Array.isArray(places) ? (places as DeckPlace[]) : [];
}

// Two mounts asking for the same deck at once - React StrictMode's dev
// double-mount, or a quick remount - share one request: each is three
// billable Google searches, and the second would just be thrown away.
// Entries clear the moment the request settles (loadDeck gives up after
// DECK_TIMEOUT_MS, so none outlives that), and a later visit always builds a
// fresh deck (Google's policies forbid holding Places content).
const decksInFlight = new Map<string, Promise<DeckPlace[]>>();

function fetchDeck(destination: string): Promise<DeckPlace[]> {
  let request = decksInFlight.get(destination);
  if (!request) {
    request = loadDeck(destination).finally(() => decksInFlight.delete(destination));
    decksInFlight.set(destination, request);
  }
  return request;
}

// Best-effort: if this fails - or takes longer than SAVED_CHOICES_TIMEOUT_MS -
// the person just sees the full deck again, and re-swiping a place replaces
// their earlier answer, so nothing is lost.
async function loadSavedChoices(tripId: string): Promise<Record<string, SwipeChoice>> {
  try {
    const { ok, data } = await requestJson(
      `/api/trip/swipe?tripId=${encodeURIComponent(tripId)}`,
      SAVED_CHOICES_TIMEOUT_MS
    );
    if (!ok) return {};

    const swipes = (data as { swipes?: unknown } | null)?.swipes;
    const saved: Record<string, SwipeChoice> = {};
    for (const swipe of Array.isArray(swipes) ? swipes : []) {
      if (
        typeof swipe?.googlePlaceId === "string" &&
        (swipe.action === "LIKE" || swipe.action === "DISLIKE")
      ) {
        saved[swipe.googlePlaceId] = swipe.action;
      }
    }
    return saved;
  } catch {
    return {};
  }
}

// A save that gets no answer within SAVE_TIMEOUT_MS counts as failed, so it
// shows up as "wasn't saved" with a Retry instead of a spinner that never ends.
async function postSwipe(tripId: string, googlePlaceId: string, action: SwipeChoice): Promise<boolean> {
  try {
    const { ok } = await requestJson("/api/trip/swipe", SAVE_TIMEOUT_MS, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tripId, googlePlaceId, action }),
    });
    return ok;
  } catch {
    return false;
  }
}

function summarize(liked: number, passed: number, total: number) {
  const noun = total === 1 ? "place" : "places";
  if (liked === total) return total === 1 ? "You liked this place." : `You liked all ${total} places.`;
  if (passed === total) return total === 1 ? "You passed on this place." : `You passed on all ${total} places.`;
  return `You liked ${liked} of ${total} ${noun} and passed on ${passed}.`;
}

// ---------------------------------------------------------------------
// Shared pieces
// ---------------------------------------------------------------------

const primaryButtonClass =
  "inline-flex min-h-12 items-center justify-center gap-2 rounded-full bg-rose-500 px-5 text-sm font-semibold text-white shadow-md shadow-rose-500/20 outline-none transition hover:bg-rose-600 focus-visible:ring-4 focus-visible:ring-rose-200 active:scale-[0.98]";
// The full-width submit-sized primary (52-56px, 16px label) - its own string
// rather than primaryButtonClass plus overrides, since two utilities for one
// property (text-sm / text-base) are decided by stylesheet order, not by
// which comes last in the class attribute.
const primaryButtonLargeClass =
  "inline-flex min-h-14 w-full items-center justify-center gap-2 rounded-full bg-rose-500 px-6 text-base font-semibold text-white shadow-md shadow-rose-500/20 outline-none transition hover:bg-rose-600 focus-visible:ring-4 focus-visible:ring-rose-200 active:scale-[0.98]";
const secondaryButtonClass =
  "inline-flex min-h-12 items-center justify-center gap-2 rounded-full border border-slate-200 bg-white px-5 text-sm font-semibold text-slate-700 shadow-sm outline-none transition hover:bg-slate-50 focus-visible:ring-4 focus-visible:ring-slate-300 active:scale-[0.98]";
const ghostButtonClass =
  "inline-flex min-h-12 items-center justify-center gap-2 rounded-full px-5 text-sm font-semibold text-slate-700 outline-none transition hover:bg-slate-100 focus-visible:ring-4 focus-visible:ring-slate-300 active:scale-[0.98]";

/**
 * The ticket's tear line: a dashed Perforation with a half-moon notch bitten
 * out of each edge, built the same way as the poll page's boarding pass. The
 * notches overlap the card's own border, so the card must not clip its
 * children (the photo clips itself instead).
 */
function Perforation() {
  return (
    <div aria-hidden="true" className="relative border-t border-dashed border-slate-300">
      <span className="absolute -left-px -top-2.5 h-5 w-2.5 rounded-r-full border border-l-0 border-slate-200 bg-slate-50" />
      <span className="absolute -right-px -top-2.5 h-5 w-2.5 rounded-l-full border border-r-0 border-slate-200 bg-slate-50" />
    </div>
  );
}

// Google requires this attribution wherever Places content is shown without
// a Google map. The text form is the permitted fallback for tight spaces;
// it must not be altered or translated.
function GoogleMapsAttribution() {
  return <p className="mt-3 text-center text-xs font-medium text-slate-500">Google Maps</p>;
}

function SaveFailedBanner({ count, onRetry }: { count: number; onRetry: () => void }) {
  return (
    <div
      role="alert"
      className="mb-4 flex items-center justify-between gap-3 rounded-2xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700"
    >
      <span className="flex items-center gap-2 font-medium">
        <CircleAlert className="h-4 w-4 shrink-0 text-red-500" aria-hidden="true" />
        {count === 1 ? "1 pick wasn't saved." : `${count} picks weren't saved.`}
      </span>
      <button
        type="button"
        onClick={onRetry}
        className="-my-3 shrink-0 rounded-full px-2 py-3 font-semibold underline underline-offset-2 outline-none focus-visible:ring-4 focus-visible:ring-red-200"
      >
        Retry
      </button>
    </div>
  );
}

/** A key cap in the desktop hint line. */
function Key({ children }: { children: ReactNode }) {
  return (
    <kbd className="inline-flex h-5 min-w-6 items-center justify-center rounded-full border border-slate-200 bg-white px-1.5 text-slate-600 shadow-sm">
      {children}
    </kbd>
  );
}

function DeckMessage({
  icon,
  tone,
  title,
  body,
  children,
}: {
  icon: ReactNode;
  tone: "alert" | "neutral";
  title: string;
  body: string;
  children?: ReactNode;
}) {
  const toneClass =
    tone === "alert"
      ? "bg-red-50 text-red-500 ring-red-200"
      : "bg-slate-100 text-slate-500 ring-slate-200";

  return (
    <section
      role={tone === "alert" ? "alert" : undefined}
      className="rounded-3xl border border-slate-200 bg-white p-6 shadow-sm"
    >
      <div className={`flex h-12 w-12 items-center justify-center rounded-2xl ring-1 ${toneClass}`}>
        {icon}
      </div>
      <h2 className="mt-4 text-xl font-bold tracking-tight text-slate-900">{title}</h2>
      <p className="mt-1.5 text-sm leading-relaxed text-slate-600">{body}</p>
      {children}
    </section>
  );
}

// ---------------------------------------------------------------------
// The ticket
// ---------------------------------------------------------------------

type PhotoState = "loading" | "loaded" | "failed";

/**
 * The visual face of one card: a photo on top, a tear line, and a stub with
 * the name and rating. The photo's loading state lives in SwipeDeck (not
 * here), so a card that is dealt back by Undo - or finished loading while it
 * waited behind the others - doesn't reload or flash a placeholder when it
 * reaches the top.
 */
function PlaceCard({
  place,
  photoState,
  onPhotoState,
  likeStamp,
  passStamp,
}: {
  place: DeckPlace;
  photoState: PhotoState;
  onPhotoState: (placeId: string, state: "loaded" | "failed") => void;
  likeStamp: number;
  passStamp: number;
}) {
  const showPhoto = Boolean(place.photoUrl) && photoState !== "failed";
  // Google requires crediting a photo's author wherever the photo is shown -
  // and only while the photo is.
  const credit = showPhoto && photoState === "loaded" ? place.photoAttribution : null;

  return (
    <article
      aria-label={place.name}
      className="relative flex h-full select-none flex-col rounded-3xl border border-slate-200 bg-white shadow-xl shadow-slate-900/10"
    >
      <div className="relative min-h-0 flex-1 overflow-hidden rounded-t-[calc(1.5rem-1px)] bg-slate-100">
        {showPhoto ? (
          <>
            {photoState === "loading" && (
              <div className="absolute inset-0 bg-slate-200 motion-safe:animate-pulse" />
            )}
            {/* A plain <img>: the source is a same-origin redirect to a Google-hosted image, which next/image would only add a second proxy hop in front of. */}
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={place.photoUrl ?? undefined}
              alt={`Photo of ${place.name}`}
              draggable={false}
              onLoad={() => onPhotoState(place.id, "loaded")}
              onError={() => onPhotoState(place.id, "failed")}
              className={`h-full w-full object-cover transition-opacity duration-200 ${
                photoState === "loaded" ? "opacity-100" : "opacity-0"
              }`}
            />
          </>
        ) : (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-2">
            <ImageOff className="h-8 w-8 text-slate-400" strokeWidth={1.5} aria-hidden="true" />
            {/* Ink Body, not Muted: Muted drops to 4.35:1 on the Stub Grey fill. */}
            <span className="text-[11px] font-medium uppercase tracking-[0.1em] text-slate-600">
              No photo
            </span>
          </div>
        )}

        {/* A neutral label, not a per-category icon: the text is Google's own type for this exact place, but the search that surfaced it isn't always the same thing (a mall can turn up under "cafes"). */}
        <span className="absolute left-3 top-3 max-w-[70%] truncate rounded-full border border-slate-200 bg-white px-3 py-1.5 text-[11px] font-medium uppercase leading-none tracking-[0.1em] text-slate-600 shadow-sm">
          {place.categoryLabel}
        </span>

        {credit && (
          <span className="absolute bottom-3 left-3 flex max-w-[80%] items-baseline gap-1.5 rounded-full bg-slate-900/70 px-3 py-1 text-xs font-medium text-white">
            <span className="shrink-0 font-semibold uppercase tracking-wider">Photo</span>
            <span aria-hidden="true">&middot;</span>
            {credit.uri ? (
              <a
                href={credit.uri}
                target="_blank"
                rel="noopener noreferrer"
                className="-my-2 min-w-0 truncate py-2 underline decoration-white/50 underline-offset-2"
              >
                {credit.name}
              </a>
            ) : (
              <span className="min-w-0 truncate">{credit.name}</span>
            )}
          </span>
        )}

        <div aria-hidden="true" className="pointer-events-none absolute inset-0">
          <span
            style={{ opacity: likeStamp }}
            className="absolute left-5 top-16 -rotate-12 rounded-xl border-[3px] border-rose-500 bg-white px-3 py-1 text-2xl font-extrabold uppercase tracking-widest text-rose-500"
          >
            Like
          </span>
          <span
            style={{ opacity: passStamp }}
            className="absolute right-5 top-16 rotate-12 rounded-xl border-[3px] border-slate-700 bg-white px-3 py-1 text-2xl font-extrabold uppercase tracking-widest text-slate-700"
          >
            Pass
          </span>
        </div>
      </div>

      <Perforation />

      {/* The stub has a fixed height - room for a two-line name - so the tear line sits at the same height on every card; name and rating sit together in the middle of it. The line height leaves room for Thai tone marks, and letter-spacing stays at its default because tightening it can collide combining marks. */}
      <div className="flex min-h-[7.5rem] flex-col justify-center px-5 pb-5 pt-4">
        <h2 className="line-clamp-2 text-xl font-bold leading-[1.4] text-slate-900">{place.name}</h2>

        <div className="mt-2 flex items-center gap-1.5 text-sm">
          {place.rating !== null ? (
            <>
              <Star className="h-4 w-4 fill-amber-400 text-amber-400" aria-hidden="true" />
              <span className="sr-only">Rated</span>
              <span className="font-semibold text-slate-900">{place.rating.toFixed(1)}</span>
              {place.ratingCount !== null && (
                <span className="text-slate-500">({compactCount.format(place.ratingCount)})</span>
              )}
            </>
          ) : (
            <span className="text-slate-500">No ratings yet</span>
          )}
        </div>
      </div>
    </article>
  );
}

/**
 * Stand-in for the whole deck while it loads - same stack, same heights,
 * same controls, same tear line, so the real thing drops in without anything
 * shifting. Exported so the page can show it while it's still waiting on the
 * session.
 */
export function SwipeDeckSkeleton({ destination, slow = false }: { destination?: string; slow?: boolean }) {
  const status = slow ? "Still looking, this can take a few seconds" : "Loading places...";

  return (
    <div role="status" aria-busy="true">
      <span className="sr-only">{status}</span>

      <div className="motion-safe:animate-pulse">
        <div className="mb-3 flex h-4 items-center justify-between">
          <div className="h-3 w-14 rounded-full bg-slate-200" />
          <div className="h-3 w-20 rounded-full bg-slate-200" />
        </div>
        <div className="mb-4 h-1.5 w-full rounded-full bg-slate-200" />

        <div className={`relative mx-auto w-full ${STACK_HEIGHT_CLASS}`}>
          <div className="absolute inset-0 translate-y-[calc(20px+5%)] scale-90 rounded-3xl border border-slate-200 bg-white opacity-50" />
          <div className="absolute inset-0 translate-y-[calc(10px+2.5%)] scale-95 rounded-3xl border border-slate-200 bg-white opacity-80" />
          <div className="absolute inset-0 flex flex-col rounded-3xl border border-slate-200 bg-white shadow-xl shadow-slate-900/10">
            <div className="min-h-0 flex-1 rounded-t-[calc(1.5rem-1px)] bg-slate-200" />
            <Perforation />
            <div className="flex min-h-[7.5rem] flex-col justify-center px-5 pb-5 pt-4">
              <div className="space-y-2.5">
                <div className="h-5 w-3/4 rounded-full bg-slate-200" />
                <div className="h-5 w-1/2 rounded-full bg-slate-200" />
              </div>
              <div className="mt-3 h-4 w-24 rounded-full bg-slate-200" />
            </div>
          </div>
        </div>

        <div className="mt-7 flex items-start justify-center gap-7">
          <div className="h-16 w-16 p-2">
            <div className="h-12 w-12 rounded-full bg-slate-200" />
          </div>
          <div className="h-16 w-16 rounded-full bg-slate-200" />
          <div className="h-16 w-16 rounded-full bg-slate-200" />
        </div>
      </div>

      {destination && (
        <p aria-hidden="true" className="mt-6 text-center text-sm text-slate-500">
          {slow ? (
            status
          ) : (
            <>
              Finding great spots near <span className="font-semibold text-slate-700">{destination}</span>...
            </>
          )}
        </p>
      )}
    </div>
  );
}

/** The finish card: a ticket stub of what was picked, and the way back. */
function DoneCard({
  tripId,
  headingRef,
  likedPlaces,
  passedCount,
  total,
  pendingSaves,
  failedCount,
  canUndo,
  onUndo,
  onStartOver,
  onRetry,
}: {
  tripId: string;
  headingRef: RefObject<HTMLHeadingElement | null>;
  likedPlaces: DeckPlace[];
  passedCount: number;
  total: number;
  pendingSaves: number;
  failedCount: number;
  canUndo: boolean;
  onUndo: () => void;
  onStartOver: () => void;
  onRetry: () => void;
}) {
  const hiddenLiked = Math.max(likedPlaces.length - LIKED_ROWS_SHOWN, 0);

  return (
    <div>
      {failedCount > 0 && <SaveFailedBanner count={failedCount} onRetry={onRetry} />}

      <section className="rounded-3xl border border-slate-200 bg-white shadow-sm">
        <div className="p-5">
          <div className="flex items-start justify-between gap-3">
            <h2
              ref={headingRef}
              tabIndex={-1}
              className="text-2xl font-bold leading-tight tracking-tight text-slate-900 outline-none"
            >
              You&apos;re all set!
            </h2>
            {failedCount === 0 &&
              (pendingSaves > 0 ? (
                <span className="mt-1 inline-flex shrink-0 items-center gap-1.5 rounded-full border border-slate-200 bg-slate-100 px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.1em] text-slate-600">
                  <RefreshCw className="h-3 w-3 motion-safe:animate-spin" aria-hidden="true" />
                  Saving
                </span>
              ) : (
                <span className="mt-1 inline-flex shrink-0 items-center gap-1.5 rounded-full border border-emerald-200 bg-emerald-50 px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.1em] text-emerald-700">
                  <Check className="h-3 w-3" strokeWidth={3} aria-hidden="true" />
                  Saved
                </span>
              ))}
          </div>
          <p className="mt-1.5 text-sm text-slate-600">{summarize(likedPlaces.length, passedCount, total)}</p>
        </div>

        <Perforation />

        <div className="p-5">
          {likedPlaces.length > 0 && (
            <>
              <ul aria-label="Places you liked" className="divide-y divide-slate-200">
                {likedPlaces.slice(0, LIKED_ROWS_SHOWN).map((place) => (
                  <li key={place.id} className="flex items-baseline justify-between gap-3 py-2.5 first:pt-0">
                    <span className="min-w-0 truncate text-sm font-medium text-slate-900">{place.name}</span>
                    <span className="max-w-[40%] shrink-0 truncate text-xs text-slate-500">
                      {place.categoryLabel}
                    </span>
                  </li>
                ))}
              </ul>
              {hiddenLiked > 0 && <p className="pt-2.5 text-sm text-slate-500">+{hiddenLiked} more</p>}
            </>
          )}
          <p className={`flex items-center gap-2 text-sm text-slate-500 ${likedPlaces.length > 0 ? "mt-4" : ""}`}>
            <Lock className="h-4 w-4 shrink-0" aria-hidden="true" />
            Only you can see what you picked.
          </p>
        </div>
      </section>

      <div className="mt-6 space-y-3">
        <Link href={`/trip/poll/${encodeURIComponent(tripId)}`} className={primaryButtonLargeClass}>
          Back to trip
        </Link>
        <div className={`grid gap-3 ${canUndo ? "grid-cols-2" : "grid-cols-1"}`}>
          <button type="button" onClick={onStartOver} className={secondaryButtonClass}>
            <RefreshCw className="h-4 w-4" aria-hidden="true" />
            Review again
          </button>
          {canUndo && (
            <button type="button" onClick={onUndo} className={ghostButtonClass}>
              <Undo2 className="h-4 w-4" aria-hidden="true" />
              Undo last swipe
            </button>
          )}
        </div>
      </div>

      <GoogleMapsAttribution />
    </div>
  );
}

// ---------------------------------------------------------------------
// The deck
// ---------------------------------------------------------------------

/**
 * Tinder-style place picker: fetches a deck of places around `destination`
 * (GET /api/places/deck), lets the signed-in user swipe each one right
 * (like) or left (pass) - by dragging the card, the Like/Pass buttons, or
 * the arrow keys - and records every answer via POST /api/trip/swipe.
 *
 * Saves are optimistic: the next card appears immediately and the answer
 * is written in the background, so a slow network never stalls the deck.
 * A write that fails isn't lost silently - it's held and offered back as
 * a "Retry" banner. On load the user's earlier answers for this trip are
 * fetched too (GET /api/trip/swipe), so a refresh resumes where they left
 * off, and "Review again" lets them revisit everything (a new answer simply
 * replaces the old one server-side).
 *
 * Undo takes back the last swipe (one step): the card flies back in and the
 * counts follow. There is no endpoint to delete an answer, so until the
 * person answers that card again the earlier answer simply stays saved.
 *
 * Only place ids are ever persisted. Names, photos and ratings live in
 * component state for the current visit and are discarded after - Google's
 * Places policies don't allow keeping them.
 *
 * Pair with a `key` that changes with `destination` if the destination can
 * change while mounted, so the deck remounts and reloads.
 */
export function SwipeDeck({
  tripId,
  destination,
  onChangeDestination,
}: {
  tripId: string;
  destination: string;
  /** Offers a "Try a different place" action in the empty/error states. */
  onChangeDestination?: () => void;
}) {
  const reducedMotion = usePrefersReducedMotion();

  const [loadState, setLoadState] = useState<"loading" | "ready" | "error">("loading");
  const [slowLoad, setSlowLoad] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [reloadKey, setReloadKey] = useState(0);

  // `deck` is every place dealt this visit; `queue` is what's still left to
  // answer, in order; `choices` is the answer to each place so far
  // (including ones restored from a previous visit).
  const [deck, setDeck] = useState<DeckPlace[]>([]);
  const [queue, setQueue] = useState<DeckPlace[]>([]);
  const [choices, setChoices] = useState<Record<string, SwipeChoice>>({});
  const [photoStates, setPhotoStates] = useState<Record<string, "loaded" | "failed">>({});

  const [pendingSaves, setPendingSaves] = useState(0);
  const [failedSaves, setFailedSaves] = useState<Record<string, SwipeChoice>>({});

  const [drag, setDrag] = useState({ x: 0, y: 0, active: false });
  const [exiting, setExiting] = useState<SwipeChoice | null>(null);
  // Set by Undo for the card it lays back (and which side it flies in from).
  const [reentry, setReentry] = useState<{ from: SwipeChoice } | null>(null);
  const [nudgeDue, setNudgeDue] = useState(false);
  // For screen readers: one sentence per answer, in a polite live region.
  const [announcement, setAnnouncement] = useState("");

  const dragStart = useRef<{ x: number; y: number; time: number } | null>(null);
  const exitTimer = useRef<number | null>(null);
  // True from the moment a card is answered until its fly-off finishes. A
  // ref rather than the `exiting` state, which only changes on the next
  // render - too late to stop a second activation in the same tick (a
  // double-click, a ghost tap after a touch) from answering the same card
  // twice and then skipping a real one when both timers fire.
  const isDeciding = useRef(false);
  // The most recent answer given this visit - what Undo takes back. A ref for
  // the same reason as `isDeciding`: two presses in one tick must not both
  // see it and deal the same card back twice. `canUndo` is its on-screen twin.
  const lastAnswer = useRef<{ place: DeckPlace; choice: SwipeChoice } | null>(null);
  const [canUndo, setCanUndo] = useState(false);
  const topCardRef = useRef<HTMLDivElement | null>(null);
  const doneHeadingRef = useRef<HTMLHeadingElement | null>(null);
  const stackRef = useRef<HTMLDivElement | null>(null);
  const wasDone = useRef(false);
  // The nudge or Undo animation currently running on the top card, so
  // grabbing the card can cancel it instead of fighting it.
  const activeAnimation = useRef<Animation | null>(null);
  // The answer each place currently stands at as far as the person is
  // concerned - set the moment they swipe and dropped by Undo. A save that
  // finishes after that decides whether its failure is still worth showing.
  const standingAnswers = useRef<Record<string, SwipeChoice>>({});
  // Saves for one place run one after another, so a quick swipe, Undo and
  // swipe the other way can't arrive out of order and leave the old answer
  // stored last.
  const saveChains = useRef(new Map<string, Promise<unknown>>());

  useEffect(() => {
    let cancelled = false;
    const slowTimer = window.setTimeout(() => setSlowLoad(true), SLOW_LOAD_MS);

    async function load() {
      try {
        const [places, saved] = await Promise.all([fetchDeck(destination), loadSavedChoices(tripId)]);
        if (cancelled) return;

        standingAnswers.current = { ...saved };
        lastAnswer.current = null;
        setCanUndo(false);
        setDeck(places);
        setChoices(saved);
        setQueue(places.filter((place) => !saved[place.id]));
        // Only someone who has never answered anything here, in a browser that
        // hasn't shown it yet.
        setNudgeDue(places.length > 0 && Object.keys(saved).length === 0 && !hasSeenNudge());
        setLoadState("ready");
      } catch (error) {
        if (cancelled) return;
        setLoadError(
          error instanceof Error && error.message
            ? error.message
            : "We couldn't load places right now. Please try again."
        );
        setLoadState("error");
      } finally {
        window.clearTimeout(slowTimer);
      }
    }

    load();
    return () => {
      cancelled = true;
      window.clearTimeout(slowTimer);
    };
  }, [tripId, destination, reloadKey]);

  useEffect(() => {
    return () => {
      if (exitTimer.current !== null) window.clearTimeout(exitTimer.current);
    };
  }, []);

  // The first card of a first-time swiper tips toward "like" once, then
  // settles - the only teaching the gesture gets. Starts a beat after the
  // deck appears (so it isn't missed) and is called off by the first touch.
  const playNudge = useEffectEvent(() => {
    const card = topCardRef.current;
    if (!card || reducedMotion) return;
    activeAnimation.current = card.animate(
      [
        { transform: cardTransform("0px", "0px", 0, 1) },
        { transform: cardTransform("36px", "0px", 4, 1), offset: 0.4 },
        { transform: cardTransform("0px", "0px", 0, 1) },
      ],
      { duration: NUDGE_MS, easing: "cubic-bezier(0.4, 0, 0.2, 1)" }
    );
    markNudgeSeen();
  });

  useEffect(() => {
    if (!nudgeDue) return;
    const timer = window.setTimeout(() => {
      playNudge();
      setNudgeDue(false);
    }, NUDGE_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [nudgeDue]);

  // Lays an undone card back down. Runs before paint (the card is mounted at
  // rest this render), so it never shows at rest for a frame first.
  const playReentry = useEffectEvent((from: SwipeChoice) => {
    const card = topCardRef.current;
    if (!card) return undefined;
    const direction = from === "LIKE" ? 1 : -1;
    const animation = card.animate(
      reducedMotion
        ? [{ opacity: 0 }, { opacity: 1 }]
        : [
            { transform: cardTransform(`${direction * 140}%`, "0px", direction * 20, 1), opacity: 0 },
            { transform: cardTransform("0px", "0px", 0, 1), opacity: 1 },
          ],
      { duration: reducedMotion ? REDUCED_MOTION_MS : UNDO_MS, easing: "cubic-bezier(0.2, 0.8, 0.2, 1)" }
    );
    activeAnimation.current = animation;
    return () => animation.cancel();
  });

  useLayoutEffect(() => {
    if (!reentry) return;
    return playReentry(reentry.from);
  }, [reentry]);

  // Focus follows the screen. Into the finish card's heading when the deck is
  // done, and back onto the deck when "Review again" or Undo leaves it - in
  // both cases the button that was just pressed unmounts, and without this
  // keyboard and screen-reader users would drop to the top of the page.
  const isDone = loadState === "ready" && deck.length > 0 && queue.length === 0;
  useEffect(() => {
    if (isDone) {
      wasDone.current = true;
      doneHeadingRef.current?.focus({ preventScroll: true });
    } else if (wasDone.current) {
      wasDone.current = false;
      stackRef.current?.focus({ preventScroll: true });
    }
  }, [isDone]);

  function cancelMotion() {
    activeAnimation.current?.cancel();
    activeAnimation.current = null;
    if (nudgeDue) {
      setNudgeDue(false);
      markNudgeSeen();
    }
  }

  function reload() {
    setLoadState("loading");
    setSlowLoad(false);
    setPhotoStates({});
    setReloadKey((key) => key + 1);
  }

  async function saveChoice(placeId: string, choice: SwipeChoice) {
    setPendingSaves((count) => count + 1);

    const previous = saveChains.current.get(placeId) ?? Promise.resolve();
    const request = previous.then(() => postSwipe(tripId, placeId, choice));
    saveChains.current.set(placeId, request);
    const saved = await request;
    if (saveChains.current.get(placeId) === request) saveChains.current.delete(placeId);

    setPendingSaves((count) => count - 1);
    setFailedSaves((current) => {
      const next = { ...current };
      if (saved) {
        delete next[placeId];
      } else if (standingAnswers.current[placeId] === choice) {
        next[placeId] = choice;
      }
      return next;
    });
  }

  function retryFailedSaves() {
    for (const [placeId, choice] of Object.entries(failedSaves)) {
      void saveChoice(placeId, choice);
    }
  }

  function decide(choice: SwipeChoice) {
    const place = queue[0];
    if (!place || isDeciding.current) return;
    isDeciding.current = true;
    cancelMotion();

    const answered = deck.length - queue.length + 1;
    standingAnswers.current[place.id] = choice;
    setAnnouncement(
      `${choice === "LIKE" ? "Liked" : "Passed on"} ${place.name}. ${answered} of ${deck.length}.${
        answered === deck.length ? " All done." : ""
      }`
    );

    // The card starts flying off now and the answer is sent straight away;
    // the deck itself only advances when the animation ends.
    setExiting(choice);
    void saveChoice(place.id, choice);

    exitTimer.current = window.setTimeout(() => {
      exitTimer.current = null;
      isDeciding.current = false;
      setQueue((current) => current.slice(1));
      setChoices((current) => ({ ...current, [place.id]: choice }));
      lastAnswer.current = { place, choice };
      setCanUndo(true);
      setExiting(null);
      setDrag({ x: 0, y: 0, active: false });
    }, reducedMotion ? REDUCED_MOTION_MS : EXIT_MS);
  }

  function undo() {
    const last = lastAnswer.current;
    if (!last || isDeciding.current) return;
    const { place, choice } = last;
    lastAnswer.current = null;
    setCanUndo(false);
    cancelMotion();

    delete standingAnswers.current[place.id];
    setAnnouncement(`Brought back ${place.name}. ${deck.length - queue.length - 1} of ${deck.length}.`);
    setQueue((current) => [place, ...current]);
    setChoices((current) => {
      const next = { ...current };
      delete next[place.id];
      return next;
    });
    // The pick it was waiting to save no longer stands, so it isn't worth a
    // banner (or a retry that would put the old answer back).
    setFailedSaves((current) => {
      const next = { ...current };
      delete next[place.id];
      return next;
    });
    setDrag({ x: 0, y: 0, active: false });
    setReentry({ from: choice });
  }

  function startOver() {
    cancelMotion();
    setChoices({});
    setQueue(deck);
    // A photo that failed once gets another try; the ones that loaded stay.
    setPhotoStates((current) =>
      Object.fromEntries(Object.entries(current).filter(([, state]) => state === "loaded"))
    );
    lastAnswer.current = null;
    setCanUndo(false);
    setAnnouncement(`Starting over. 0 of ${deck.length}.`);
  }

  function handlePhotoState(placeId: string, state: "loaded" | "failed") {
    setPhotoStates((current) => (current[placeId] === state ? current : { ...current, [placeId]: state }));
  }

  function handlePointerDown(event: ReactPointerEvent<HTMLDivElement>) {
    if (isDeciding.current) return;
    // Only the primary button drags; touch and pen report button 0 too.
    if (event.pointerType === "mouse" && event.button !== 0) return;
    // Let the photo-credit link be clicked instead of starting a drag.
    if ((event.target as HTMLElement).closest("a")) return;

    cancelMotion();
    event.currentTarget.setPointerCapture(event.pointerId);
    dragStart.current = { x: event.clientX, y: event.clientY, time: event.timeStamp };
    setDrag({ x: 0, y: 0, active: true });
  }

  function handlePointerMove(event: ReactPointerEvent<HTMLDivElement>) {
    const start = dragStart.current;
    if (!start) return;
    setDrag({ x: event.clientX - start.x, y: event.clientY - start.y, active: true });
  }

  function handlePointerUp(event: ReactPointerEvent<HTMLDivElement>) {
    const start = dragStart.current;
    if (!start) return;
    dragStart.current = null;

    const dx = event.clientX - start.x;
    const velocity = dx / Math.max(event.timeStamp - start.time, 1);
    const farEnough = Math.abs(dx) >= SWIPE_DISTANCE_PX;
    const fastEnough = Math.abs(dx) >= FLICK_DISTANCE_PX && Math.abs(velocity) >= FLICK_VELOCITY_PX_PER_MS;

    if (farEnough || fastEnough) {
      decide(dx > 0 ? "LIKE" : "DISLIKE");
    } else {
      setDrag({ x: 0, y: 0, active: false });
    }
  }

  // The browser took the gesture over (e.g. it became a page scroll).
  function handlePointerCancel() {
    dragStart.current = null;
    setDrag({ x: 0, y: 0, active: false });
  }

  // Arrow keys answer, Backspace undoes - from anywhere on the page that
  // isn't a text field, so the hint line under the deck is true without
  // first tabbing into it. An Effect Event, so the listener below is added
  // once yet always sees the current deck.
  const handleShortcut = useEffectEvent((event: KeyboardEvent) => {
    // A held key auto-repeats - one deliberate press should answer one
    // card, not run on through the deck.
    if (event.repeat || event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey) return;

    const target = event.target;
    if (
      target instanceof HTMLElement &&
      (target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName))
    ) {
      return;
    }

    if (event.key === "ArrowRight") {
      event.preventDefault();
      decide("LIKE");
    } else if (event.key === "ArrowLeft") {
      event.preventDefault();
      decide("DISLIKE");
    } else if (event.key === "Backspace") {
      event.preventDefault();
      undo();
    }
  });

  useEffect(() => {
    const listener = (event: KeyboardEvent) => handleShortcut(event);
    document.addEventListener("keydown", listener);
    return () => document.removeEventListener("keydown", listener);
  }, []);

  if (loadState === "loading") {
    return <SwipeDeckSkeleton destination={destination} slow={slowLoad} />;
  }

  if (loadState === "error") {
    return (
      <DeckMessage
        tone="alert"
        icon={<CircleAlert className="h-6 w-6" aria-hidden="true" />}
        title="Couldn't load places"
        body={loadError}
      >
        <div className="mt-6 flex flex-wrap gap-3">
          <button type="button" onClick={reload} className={primaryButtonClass}>
            <RefreshCw className="h-4 w-4" aria-hidden="true" />
            Try again
          </button>
          {onChangeDestination && (
            <button type="button" onClick={onChangeDestination} className={secondaryButtonClass}>
              Change place
            </button>
          )}
        </div>
      </DeckMessage>
    );
  }

  if (deck.length === 0) {
    return (
      <DeckMessage
        tone="neutral"
        icon={<MapPin className="h-6 w-6" aria-hidden="true" />}
        title="No places found"
        body={`We couldn't find anything to suggest near "${destination}". Try a nearby landmark or a broader area.`}
      >
        {onChangeDestination && (
          <div className="mt-6 flex">
            <button type="button" onClick={onChangeDestination} className={primaryButtonClass}>
              Try a different place
            </button>
          </div>
        )}
      </DeckMessage>
    );
  }

  const likedPlaces = deck.filter((place) => choices[place.id] === "LIKE");
  const failedCount = Object.keys(failedSaves).length;
  const answered = deck.length - queue.length;

  if (queue.length === 0) {
    return (
      <>
        <DoneCard
          tripId={tripId}
          headingRef={doneHeadingRef}
          likedPlaces={likedPlaces}
          passedCount={deck.filter((place) => choices[place.id] === "DISLIKE").length}
          total={deck.length}
          pendingSaves={pendingSaves}
          failedCount={failedCount}
          canUndo={canUndo}
          onUndo={undo}
          onStartOver={startOver}
          onRetry={retryFailedSaves}
        />
        <p role="status" className="sr-only">
          {announcement}
        </p>
      </>
    );
  }

  const top = queue[0];
  // 0..1: how far the top card has been pulled toward leaving - the card
  // behind it grows into place as this rises (fully, once it's flying off).
  const pull = exiting ? 1 : clamp(Math.abs(drag.x) / SWIPE_DISTANCE_PX, 0, 1);
  const likeStamp = exiting ? (exiting === "LIKE" ? 1 : 0) : clamp(drag.x / SWIPE_DISTANCE_PX, 0, 1);
  const passStamp = exiting ? (exiting === "DISLIKE" ? 1 : 0) : clamp(-drag.x / SWIPE_DISTANCE_PX, 0, 1);

  function cardStyle(depth: number): CSSProperties {
    const zIndex = (RENDERED_CARDS - depth) * 10;

    if (depth > 0) {
      const settled = depth === 1 ? 1 - 0.6 * pull : depth;
      // Each step back shows 10px of its card below the one in front. Scaling
      // about the centre also lifts a card's bottom edge by 2.5% of its own
      // height per step, so the drop adds that back - a fixed px offset alone
      // all but vanishes on a tall card.
      return {
        zIndex,
        opacity: depth >= VISIBLE_CARDS ? 0 : 1,
        transform: cardTransform("0px", `calc(${settled * 10}px + ${settled * 2.5}%)`, 0, 1 - settled * 0.05),
        // No easing while a finger is dragging the top card - the card
        // behind has to track it frame for frame, not lag a step behind.
        transition:
          reducedMotion || (drag.active && !exiting) ? "none" : "transform 300ms ease, opacity 300ms ease",
      };
    }

    if (exiting) {
      // Reduced motion: no flight, no tilt - the card stays where it is and
      // fades, with its stamp still showing which way it went.
      if (reducedMotion) {
        return {
          zIndex,
          opacity: 0,
          transform: cardTransform(`${drag.x}px`, `${drag.y}px`, 0, 1),
          transition: `opacity ${REDUCED_MOTION_MS}ms linear`,
          touchAction: "pan-y",
        };
      }
      const direction = exiting === "LIKE" ? 1 : -1;
      return {
        zIndex,
        opacity: 0,
        transform: cardTransform(`${direction * 140}%`, `${drag.y * 0.4}px`, direction * 20, 1),
        transition: `transform ${EXIT_MS}ms ease-in, opacity ${EXIT_MS}ms ease-in`,
        touchAction: "pan-y",
      };
    }

    return {
      zIndex,
      transform: drag.active
        ? cardTransform(
            `${drag.x}px`,
            `${drag.y}px`,
            reducedMotion ? 0 : clamp(drag.x / 18, -MAX_TILT_DEG, MAX_TILT_DEG),
            1
          )
        : cardTransform("0px", "0px", 0, 1),
      transition: drag.active || reducedMotion ? "none" : "transform 300ms cubic-bezier(0.2, 0.8, 0.2, 1)",
      // Vertical pans still scroll the page; horizontal ones are ours.
      touchAction: "pan-y",
    };
  }

  return (
    <>
      <div>
        <div className="mb-3 flex items-center justify-between text-xs font-semibold uppercase tracking-wider text-slate-500">
          <span>
            {answered} of {deck.length}
          </span>
          <span className="flex items-center gap-1.5">
            {/* A fixed-size slot, so the spinner appearing doesn't nudge the count. */}
            <span className="flex h-3 w-3 items-center justify-center" title={pendingSaves > 0 ? "Saving" : undefined}>
              {pendingSaves > 0 && (
                <RefreshCw className="h-3 w-3 motion-safe:animate-spin" aria-label="Saving" />
              )}
            </span>
            <Heart className="h-3 w-3" aria-hidden="true" />
            {likedPlaces.length} liked
          </span>
        </div>
        <div
          role="progressbar"
          aria-label="Places answered"
          aria-valuemin={0}
          aria-valuemax={deck.length}
          aria-valuenow={answered}
          className="mb-4 h-1.5 w-full overflow-hidden rounded-full bg-slate-200"
        >
          <div
            className="h-full rounded-full bg-rose-500 transition-[width] duration-300 motion-reduce:transition-none"
            style={{ width: `${(answered / deck.length) * 100}%` }}
          />
        </div>

        {failedCount > 0 && <SaveFailedBanner count={failedCount} onRetry={retryFailedSaves} />}

        <div
          ref={stackRef}
          role="group"
          tabIndex={-1}
          aria-label="Place suggestions. Use the left and right arrow keys to pass or like, and Backspace to undo."
          className={`relative mx-auto w-full outline-none ${
            failedCount > 0 ? STACK_HEIGHT_WITH_BANNER_CLASS : STACK_HEIGHT_CLASS
          }`}
        >
          {queue.slice(0, RENDERED_CARDS).map((place, depth) => {
            const isTop = depth === 0;
            return (
              <div
                key={place.id}
                ref={isTop ? topCardRef : undefined}
                inert={!isTop}
                style={cardStyle(depth)}
                className={`absolute inset-0 ${isTop ? "cursor-grab active:cursor-grabbing" : "pointer-events-none"}`}
                {...(isTop
                  ? {
                      onPointerDown: handlePointerDown,
                      onPointerMove: handlePointerMove,
                      onPointerUp: handlePointerUp,
                      onPointerCancel: handlePointerCancel,
                    }
                  : {})}
              >
                <PlaceCard
                  place={place}
                  photoState={place.photoUrl ? (photoStates[place.id] ?? "loading") : "failed"}
                  onPhotoState={handlePhotoState}
                  likeStamp={isTop ? likeStamp : 0}
                  passStamp={isTop ? passStamp : 0}
                />
              </div>
            );
          })}
        </div>

        <div className="mt-7 flex items-start justify-center gap-7">
          <div className="flex flex-col items-center gap-2">
            <div className="flex h-16 w-16 items-center justify-center">
              <button
                type="button"
                onClick={undo}
                aria-disabled={!canUndo}
                aria-label="Undo last swipe"
                className="flex h-12 w-12 items-center justify-center rounded-full border border-slate-200 text-slate-700 outline-none transition hover:bg-white focus-visible:ring-4 focus-visible:ring-slate-300 active:scale-[0.97] aria-disabled:opacity-50 aria-disabled:hover:bg-transparent aria-disabled:active:scale-100"
              >
                <Undo2 className="h-5 w-5" aria-hidden="true" />
              </button>
            </div>
            <span className="text-xs font-medium text-slate-500">Undo</span>
          </div>

          <div className="flex flex-col items-center gap-2">
            <button
              type="button"
              onClick={() => decide("DISLIKE")}
              aria-label={`Pass on ${top.name}`}
              className="flex h-16 w-16 items-center justify-center rounded-full border border-slate-200 bg-white text-slate-700 shadow-sm outline-none transition hover:bg-slate-50 focus-visible:ring-4 focus-visible:ring-slate-300 active:scale-[0.97]"
            >
              <X className="h-6 w-6" strokeWidth={2.25} aria-hidden="true" />
            </button>
            <span className="text-xs font-medium text-slate-500">Pass</span>
          </div>

          <div className="flex flex-col items-center gap-2">
            <button
              type="button"
              onClick={() => decide("LIKE")}
              aria-label={`Like ${top.name}`}
              className="flex h-16 w-16 items-center justify-center rounded-full bg-rose-500 text-white shadow-md shadow-rose-500/20 outline-none transition hover:bg-rose-600 focus-visible:ring-4 focus-visible:ring-rose-200 active:scale-[0.97]"
            >
              <Heart className="h-6 w-6" strokeWidth={2.25} aria-hidden="true" />
            </button>
            <span className="text-xs font-medium text-slate-500">Like</span>
          </div>
        </div>

        {/* Touch screens swipe; a mouse also gets the keyboard shortcuts spelled out. */}
        <p className="mt-5 flex min-h-5 items-center justify-center text-center text-xs text-slate-500 [@media(pointer:fine)]:hidden">
          Swipe right to like, left to pass
        </p>
        <p className="mt-5 hidden items-center justify-center gap-x-4 gap-y-1.5 text-xs text-slate-500 [@media(pointer:fine)]:flex">
          <span className="inline-flex items-center gap-1.5">
            <Key>
              <ArrowLeft className="h-3 w-3" aria-hidden="true" />
            </Key>
            Pass
          </span>
          <span className="inline-flex items-center gap-1.5">
            <Key>
              <ArrowRight className="h-3 w-3" aria-hidden="true" />
            </Key>
            Like
          </span>
          <span className="inline-flex items-center gap-1.5">
            <Key>
              <Delete className="h-3 w-3" aria-hidden="true" />
            </Key>
            Undo
          </span>
        </p>
        <GoogleMapsAttribution />
      </div>

      <p role="status" className="sr-only">
        {announcement}
      </p>
    </>
  );
}
