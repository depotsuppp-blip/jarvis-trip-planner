"use client";

import { useEffect, useState, type FormEvent } from "react";
import { signIn } from "next-auth/react";
import { ArrowLeft, CircleAlert, Mail, Plane } from "lucide-react";

const fieldClass =
  "mt-1.5 w-full rounded-2xl border border-slate-200 bg-slate-50 px-4 py-3.5 text-base text-slate-900 placeholder:text-slate-400 shadow-inner shadow-slate-900/5 outline-none transition focus:border-blue-400 focus:bg-white focus:ring-4 focus:ring-blue-100";

const primaryButtonClass =
  "flex w-full items-center justify-center gap-2.5 rounded-full bg-rose-500 px-4 py-3.5 text-sm font-semibold text-white shadow-md shadow-rose-500/20 outline-none transition hover:bg-rose-600 focus-visible:ring-4 focus-visible:ring-rose-200 active:scale-[0.98] disabled:opacity-50";

// Shown for ANY ?error= value. NextAuth's codes (OAuthCallback,
// Verification, Configuration, ...) are meant for developers - someone
// opening a trip link just needs to know it didn't work and to try again.
const LOGIN_FAILED_MESSAGE = "Login failed. Please try again.";

function GoogleIcon() {
  return (
    <svg viewBox="0 0 18 18" className="h-[18px] w-[18px]" aria-hidden="true">
      <path
        fill="#4285F4"
        d="M17.64 9.2c0-.64-.06-1.25-.16-1.84H9v3.48h4.84a4.14 4.14 0 0 1-1.8 2.72v2.26h2.9c1.7-1.56 2.7-3.87 2.7-6.62Z"
      />
      <path
        fill="#34A853"
        d="M9 18c2.43 0 4.47-.8 5.96-2.18l-2.9-2.26c-.8.54-1.84.86-3.06.86-2.35 0-4.34-1.59-5.05-3.72H.95v2.33A9 9 0 0 0 9 18Z"
      />
      <path
        fill="#FBBC05"
        d="M3.95 10.7A5.4 5.4 0 0 1 3.66 9c0-.59.1-1.17.29-1.7V4.97H.95A9 9 0 0 0 0 9c0 1.45.35 2.83.95 4.03l3-2.33Z"
      />
      <path
        fill="#EA4335"
        d="M9 3.58c1.32 0 2.51.46 3.44 1.35l2.58-2.58A9 9 0 0 0 .95 4.97l3 2.33C4.66 5.17 6.65 3.58 9 3.58Z"
      />
    </svg>
  );
}

/**
 * Where NextAuth should send the visitor once they're signed in, in order:
 *
 * 1. ?callbackUrl= - NextAuth adds it itself when it bounces a failed
 *    sign-in to /login (see `pages` in lib/auth.ts), so retrying still
 *    lands back on the trip they were trying to open. NextAuth re-validates
 *    it (same origin only) before honoring it, so passing a tampered value
 *    straight through is safe.
 * 2. `fallback` - for the standalone /login route, which has no trip of its
 *    own to come back to.
 * 3. The current URL - the inline gate on the trip poll/dashboard pages,
 *    where "here" is exactly where to return to.
 *
 * Browser-only (reads window), so call it from event handlers, never during
 * render.
 */
function resolveCallbackUrl(fallback?: string) {
  const fromQuery = new URLSearchParams(window.location.search).get(
    "callbackUrl"
  );
  return fromQuery || fallback || window.location.href;
}

/**
 * The entire sign-in UI. Rendered inline in place of the poll/dashboard
 * content whenever a visitor isn't signed in and didn't arrive with the
 * organizer's ?admin= token - see the gate check in app/trip/poll/[id]/
 * page.tsx and app/trip/dashboard/[id]/page.tsx - and standalone at /login
 * (app/login/page.tsx), which is where NextAuth redirects a failed
 * sign-in instead of showing its own error page. Deliberately calls
 * signIn() with an explicit provider id ("google"/"email") rather than the
 * bare signIn(), so NextAuth's own unstyled chooser/verify-request pages
 * never appear.
 *
 * Intentionally shows no trip id or other trip details: this is a signed-out
 * screen, and a raw database id on it reads as a bug rather than reassurance.
 *
 * fallbackCallbackUrl is where to land after sign-in when the URL itself
 * doesn't say - see resolveCallbackUrl. Omit it to return to the current
 * page.
 */
