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
  Globe,
  Heart,
  ImageOff,
  Lock,
  MapPin,
  RefreshCw,
  Star,
  Undo2,
  X,
} from "lucide-react";
import {
  DECK_CATEGORY_LABELS,
  type DeckPlace,
  type DeckPlaceCategory,
  type SwipeChoice,
  type TasteAnswer,
} from "@/lib/tripTypes";

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

// The deck is eight Google searches run side by side, usually a second or
// two. Past SLOW_LOAD_MS the skeleton says so; past DECK_TIMEOUT_MS we stop
// waiting and offer a retry rather than leave a skeleton up forever.
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

// The card stack is sized from the viewport so the Undo/Pass/Like buttons
// below it stay on screen even on a short phone: everything else on the page
// (sticky bar, counters, buttons, hint, attribution, page padding) measures
// 19.1rem, so the card gets what's left, up to 32rem. Shared by the real stack
// and the skeleton so nothing jumps when the deck finishes loading.
const STACK_HEIGHT_CLASS = "h-[clamp(16rem,calc(100svh-19.2rem),32rem)]";
// While the "picks weren't saved" banner is showing it takes 4rem more, so
// the card gives that back rather than pushing the buttons off screen.
const STACK_HEIGHT_WITH_BANNER_CLASS = "h-[clamp(12rem,calc(100svh-23.2rem),32rem)]";

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

async function loadDeck(): Promise<DeckPlace[]> {
  let result: Awaited<ReturnType<typeof requestJson>>;
  try {
    result = await requestJson("/api/places/deck", DECK_TIMEOUT_MS);
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

// Two mounts asking for the deck at once - React StrictMode's dev
// double-mount, or a quick remount - share one request: it is eight billable
// Google searches, and the second would just be thrown away. The entry
// clears the moment the request settles (loadDeck gives up after
// DECK_TIMEOUT_MS, so it never outlives that), and a later visit always builds
// a fresh deck (Google's policies forbid holding Places content).
let deckInFlight: Promise<DeckPlace[]> | null = null;

function fetchDeck(): Promise<DeckPlace[]> {
  deckInFlight ??= loadDeck().finally(() => {
    deckInFlight = null;
  });
  return deckInFlight;
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
async function postSwipe(
  tripId: string,
  googlePlaceId: string,
  action: SwipeChoice,
  category: DeckPlaceCategory | undefined
): Promise<boolean> {
  try {
    const { ok } = await requestJson("/api/trip/swipe", SAVE_TIMEOUT_MS, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tripId, googlePlaceId, action, category }),
    });
    return ok;
  } catch {
    return false;
  }
}

