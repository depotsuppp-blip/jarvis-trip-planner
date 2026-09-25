"use client";

import { useState, type FormEvent } from "react";
import { signIn } from "next-auth/react";
import { Mail, ArrowLeft } from "lucide-react";

const fieldClass =
  "mt-1.5 w-full rounded-2xl border border-slate-200 bg-slate-50 px-4 py-3.5 text-base text-slate-900 placeholder:text-slate-400 shadow-inner shadow-slate-900/5 outline-none transition focus:border-blue-400 focus:bg-white focus:ring-4 focus:ring-blue-100";

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
 * Full-screen gate shown in place of the poll/dashboard content whenever a
 * visitor isn't signed in and didn't arrive with the organizer's ?admin=
 * token - see the gate check in app/trip/poll/[id]/page.tsx and
 * app/trip/dashboard/[id]/page.tsx. Deliberately calls signIn() with an
 * explicit provider id ("google"/"email") rather than the bare signIn(),
 * so NextAuth's own unstyled chooser/verify-request pages never appear -
 * this component is the entire sign-in UI.
 *
 * tripId is optional purely for the "Trip code" chip below, which exists
 * to reassure someone landing here that they followed the right link, not
 * as any kind of access control.
 */
export function LoginScreen({ tripId }: { tripId?: string }) {
  const [showEmailForm, setShowEmailForm] = useState(false);
  const [email, setEmail] = useState("");
  const [isSendingLink, setIsSendingLink] = useState(false);
  const [isRedirectingToGoogle, setIsRedirectingToGoogle] = useState(false);
  const [linkSentTo, setLinkSentTo] = useState("");
  const [error, setError] = useState("");

  function callbackUrl() {
    return typeof window !== "undefined" ? window.location.href : "/";
  }

  async function handleGoogleSignIn() {
    setError("");
    setIsRedirectingToGoogle(true);
    try {
      await signIn("google", { callbackUrl: callbackUrl() });
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
        callbackUrl: callbackUrl(),
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
      <div className="w-full max-w-sm rounded-3xl border border-slate-200 bg-white p-8 shadow-xl shadow-slate-900/5">
        <div className="flex flex-col items-center text-center">
          <div className="flex h-12 w-12 items-center justify-center rounded-2xl border border-slate-200 bg-slate-50 text-2xl">
            &#9992;
          </div>

          {tripId && (
            <span className="mt-4 rounded-full border border-slate-200 bg-slate-50 px-3 py-1 font-mono text-xs font-semibold uppercase tracking-widest text-slate-500">
              Trip {tripId.toUpperCase()}
            </span>
          )}

          <h1 className="mt-4 text-xl font-bold text-slate-900">
            Sign in to continue
          </h1>
          <p className="mt-1.5 text-sm text-slate-500">
            You&apos;ve been invited to a trip - sign in to vote and see the
            plan.
          </p>
        </div>

        <div className="mt-7 space-y-3">
          <button
            type="button"
            onClick={handleGoogleSignIn}
            disabled={isRedirectingToGoogle}
            className="flex w-full items-center justify-center gap-3 rounded-full border border-slate-200 bg-white px-4 py-3.5 text-sm font-semibold text-slate-700 shadow-sm transition hover:bg-slate-50 active:scale-[0.98] disabled:opacity-50"
          >
            <GoogleIcon />
            {isRedirectingToGoogle ? "Redirecting..." : "Continue with Google"}
          </button>

          {!showEmailForm ? (
            <button
              type="button"
              onClick={() => {
                setError("");
                setShowEmailForm(true);
              }}
              className="flex w-full items-center justify-center gap-2.5 rounded-full bg-rose-500 px-4 py-3.5 text-sm font-semibold text-white shadow-md shadow-rose-500/20 transition hover:bg-rose-600 active:scale-[0.98]"
            >
              <Mail className="h-4 w-4" aria-hidden="true" />
              Continue with Email
            </button>
          ) : linkSentTo ? (
            <div className="rounded-2xl border border-emerald-200 bg-emerald-50 p-4 text-center">
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
                className="w-full rounded-full bg-rose-500 px-4 py-3.5 text-sm font-semibold text-white shadow-md shadow-rose-500/20 transition hover:bg-rose-600 active:scale-[0.98] disabled:opacity-50"
              >
                {isSendingLink ? "Sending..." : "Send magic link"}
              </button>
            </form>
          )}
        </div>

        {error && (
          <p className="mt-4 text-center text-sm text-red-600">{error}</p>
        )}
      </div>
    </div>
  );
}