export function LoginScreen({
  fallbackCallbackUrl,
}: {
  fallbackCallbackUrl?: string;
}) {
  const [showEmailForm, setShowEmailForm] = useState(false);
  const [email, setEmail] = useState("");
  const [isSendingLink, setIsSendingLink] = useState(false);
  const [isRedirectingToGoogle, setIsRedirectingToGoogle] = useState(false);
  const [linkSentTo, setLinkSentTo] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    // NextAuth reports a failed sign-in by redirecting back here with
    // ?error=<Code> (see `pages` in lib/auth.ts) - turn that into the
    // same inline message the interactive failures below use. Read from
    // window.location rather than next/navigation's useSearchParams(),
    // which would force every page that renders this component into a
    // Suspense boundary just to avoid a static-prerender build error; the
    // microtask wrapper matches the ?admin= read in app/trip/poll/[id]/
    // page.tsx, for the same synchronize-with-an-external-system reason.
    queueMicrotask(() => {
      if (new URLSearchParams(window.location.search).get("error")) {
        setError(LOGIN_FAILED_MESSAGE);
      }
    });
  }, []);

  async function handleGoogleSignIn() {
    setError("");
    setIsRedirectingToGoogle(true);
    try {
      await signIn("google", {
        callbackUrl: resolveCallbackUrl(fallbackCallbackUrl),
      });
    } catch {
      setError("Couldn't reach Google sign-in. Please try again.");
      setIsRedirectingToGoogle(false);
    }
  }

  async function handleEmailSubmit(event: FormEvent) {
    event.preventDefault();
    setError("");

    const trimmed = email.trim();
    if (!trimmed) {
      setError("Enter your email to continue.");
      return;
    }

    setIsSendingLink(true);
    try {
      const result = await signIn("email", {
        email: trimmed,
        redirect: false,
        callbackUrl: resolveCallbackUrl(fallbackCallbackUrl),
      });

      if (result?.error) {
        setError("Couldn't send the sign-in link. Please try again.");
      } else {
        setLinkSentTo(trimmed);
      }
    } catch {
      setError("Couldn't send the sign-in link. Please try again.");
    } finally {
      setIsSendingLink(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-50 px-4 py-12">
      <div className="w-full max-w-sm rounded-3xl border border-slate-200 bg-white p-8 shadow-xl shadow-slate-900/5 sm:p-10">
        <div className="flex flex-col items-center text-center">
          <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-rose-50 text-rose-500 ring-1 ring-rose-100">
            <Plane className="h-6 w-6" aria-hidden="true" />
          </div>

          <h1 className="mt-5 text-2xl font-bold tracking-tight text-slate-900">
            Join the Trip
          </h1>
          <p className="mt-2 text-sm leading-relaxed text-slate-500">
            Sign in to vote on dates, pick your vibes, and see the plan come
            together.
          </p>
        </div>

        {error && (
          <div
            role="alert"
            className="mt-6 flex items-start gap-2.5 rounded-2xl border border-red-200 bg-red-50 px-4 py-3"
          >
            <CircleAlert
              className="mt-0.5 h-4 w-4 shrink-0 text-red-500"
              aria-hidden="true"
            />
            <p className="text-sm font-medium text-red-700">{error}</p>
          </div>
        )}

        <div className="mt-6 space-y-3">
          <button
            type="button"
            onClick={handleGoogleSignIn}
            disabled={isRedirectingToGoogle}
            className="flex w-full items-center justify-center gap-3 rounded-full border border-slate-200 bg-white px-4 py-3.5 text-sm font-semibold text-slate-700 shadow-sm outline-none transition hover:border-slate-300 hover:bg-slate-50 focus-visible:ring-4 focus-visible:ring-slate-200 active:scale-[0.98] disabled:opacity-50"
          >
            <GoogleIcon />
            {isRedirectingToGoogle ? "Redirecting..." : "Continue with Google"}
          </button>

          <div className="flex items-center gap-3 py-1" aria-hidden="true">
            <span className="h-px flex-1 bg-slate-200" />
            <span className="text-xs font-medium uppercase tracking-wider text-slate-400">
              or
            </span>
            <span className="h-px flex-1 bg-slate-200" />
          </div>

          {!showEmailForm ? (
            <button
              type="button"
              onClick={() => {
                setError("");
                setShowEmailForm(true);
              }}
              className={primaryButtonClass}
            >
              <Mail className="h-4 w-4" aria-hidden="true" />
              Continue with Email
            </button>
          ) : linkSentTo ? (
            <div
              role="status"
              className="rounded-2xl border border-emerald-200 bg-emerald-50 p-4 text-center"
            >
              <p className="text-sm font-medium text-emerald-800">
                Check your inbox
              </p>
              <p className="mt-1 text-sm text-emerald-700">
                We sent a magic link to <strong>{linkSentTo}</strong>.
              </p>
              <button
                type="button"
                onClick={() => {
                  setLinkSentTo("");
                  setEmail("");
                }}
                className="mt-3 inline-flex items-center gap-1 text-xs font-semibold text-emerald-700 underline underline-offset-2"
              >
                Use a different email
              </button>
            </div>
          ) : (
            <form onSubmit={handleEmailSubmit} className="space-y-2.5">
              <div className="flex items-center gap-1.5">
                <button
                  type="button"
                  onClick={() => {
                    setError("");
                    setShowEmailForm(false);
                  }}
                  aria-label="Back"
                  className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-slate-400 transition hover:bg-slate-100 hover:text-slate-600"
                >
                  <ArrowLeft className="h-4 w-4" aria-hidden="true" />
                </button>
                <label htmlFor="loginEmail" className="text-xs font-semibold uppercase tracking-wider text-slate-500">
                  Your email
                </label>
              </div>
              <input
                id="loginEmail"
                type="email"
                autoFocus
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                placeholder="you@example.com"
                className={fieldClass}
              />
              <button
                type="submit"
                disabled={isSendingLink}
                className={primaryButtonClass}
              >
                {isSendingLink ? "Sending..." : "Send magic link"}
              </button>
            </form>
          )}
        </div>
      </div>
    </div>
  );
}
