"use client";

import { memo } from "react";
import Link from "next/link";
import { useSession } from "next-auth/react";
import { Menu } from "lucide-react";
import { useChat, useOrb, useShell, useShellActions } from "@/lib/jarvis/JarvisProvider";
import { selectActiveTitle } from "@/lib/jarvis/selectors";
import { ORB_LABEL } from "./orbModes";
import { focusRing, iconButton } from "./styles";

/**
 * The 56px bar over the conversation: the open session's title, what Jarvis is
 * doing right now (a live region, so a screen reader hears "Thinking" and
 * "Idle"), and - only when signed out - the way in. On a phone it also carries
 * the button that opens the sidebar drawer.
 */
export const TopBar = memo(function TopBar() {
  const title = useChat(selectActiveTitle);
  const mode = useOrb((state) => state.mode);
  const drawerOpen = useShell((state) => state.drawerOpen);
  const shell = useShellActions();
  // useSession re-renders on every window focus; here it only costs this small bar.
  const { status } = useSession();

  return (
    <header className="flex h-14 shrink-0 items-center gap-1 border-b border-slate-200 bg-white pl-2 pr-4 md:pl-4">
      <button
        type="button"
        onClick={(event) => shell.openDrawer(event.currentTarget)}
        aria-label="Open sidebar"
        aria-controls="jarvis-sidebar"
        aria-expanded={drawerOpen}
        className={`${iconButton} md:hidden`}
      >
        <Menu className="size-5" aria-hidden="true" />
      </button>

      <h1 className="min-w-0 flex-1 truncate px-1 text-[15px] font-semibold tracking-tight text-slate-900">{title}</h1>

      <span className="shrink-0 text-xs font-medium uppercase tracking-[0.08em] text-slate-600" aria-live="polite">
        {ORB_LABEL[mode]}
      </span>

      {status === "unauthenticated" && (
        <Link
          href="/login?callbackUrl=%2Fjarvis"
          className={`ml-3 shrink-0 rounded-md px-1 py-2 text-sm font-semibold text-slate-900 underline underline-offset-2 ${focusRing}`}
        >
          Sign in
        </Link>
      )}
    </header>
  );
});
