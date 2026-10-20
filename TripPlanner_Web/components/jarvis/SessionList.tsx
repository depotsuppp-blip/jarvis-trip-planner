"use client";

import { memo, useMemo } from "react";
import { useChat, useChatActions } from "@/lib/jarvis/JarvisProvider";
import { formatSessionTime, groupSessions, sessionTitle } from "@/lib/jarvis/sessions";
import { useMinuteClock } from "@/lib/jarvis/useClock";
import { focusRingInset } from "./styles";

/**
 * The sidebar's history: the person's saved sessions grouped by day, newest first,
 * the open one lifted onto a white sheet. A session that has no server record yet
 * (a "New session" nobody has written in) shows as the first row, so the page
 * never says "new" in the bar and shows nothing in the list.
 *
 * `onPick` runs after a choice: the phone drawer closes itself with it.
 */
export const SessionList = memo(function SessionList({ onPick }: { onPick?: () => void }) {
  const access = useChat((state) => state.access);
  const sessions = useChat((state) => state.sessions);
  const activeId = useChat((state) => state.activeSessionId);
  const actions = useChatActions();
  const now = useMinuteClock();
  const groups = useMemo(() => groupSessions(sessions.items, now), [sessions.items, now]);

  const showDraft = activeId === null && access !== "signed-out";

  return (
    <nav aria-label="Sessions">
      <h2 className="px-2 text-[13px] font-semibold text-slate-800">Sessions</h2>

      {access === "signed-out" ? (
        <p className="mt-2 px-2 text-sm leading-5 text-slate-600">Sign in to keep your sessions.</p>
      ) : sessions.status === "loading" && sessions.items.length === 0 ? (
        <div role="status" aria-busy="true" className="mt-2 space-y-1 motion-safe:animate-pulse">
          <span className="sr-only">Loading sessions…</span>
          {[72, 56, 64].map((width) => (
            <div key={width} className="flex h-10 items-center px-2">
              <div className="h-3.5 rounded-sm bg-slate-200" style={{ width: `${width}%` }} />
            </div>
          ))}
        </div>
      ) : (
        <>
          {sessions.status === "error" && (
            <p role="alert" className="mt-2 px-2 text-sm leading-5 text-red-700">
              Couldn&apos;t load your sessions.{" "}
              <button
                type="button"
                onClick={() => void actions.refreshSessions()}
                className={`rounded-sm font-semibold underline underline-offset-2 ${focusRingInset}`}
              >
                Retry
              </button>
            </p>
          )}

          {showDraft && (
            <ul className="mt-2">
              <li>
                <div
                  aria-current="true"
                  className="flex min-h-10 items-center gap-2 rounded-md bg-white px-2 text-sm font-semibold text-slate-900 ring-1 ring-slate-200"
                >
                  <span aria-hidden="true" className="size-1.5 shrink-0 rounded-full bg-rose-600" />
                  <span className="min-w-0 flex-1 truncate">New session</span>
                </div>
              </li>
            </ul>
          )}

          {groups.map((group) => (
            <div key={group.label}>
              <h3 className="mt-3 px-2 pb-1 text-xs font-medium text-slate-600">{group.label}</h3>
              <ul>
                {group.items.map((session) => {
                  const active = session.id === activeId;
                  return (
                    <li key={session.id}>
                      <button
                        type="button"
                        onClick={() => {
                          void actions.selectSession(session.id);
                          onPick?.();
                        }}
                        aria-current={active ? "true" : undefined}
                        className={`flex min-h-10 w-full items-center gap-2 rounded-md px-2 text-left text-sm transition-colors duration-150 ${
                          active
                            ? "bg-white font-semibold text-slate-900 ring-1 ring-slate-200"
                            : "font-medium text-slate-700 hover:bg-white/70"
                        } ${focusRingInset}`}
                      >
                        {active && <span aria-hidden="true" className="size-1.5 shrink-0 rounded-full bg-rose-600" />}
                        <span className="min-w-0 flex-1 truncate">{sessionTitle(session)}</span>
                        <time
                          className="shrink-0 text-xs font-normal tabular-nums text-slate-600"
                          dateTime={new Date(session.updatedAt).toISOString()}
                        >
                          {formatSessionTime(session.updatedAt, now)}
                        </time>
                      </button>
                    </li>
                  );
                })}
              </ul>
            </div>
          ))}

          {sessions.status === "ready" && sessions.items.length === 0 && !showDraft && (
            <p className="mt-2 px-2 text-sm leading-5 text-slate-600">No sessions yet.</p>
          )}
        </>
      )}
    </nav>
  );
});
