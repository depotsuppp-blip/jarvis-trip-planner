"use client";

import { memo, useEffect, useRef } from "react";
import Link from "next/link";
import { RefreshCw } from "lucide-react";
import { useChat, useChatActions, useOrb } from "@/lib/jarvis/JarvisProvider";
import { isFocalMode, type Access } from "@/lib/jarvis/types";
import { useGreeting } from "@/lib/jarvis/useClock";
import { MessageBubble } from "./MessageBubble";
import { focusRing, thinScroll } from "./styles";

/**
 * The conversation: a scrolling log of messages, or - before there are any -
 * a greeting that says what to do, a skeleton while a session loads, or a plain
 * statement and a retry when it failed.
 *
 * It re-renders when the message list changes and when the orb goes in or out of
 * its centre-stage modes (to dim). The rows are memoised, so neither costs the
 * history above a new reply anything.
 */
export const ChatFeed = memo(function ChatFeed() {
  const messages = useChat((state) => state.messages);
  const conversation = useChat((state) => state.conversation);
  const access = useChat((state) => state.access);
  const epoch = useChat((state) => state.epoch);
  const focal = useOrb((state) => isFocalMode(state.mode));

  const scroller = useRef<HTMLDivElement | null>(null);
  const seenEpoch = useRef(epoch);

  // Follow the conversation to its newest message: glide for a new message, jump when a whole other session arrived.
  useEffect(() => {
    const element = scroller.current;
    if (!element) return;
    const jump = seenEpoch.current !== epoch || window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    seenEpoch.current = epoch;
    element.scrollTo({ top: element.scrollHeight, behavior: jump ? "auto" : "smooth" });
  }, [messages, epoch]);

  return (
    <div
      ref={scroller}
      aria-hidden={focal}
      className={`min-h-0 flex-1 overflow-y-auto transition-opacity duration-500 ease-out motion-reduce:transition-none ${thinScroll} ${
        focal ? "pointer-events-none opacity-30" : "opacity-100"
      }`}
    >
      <div className="mx-auto w-full max-w-2xl px-4 py-8">
        {conversation === "loading" && messages.length === 0 && <FeedSkeleton />}
        {conversation === "error" && <FeedError />}
        {conversation === "ready" && messages.length === 0 && <EmptyState access={access} />}

        <ol role="log" aria-label="Conversation" className="flex flex-col gap-8">
          {messages.map((message) => (
            <MessageBubble key={message.id} message={message} />
          ))}
        </ol>
      </div>
    </div>
  );
});

function EmptyState({ access }: { access: Access }) {
  const greeting = useGreeting();

  return (
    <div className="pb-8 pt-8 sm:pt-16">
      <h2 className="text-[28px] font-bold leading-tight tracking-tight text-slate-900">{greeting}.</h2>
      <p className="mt-2 text-base text-slate-700">What are we planning?</p>

      {access === "signed-out" ? (
        <p className="mt-6 text-sm leading-6 text-slate-700">
          You&apos;re not signed in.{" "}
          <Link
            href="/login?callbackUrl=%2Fjarvis"
            className={`rounded-sm font-semibold text-slate-900 underline underline-offset-2 ${focusRing}`}
          >
            Sign in
          </Link>{" "}
          to chat with Jarvis and keep your sessions.
        </p>
      ) : (
        <p className="mt-6 text-sm leading-6 text-slate-600">
          Try{" "}
          <kbd className="rounded-sm border border-slate-300 px-1.5 py-0.5 font-mono text-xs text-slate-800">/status</kbd>{" "}
          to preview a widget reply.
        </p>
      )}
    </div>
  );
}

function FeedSkeleton() {
  return (
    <div role="status" aria-busy="true" className="space-y-8 pt-4 motion-safe:animate-pulse">
      <span className="sr-only">Loading your session…</span>
      <div className="flex justify-end">
        <div className="h-11 w-2/5 rounded-md bg-slate-200" />
      </div>
      <div className="space-y-2.5">
        <div className="h-3 w-12 rounded-sm bg-slate-200" />
        <div className="h-4 w-4/5 rounded-sm bg-slate-200" />
        <div className="h-4 w-3/5 rounded-sm bg-slate-200" />
      </div>
    </div>
  );
}

function FeedError() {
  const actions = useChatActions();

  return (
    <div role="alert" className="flex items-center justify-between gap-4 rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
      <span className="font-medium">Couldn&apos;t load this session.</span>
      <button
        type="button"
        onClick={() => void actions.retryConversation()}
        className={`inline-flex shrink-0 items-center gap-1.5 rounded-md px-2 py-2 font-semibold underline underline-offset-2 ${focusRing}`}
      >
        <RefreshCw className="size-4" aria-hidden="true" />
        Retry
      </button>
    </div>
  );
}
