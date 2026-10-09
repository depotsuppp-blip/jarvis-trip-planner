"use client";

import { useEffect, useState, type FormEvent } from "react";
import { signIn } from "next-auth/react";
import { ArrowLeft, CircleAlert, Mail } from "lucide-react";

// Jarvis sign-in speaks the Modern Editorial dialect: white ground, 1px slate
// rules, ink text, 6px corners, no shadows. The only colour is the rose focus
// outline, which means "this one".
const focusClass =
  "outline-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-rose-600";

const fieldClass = `mt-1.5 h-12 w-full rounded-md border border-slate-300 bg-white px-3 text-base text-slate-900 placeholder:text-slate-500 focus:border-slate-900 ${focusClass}`;

const primaryButtonClass = `flex h-12 w-full items-center justify-center gap-2.5 rounded-md bg-slate-900 px-4 text-sm font-semibold text-white transition hover:bg-slate-700 active:scale-[0.98] disabled:opacity-50 ${focusClass}`;

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
 * The sign-in for all of Jarvis, not for any one tool: it carries no trip
 * wording, id or other details of what the visitor was opening.
 *
 * fallbackCallbackUrl is where to land after sign-in when the URL itself
 * doesn't say - see resolveCallbackUrl. Omit it to return to the current
 * page.
 */
export function LoginScreen({
  fallbackCallbackUrl,
  googleReady = true,
  emailReady = true,
}: {
  fallbackCallbackUrl?: string;
  /** False when the server has no credentials for that provider (see lib/authConfig.ts). */
  googleReady?: boolean;
  emailReady?: boolean;
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
    <div className="flex min-h-screen items-center justify-center bg-white px-4 py-12 text-slate-900">
      <div className="w-full max-w-sm">
        <p className="text-[11px] font-medium uppercase tracking-[0.08em] text-slate-500">Jarvis</p>
        <h1 className="mt-3 text-[22px] font-semibold leading-tight tracking-tight">Sign in to Jarvis</h1>
        <p className="mt-2 text-[15px] leading-6 text-slate-600">Your intelligent executive assistant.</p>

        <div className="mt-8 border-t border-slate-200 pt-8">
          {error && (
            <div role="alert" className="mb-6 flex items-start gap-2.5 rounded-md border border-slate-900 px-4 py-3">
              <CircleAlert className="mt-0.5 h-4 w-4 shrink-0 text-rose-600" aria-hidden="true" />
              <p className="text-sm font-medium text-slate-900">{error}</p>
            </div>
          )}

          <div className="space-y-3">
            <button
              type="button"
              onClick={handleGoogleSignIn}
              disabled={isRedirectingToGoogle || !googleReady}
              className={`flex h-12 w-full items-center justify-center gap-3 rounded-md border border-slate-300 bg-white px-4 text-sm font-semibold text-slate-900 transition hover:border-slate-900 active:scale-[0.98] disabled:opacity-50 ${focusClass}`}
            >
              <GoogleIcon />
              {isRedirectingToGoogle ? "Redirecting..." : "Continue with Google"}
            </button>
            {!googleReady && (
              <p className="text-xs text-slate-600">Google sign-in isn&apos;t set up on this server.</p>
            )}

            <div className="flex items-center gap-3 py-1" aria-hidden="true">
              <span className="h-px flex-1 bg-slate-200" />
              <span className="text-[11px] font-medium uppercase tracking-[0.08em] text-slate-500">or</span>
              <span className="h-px flex-1 bg-slate-200" />
            </div>

            {!emailReady ? (
              <p className="text-xs text-slate-600">Email sign-in isn&apos;t set up on this server.</p>
            ) : !showEmailForm ? (
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
              <div role="status" className="rounded-md border border-slate-200 p-4">
                <p className="text-sm font-semibold">Check your inbox</p>
                <p className="mt-1 text-sm text-slate-600">
                  We sent a sign-in link to <strong className="text-slate-900">{linkSentTo}</strong>.
                </p>
                <button
                  type="button"
                  onClick={() => {
                    setLinkSentTo("");
                    setEmail("");
                  }}
                  className={`mt-3 rounded-md text-xs font-semibold text-slate-900 underline underline-offset-2 ${focusClass}`}
                >
                  Use a different email
                </button>
              </div>
            ) : (
              <form onSubmit={handleEmailSubmit} className="space-y-3">
                <div className="flex items-center gap-1.5">
                  <button
                    type="button"
                    onClick={() => {
                      setError("");
                      setShowEmailForm(false);
                    }}
                    aria-label="Back"
                    className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-slate-600 transition hover:text-slate-900 ${focusClass}`}
                  >
                    <ArrowLeft className="h-4 w-4" aria-hidden="true" />
                  </button>
                  <label htmlFor="loginEmail" className="text-[11px] font-medium uppercase tracking-[0.08em] text-slate-600">
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
                <button type="submit" disabled={isSendingLink} className={primaryButtonClass}>
                  {isSendingLink ? "Sending..." : "Send sign-in link"}
                </button>
              </form>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
