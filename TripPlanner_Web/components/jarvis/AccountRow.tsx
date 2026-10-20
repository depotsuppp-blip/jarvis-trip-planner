"use client";

import { memo } from "react";
import Link from "next/link";
import { useSession } from "next-auth/react";
import { focusRing } from "./styles";

/**
 * Who is signed in, at the foot of the sidebar - or the way to sign in. The
 * compact form (the collapsed rail) is just the initial. It owns its own
 * useSession(), so the session refetch on window focus re-renders this row and
 * nothing else on the page.
 */
export const AccountRow = memo(function AccountRow({ compact = false }: { compact?: boolean }) {
  const { data: session, status } = useSession();

  if (status === "loading") {
    return <div className={`motion-safe:animate-pulse ${compact ? "size-8" : "h-8"} rounded-full bg-slate-200`} aria-hidden="true" />;
  }

  if (!session?.user) {
    return compact ? null : (
      <Link
        href="/login?callbackUrl=%2Fjarvis"
        className={`flex min-h-10 items-center rounded-md px-2 text-sm font-semibold text-slate-900 underline underline-offset-2 ${focusRing}`}
      >
        Sign in
      </Link>
    );
  }

  const name = session.user.name ?? session.user.email ?? "You";
  const initial = name.trim().charAt(0).toUpperCase() || "?";

  return (
    <div className="flex items-center gap-2.5 px-2" title={compact ? name : undefined}>
      <span
        aria-hidden="true"
        className="grid size-8 shrink-0 place-items-center rounded-full bg-slate-200 text-xs font-semibold text-slate-800"
      >
        {initial}
      </span>
      {!compact && (
        <span className="min-w-0">
          <span className="block truncate text-sm font-medium text-slate-900">{name}</span>
          {session.user.name && session.user.email && (
            <span className="block truncate text-xs text-slate-600">{session.user.email}</span>
          )}
        </span>
      )}
      {compact && <span className="sr-only">{name}</span>}
    </div>
  );
});