// The finished quiz, handed over as the person's Taste Profile (see
// POST /api/taste-profile, which also marks the quiz as done for them).
// `keepalive` lets the request outlive the page, so closing the tab right after
// the last card doesn't lose it. Like postSwipe, a save with no answer within
// SAVE_TIMEOUT_MS counts as failed.
async function postTasteProfile(answers: TasteAnswer[]): Promise<boolean> {
  try {
    const { ok } = await requestJson("/api/taste-profile", SAVE_TIMEOUT_MS, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ answers }),
      keepalive: true,
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

// The swipe step speaks the Modern Editorial dialect, the same as the plan
// view and sign-in: a white ground, 1px slate rules, ink text, 6px corners
// and no shadows. Deep Signal Rose (rose-600) means "like", the one primary
// action and the keyboard focus ring - nothing else.
const focusClass =
  "outline-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-rose-600";
const buttonShape = `inline-flex items-center justify-center gap-2 rounded-md font-semibold transition-[background-color,border-color,color,scale] duration-150 ease-out active:scale-[0.98] ${focusClass}`;
const primaryButtonClass = `${buttonShape} h-12 bg-rose-600 px-5 text-sm text-white hover:bg-rose-700`;
// The full-width primary (52px, 16px label) - its own string rather than
// primaryButtonClass plus overrides, since two utilities for one property
// (text-sm / text-base) are decided by stylesheet order, not by which comes
// last in the class attribute.
const primaryButtonLargeClass = `${buttonShape} h-13 w-full bg-rose-600 px-6 text-base text-white hover:bg-rose-700`;
const secondaryButtonClass = `${buttonShape} h-12 border border-slate-300 bg-white px-5 text-sm text-slate-900 hover:border-slate-900`;
const ghostButtonClass = `${buttonShape} h-12 px-5 text-sm text-slate-700 hover:bg-slate-100`;

// Google requires this attribution wherever Places content is shown without
// a Google map. The text form is the permitted fallback for tight spaces;
// it must not be altered or translated.
function GoogleMapsAttribution() {
  return <p className="mt-3 text-center text-xs font-medium text-slate-500">Google Maps</p>;
}

function unsavedPicksMessage(count: number) {
  return count === 1 ? "1 pick wasn't saved." : `${count} picks weren't saved.`;
}

function SaveFailedBanner({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div
      role="alert"
      className="mb-4 flex items-center justify-between gap-3 rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700"
    >
      <span className="flex items-center gap-2 font-medium">
        <CircleAlert className="size-4 shrink-0 text-red-600" aria-hidden="true" />
        {message}
      </span>
      <button
        type="button"
        onClick={onRetry}
        className={`-my-3 shrink-0 rounded-md px-2 py-3 font-semibold underline underline-offset-2 ${focusClass}`}
      >
        Retry
      </button>
    </div>
  );
}

/** A key cap in the desktop hint line. */
function Key({ children }: { children: ReactNode }) {
  return (
    <kbd className="inline-flex h-5 min-w-6 items-center justify-center rounded border border-slate-300 bg-white px-1.5 text-slate-600">
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
      ? "bg-red-50 text-red-600 ring-red-200"
      : "bg-slate-100 text-slate-600 ring-slate-200";

  return (
    <section
      role={tone === "alert" ? "alert" : undefined}
      className="rounded-md border border-slate-200 bg-white p-6"
    >
      <div className={`flex size-10 items-center justify-center rounded-md ring-1 ${toneClass}`}>{icon}</div>
      <h2 className="mt-4 text-xl font-bold tracking-tight text-slate-900">{title}</h2>
      <p className="mt-1.5 text-sm leading-6 text-slate-600">{body}</p>
      {children}
    </section>
  );
}

// ---------------------------------------------------------------------
// The card
// ---------------------------------------------------------------------

type PhotoState = "loading" | "loaded" | "failed";

// The text block under the photo has a fixed minimum height - room for a
// two-line name and the row beneath it (117px with its top rule) - so the
// hairline between photo and text sits at the same height on every card (and
// in the skeleton), and a name starts in the same place on each.
const CARD_TEXT_CLASS = "flex min-h-[7.5rem] flex-col px-5 py-4";

/**
 * The visual face of one card: a photo on top, a hairline, and under it the
 * place's name, WHERE IN THE WORLD it is and which taste it stands for ("Rome
 * · History" - the deck spans the globe, so every card says where), and its
 * rating. The photo's loading state lives in SwipeDeck (not here), so a card
 * that is dealt back by Undo - or finished loading while it waited behind the
 * others - doesn't reload or flash a placeholder when it reaches the top.
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
      aria-label={`${place.name}, ${place.city}`}
      className="relative flex h-full select-none flex-col overflow-hidden rounded-md border border-slate-200 bg-white"
    >
      <div className="relative min-h-0 flex-1 bg-slate-100">
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
            <ImageOff className="size-8 text-slate-500" strokeWidth={1.5} aria-hidden="true" />
            <span className="text-sm font-medium text-slate-600">No photo</span>
          </div>
        )}

        {/* Google's own label for this exact place. It stays beside the theme in the text below, not instead of it: the search that surfaced a place isn't always the same thing (a mall can turn up under "cafes"). */}
        <span className="absolute left-3 top-3 max-w-[70%] truncate rounded-md border border-slate-200 bg-white px-2.5 py-1.5 text-xs font-semibold leading-none text-slate-700">
          {place.categoryLabel}
        </span>

        {credit && (
          <span className="absolute bottom-3 left-3 flex max-w-[80%] items-baseline gap-1.5 rounded-md bg-slate-900 px-2.5 py-1 text-xs font-medium text-white">
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
            className="absolute left-5 top-16 -rotate-12 rounded-md border-2 border-rose-600 bg-white px-3 py-1 text-2xl font-extrabold uppercase tracking-widest text-rose-600"
          >
            Like
          </span>
          <span
            style={{ opacity: passStamp }}
            className="absolute right-5 top-16 rotate-12 rounded-md border-2 border-slate-900 bg-white px-3 py-1 text-2xl font-extrabold uppercase tracking-widest text-slate-900"
          >
            Pass
          </span>
        </div>
      </div>

      {/* The line height leaves room for Thai tone marks, and letter-spacing stays at its default because tightening it can collide combining marks. */}
      <div className={`${CARD_TEXT_CLASS} border-t border-slate-200`}>
        <h2 className="line-clamp-2 text-xl font-bold leading-[1.4] text-slate-900">{place.name}</h2>

        {/* One row. Left: where in the world, and which taste this card measures - the second thing read, and the one that says why a place from the other side of the world is being asked about. Right: the rating. */}
        <div className="mt-1 flex items-center justify-between gap-3">
          <p className="flex min-w-0 items-center gap-1.5 text-[15px] leading-6 text-slate-600">
            <MapPin className="size-4 shrink-0 text-slate-500" aria-hidden="true" />
            <span className="min-w-0 truncate">
              <span className="font-semibold text-slate-900">{place.city}</span>
              <span aria-hidden="true"> &middot; </span>
              <span className="sr-only">, </span>
              {DECK_CATEGORY_LABELS[place.category] ?? place.category}
            </span>
          </p>

          <div className="flex shrink-0 items-center gap-1 text-[13px] leading-5">
            {place.rating !== null ? (
              <>
                <Star className="size-3.5 fill-amber-400 text-amber-400" aria-hidden="true" />
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
      </div>
    </article>
  );
}

/**
 * Stand-in for the whole deck while it loads - same stack, same heights,
 * same controls, same hairline, so the real thing drops in without anything
 * shifting. Exported so the page can show it while it's still waiting on the
 * session (without the caption, which only makes sense once places are being
 * fetched).
 */
export function SwipeDeckSkeleton({ slow = false, caption = false }: { slow?: boolean; caption?: boolean }) {
  const status = slow ? "Still looking, this can take a few seconds" : "Loading places...";

  return (
    <div role="status" aria-busy="true">
      <span className="sr-only">{status}</span>

      <div className="motion-safe:animate-pulse">
        <div className="mb-3 flex h-4 items-center justify-between">
          <div className="h-3 w-14 rounded-sm bg-slate-200" />
          <div className="h-3 w-20 rounded-sm bg-slate-200" />
        </div>
        <div className="mb-4 h-0.5 w-full bg-slate-200" />

        <div className={`relative mx-auto w-full ${STACK_HEIGHT_CLASS}`}>
          <div className="absolute inset-0 translate-y-[calc(20px+5%)] scale-90 rounded-md border border-slate-200 bg-white opacity-50" />
          <div className="absolute inset-0 translate-y-[calc(10px+2.5%)] scale-95 rounded-md border border-slate-200 bg-white opacity-80" />
          <div className="absolute inset-0 flex flex-col overflow-hidden rounded-md border border-slate-200 bg-white">
            <div className="min-h-0 flex-1 bg-slate-200" />
            <div className={`${CARD_TEXT_CLASS} border-t border-slate-200`}>
              <div className="space-y-2.5">
                <div className="h-5 w-3/4 rounded-sm bg-slate-200" />
                <div className="h-5 w-1/2 rounded-sm bg-slate-200" />
              </div>
              <div className="mt-3 flex items-center justify-between">
                <div className="h-4 w-32 rounded-sm bg-slate-200" />
                <div className="h-3.5 w-16 rounded-sm bg-slate-200" />
              </div>
            </div>
          </div>
        </div>

        <div className="mt-6 grid grid-cols-[auto_1fr_1fr] gap-3">
          <div className="h-13 w-24 rounded-md bg-slate-200" />
          <div className="h-13 rounded-md bg-slate-200" />
          <div className="h-13 rounded-md bg-slate-200" />
        </div>
      </div>

      {caption && (
        <p aria-hidden="true" className="mt-6 text-center text-sm text-slate-600">
          {slow ? status : "Picking places from around the world..."}
        </p>
      )}
    </div>
  );
}

interface TasteRow {
  category: DeckPlaceCategory;
  liked: number;
  total: number;
}

/** How many of each theme's cards were liked, most liked first (ties keep the deck's own order). */
function tasteRows(deck: DeckPlace[], choices: Record<string, SwipeChoice>): TasteRow[] {
  const rows = new Map<DeckPlaceCategory, TasteRow>();
  for (const place of deck) {
    const row = rows.get(place.category) ?? { category: place.category, liked: 0, total: 0 };
    row.total += 1;
    if (choices[place.id] === "LIKE") row.liked += 1;
    rows.set(place.category, row);
  }
  return [...rows.values()].sort((a, b) => b.liked - a.liked);
}

/**
 * The finish card: what was liked, theme by theme, and the way back. Its
 * Saving / Saved word covers both the card-by-card saves and the one that
 * hands the whole set over as the person's Taste Profile; either failing is
 * said in a banner with its own Retry.
 */
function DoneCard({
  tripId,
  headingRef,
  deck,
  choices,
  pendingSaves,
  failedCount,
  profileStatus,
  canUndo,
  onUndo,
  onStartOver,
  onRetry,
  onRetryProfile,
}: {
  tripId: string;
  headingRef: RefObject<HTMLHeadingElement | null>;
  deck: DeckPlace[];
  choices: Record<string, SwipeChoice>;
  pendingSaves: number;
  failedCount: number;
  profileStatus: "saving" | "saved" | "failed";
  canUndo: boolean;
  onUndo: () => void;
  onStartOver: () => void;
  onRetry: () => void;
  onRetryProfile: () => void;
}) {
  const liked = deck.filter((place) => choices[place.id] === "LIKE").length;
  const passed = deck.filter((place) => choices[place.id] === "DISLIKE").length;
  const saveFailed = failedCount > 0 || profileStatus === "failed";

  return (
    <div>
      {failedCount > 0 && <SaveFailedBanner message={unsavedPicksMessage(failedCount)} onRetry={onRetry} />}
      {profileStatus === "failed" && (
        <SaveFailedBanner message="Your taste profile wasn't saved." onRetry={onRetryProfile} />
      )}

      <section className="rounded-md border border-slate-200 bg-white">
        <div className="p-5">
          <div className="flex items-start justify-between gap-3">
            <h2
              ref={headingRef}
              tabIndex={-1}
              className="text-2xl font-bold leading-tight tracking-tight text-slate-900 outline-none"
            >
              You&apos;re all set
            </h2>
            {!saveFailed && (
              <span className="mt-1.5 inline-flex shrink-0 items-center gap-1.5 text-xs font-semibold text-slate-700">
                {pendingSaves > 0 || profileStatus === "saving" ? (
                  <>
                    <RefreshCw className="size-3 motion-safe:animate-spin" aria-hidden="true" />
                    Saving
                  </>
                ) : (
                  <>
                    <Check className="size-3.5" strokeWidth={3} aria-hidden="true" />
                    Saved
                  </>
                )}
              </span>
            )}
          </div>
          <p className="mt-1.5 text-sm text-slate-600">{summarize(liked, passed, deck.length)}</p>
        </div>

        <div className="border-t border-slate-200 p-5">
          <h3 className="text-sm font-semibold text-slate-900">Liked by theme</h3>
          <ul className="mt-3 divide-y divide-slate-200">
            {tasteRows(deck, choices).map((row) => (
              <li key={row.category} className="flex items-center justify-between gap-3 py-2.5 first:pt-0 last:pb-0">
                <span className="min-w-0 truncate text-sm font-medium text-slate-900">
                  {DECK_CATEGORY_LABELS[row.category] ?? row.category}
                </span>
                <span className="flex shrink-0 items-center gap-3">
                  <span aria-hidden="true" className="flex gap-1">
                    {Array.from({ length: row.total }, (_, index) => (
                      <span
                        key={index}
                        className={`h-1.5 w-5 rounded-full ${index < row.liked ? "bg-slate-900" : "bg-slate-200"}`}
                      />
                    ))}
                  </span>
                  <span className="w-12 text-right text-sm tabular-nums text-slate-600">
                    {row.liked} of {row.total}
                  </span>
                </span>
              </li>
            ))}
          </ul>
          <p className="mt-4 flex items-center gap-2 text-sm text-slate-600">
            <Lock className="size-4 shrink-0" aria-hidden="true" />
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
            <RefreshCw className="size-4" aria-hidden="true" />
            Review again
          </button>
          {canUndo && (
            <button type="button" onClick={onUndo} className={ghostButtonClass}>
              <Undo2 className="size-4" aria-hidden="true" />
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
 * Tinder-style taste quiz: fetches the worldwide deck (GET /api/places/deck -
 * 24 places from around the world, three for each of eight themes, the same
 * for everyone and tied to no trip or destination), lets the signed-in user
 * swipe each one right (like) or left (pass) - by dragging the card, the
 * Like/Pass buttons, or the arrow keys - and records every answer, together
 * with the theme the card stands for, via POST /api/trip/swipe.
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
 * Only place ids (and our own theme ids) are ever persisted. Names, photos
 * and ratings live in component state for the current visit and are
 * discarded after - Google's Places policies don't allow keeping them.
 *
 * When the last card has been answered and the card-by-card saves have
 * settled, the whole set is saved once more as the person's Taste Profile
 * (POST /api/taste-profile), which also marks the quiz as done for them: from
 * then on the poll and swipe pages skip it (lib/tasteProfile.ts). Answering
 * the same set again after an Undo sends nothing; a different set replaces it.
 *
 * `tripId` is only where answers are saved and where "Back to trip" leads: the
 * deck itself never depends on it.
 */
export function SwipeDeck({ tripId }: { tripId: string }) {
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
        const [places, saved] = await Promise.all([fetchDeck(), loadSavedChoices(tripId)]);
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
  }, [tripId, reloadKey]);

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

  // The finished quiz as the server takes it: every dealt place with its
  // answer ("" until the last card is answered). It is also the identity of an
  // answer set: the effect below sends each distinct set once.
  const tasteSignature = isDone
    ? JSON.stringify(
        deck.flatMap((place): TasteAnswer[] => {
          const action = choices[place.id];
          return action ? [{ googlePlaceId: place.id, category: place.category, action }] : [];
        })
      )
    : "";
  // The set last handed over (being sent, saved, or failed): one attempt per
  // distinct set, so a failure shows a Retry instead of looping, and giving
  // the same answers again after an Undo sends nothing.
  const profileTried = useRef("");
  const [profileSaved, setProfileSaved] = useState("");
  const [profileFailed, setProfileFailed] = useState("");
  const [profileAttempt, setProfileAttempt] = useState(0);
  const profileStatus = !tasteSignature
    ? "saving"
    : tasteSignature === profileSaved
      ? "saved"
      : tasteSignature === profileFailed
        ? "failed"
        : "saving";

  // When the last card has been answered and its own save has settled, the
  // whole set becomes the person's Taste Profile, and the quiz counts as done.
  useEffect(() => {
    if (!tasteSignature || pendingSaves > 0 || profileTried.current === tasteSignature) return;
    profileTried.current = tasteSignature;
    void postTasteProfile(JSON.parse(tasteSignature) as TasteAnswer[]).then((saved) => {
      // The set may have changed while this was in flight (Undo, Review again):
      // then this is no longer the answer to report.
      if (profileTried.current !== tasteSignature) return;
      if (saved) setProfileSaved(tasteSignature);
      else setProfileFailed(tasteSignature);
    });
  }, [tasteSignature, pendingSaves, profileAttempt]);

  function retryProfile() {
    profileTried.current = "";
    setProfileFailed("");
    setProfileAttempt((attempt) => attempt + 1);
  }

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

    // The theme the card was dealt for travels with the answer.
    const category = deck.find((candidate) => candidate.id === placeId)?.category;
    const previous = saveChains.current.get(placeId) ?? Promise.resolve();
    const request = previous.then(() => postSwipe(tripId, placeId, choice, category));
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
    return <SwipeDeckSkeleton slow={slowLoad} caption />;
  }

  if (loadState === "error") {
    return (
      <DeckMessage
        tone="alert"
        icon={<CircleAlert className="size-5" aria-hidden="true" />}
        title="Couldn't load places"
        body={loadError}
      >
        <div className="mt-6 flex flex-wrap gap-3">
          <button type="button" onClick={reload} className={primaryButtonClass}>
            <RefreshCw className="size-4" aria-hidden="true" />
            Try again
          </button>
        </div>
      </DeckMessage>
    );
  }

  if (deck.length === 0) {
    return (
      <DeckMessage
        tone="neutral"
        icon={<Globe className="size-5" aria-hidden="true" />}
        title="No places right now"
        body="We couldn't find any places to show. Try again in a little while."
      >
        <div className="mt-6 flex">
          <button type="button" onClick={reload} className={primaryButtonClass}>
            <RefreshCw className="size-4" aria-hidden="true" />
            Try again
          </button>
        </div>
      </DeckMessage>
    );
  }

  const likedCount = deck.filter((place) => choices[place.id] === "LIKE").length;
  const failedCount = Object.keys(failedSaves).length;
  const answered = deck.length - queue.length;

  if (queue.length === 0) {
    return (
      <>
        <DoneCard
          tripId={tripId}
          headingRef={doneHeadingRef}
          deck={deck}
          choices={choices}
          pendingSaves={pendingSaves}
          failedCount={failedCount}
          profileStatus={profileStatus}
          canUndo={canUndo}
          onUndo={undo}
          onStartOver={startOver}
          onRetry={retryFailedSaves}
          onRetryProfile={retryProfile}
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
        <div className="mb-3 flex items-center justify-between text-[13px] tabular-nums text-slate-600">
          <span>
            <span className="font-semibold text-slate-900">{answered}</span> of {deck.length}
          </span>
          <span className="flex items-center gap-1.5">
            {/* A fixed-size slot, so the spinner appearing doesn't nudge the count. */}
            <span className="flex size-3 items-center justify-center" title={pendingSaves > 0 ? "Saving" : undefined}>
              {pendingSaves > 0 && (
                <RefreshCw className="size-3 motion-safe:animate-spin" aria-label="Saving" />
              )}
            </span>
            <Heart className="size-3.5" aria-hidden="true" />
            {likedCount} liked
          </span>
        </div>
        <div
          role="progressbar"
          aria-label="Places answered"
          aria-valuemin={0}
          aria-valuemax={deck.length}
          aria-valuenow={answered}
          className="mb-4 h-0.5 w-full overflow-hidden bg-slate-200"
        >
          <div
            className="h-full bg-slate-900 transition-[width] duration-300 motion-reduce:transition-none"
            style={{ width: `${(answered / deck.length) * 100}%` }}
          />
        </div>

        {failedCount > 0 && <SaveFailedBanner message={unsavedPicksMessage(failedCount)} onRetry={retryFailedSaves} />}

        <div
          ref={stackRef}
          role="group"
          tabIndex={-1}
          aria-label="Place suggestions. Use the left and right arrow keys to pass or like, and Backspace to undo."
          className={`relative isolate mx-auto w-full outline-none ${
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

        <div className="mt-6 grid grid-cols-[auto_1fr_1fr] gap-3">
          <button
            type="button"
            onClick={undo}
            aria-disabled={!canUndo}
            aria-label="Undo last swipe"
            className={`${buttonShape} h-13 border border-slate-300 bg-white px-4 text-sm text-slate-700 hover:border-slate-900 aria-disabled:opacity-50 aria-disabled:hover:border-slate-300 aria-disabled:active:scale-100`}
          >
            <Undo2 className="size-4" aria-hidden="true" />
            Undo
          </button>
          <button
            type="button"
            onClick={() => decide("DISLIKE")}
            aria-label={`Pass on ${top.name}`}
            className={`${buttonShape} h-13 border border-slate-300 bg-white px-4 text-sm text-slate-900 hover:border-slate-900`}
          >
            <X className="size-[18px]" strokeWidth={2.25} aria-hidden="true" />
            Pass
          </button>
          <button
            type="button"
            onClick={() => decide("LIKE")}
            aria-label={`Like ${top.name}`}
            className={`${buttonShape} h-13 bg-rose-600 px-4 text-sm text-white hover:bg-rose-700`}
          >
            <Heart className="size-[18px]" strokeWidth={2.25} aria-hidden="true" />
            Like
          </button>
        </div>

        {/* Touch screens swipe; a mouse also gets the keyboard shortcuts spelled out. */}
        <p className="mt-5 flex min-h-5 items-center justify-center text-center text-xs text-slate-500 [@media(pointer:fine)]:hidden">
          Swipe right to like, left to pass
        </p>
        <p className="mt-5 hidden items-center justify-center gap-x-4 gap-y-1.5 text-xs text-slate-500 [@media(pointer:fine)]:flex">
          <span className="inline-flex items-center gap-1.5">
            <Key>
              <ArrowLeft className="size-3" aria-hidden="true" />
            </Key>
            Pass
          </span>
          <span className="inline-flex items-center gap-1.5">
            <Key>
              <ArrowRight className="size-3" aria-hidden="true" />
            </Key>
            Like
          </span>
          <span className="inline-flex items-center gap-1.5">
            <Key>
              <Delete className="size-3" aria-hidden="true" />
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
