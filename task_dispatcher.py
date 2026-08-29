"""
Module 10: Asynchronous task dispatcher

Keeps the microphone loop free. A webhook that takes eight seconds, a
database query that takes four, a vision call that takes six - none of
them may hold the listening thread, because while they do, Jarvis is
deaf.

    tool call -> dispatcher.submit(...)  -> returns instantly
                                         -> "I'm on it. Anything else?"
    ...user keeps talking...
    task finishes -> result parked in a queue
                  -> spoken at the next safe moment

WHAT ACTUALLY COMES THROUGH HERE
--------------------------------
Nothing registers itself with this module by name. A tool arrives here
by being declared background=True in llm_brain.TOOLS, which is what
makes llm_brain._execute call submit() instead of the handler. Today
that is two tools:

    trigger_n8n_webhook   an n8n round trip, seconds of it
    stop_subtrack_env     waits out `docker stop`'s ten-second SIGTERM
                          grace period, then two process trees

Both are slow, and neither one's result is part of the sentence Jarvis
is about to speak - which together are the test for belonging here. A
tool whose result IS the answer ("what is on my screen", and the tool
that STARTS the environment, whose summary the user is waiting for)
stays in the foreground however slow it is.

send_line_notification looks like a third candidate - it is a network
call - but it fails that test: "sent" or the specific reason it was not
is exactly the confirmation the user is waiting to hear, in one HTTP
round trip. It stays background=False in llm_brain.TOOLS and never
reaches this module.

generate_trip_plan_async (plugins/trip_planner.py) looks like the
strongest candidate of all - it is minutes of work, not seconds - and
still never reaches this module. It backgrounds ITSELF: the handler
spawns its own threading.Thread and returns a destination-specific
acknowledgement synchronously, before task_dispatcher would even have
finished dispatching it. Submitting it here as well would background an
already-backgrounded call, and this module's contract is to REPLACE
whatever the handler returns with a generic "STARTED IN THE BACKGROUND"
sentence once dispatched (see llm_brain._execute) - which would throw
away the tailored acknowledgement the tool exists to produce. It stays
background=False in llm_brain.TOOLS for that reason, not because the
underlying work is fast.

WHY A THREAD POOL BEHIND AN EVENT LOOP
--------------------------------------
This module owns one asyncio loop on one daemon thread. Work submitted
to it is either a coroutine (awaited directly) or an ordinary blocking
function (run in the loop's executor). Almost every tool in this project
is the second kind - requests.post, psycopg2, an SDK call - and wrapping
blocking IO in `run_in_executor` is what makes those cooperate with an
event loop at all. Writing `async def` around a blocking call would just
move the block onto the loop thread.

The audio capture loop deliberately stays where it is: on the main
thread, blocking on PyAudio reads. That is the one thing that must not
be moved. PortAudio callbacks and asyncio do not mix well on Windows,
and a 0.25 s blocking read is not a latency problem - it is the clock
the whole session runs on. "Never blocked" here means never blocked *by
work that is not audio*.

WHY RESULTS ARE QUEUED INSTEAD OF SPOKEN IMMEDIATELY
----------------------------------------------------
A finished task cannot just grab the speakers. Two reasons, both
learned the hard way in this codebase:

    * Jarvis would talk over the user mid-command.
    * In STANDBY, Vosk hears whatever Jarvis says. An announcement
      containing the word "jarvis" would wake the assistant up on its
      own voice, forever.

So completions are parked in a queue and drained by session_manager at
turn boundaries, where the microphone is known to be closed.

DEADLOCK POLICY (the explicit constraint)
-----------------------------------------
Three things run concurrently: the STT loop (main thread), the vision
daemon (its own thread), and this dispatcher. They share no locks. The
only shared state is this queue, guarded by a mutex held for the length
of a list append.

Everything crossing the boundary is timed out rather than waited on
forever: submit() never blocks, shutdown() joins with a timeout and
gives up rather than hanging exit, and every task body is wrapped so an
exception becomes a spoken sentence instead of a dead worker.
"""

from __future__ import annotations

import asyncio
import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# Hard cap on one background task. A webhook that never answers must not
# leave a worker (and a "still running" status) alive for the rest of the
# session.
DEFAULT_TASK_TIMEOUT = 120.0

# Concurrent blocking tasks. Small on purpose: these are IO calls, and a
# voice assistant that has more than a handful in flight has a bug.
MAX_WORKERS = 4


def humanise_exception(exc: BaseException) -> str:
    """
    Turns an exception into a lowercase clause a person can listen to.

    The result is spoken as "The <task> failed, <clause>.", so each
    branch returns a fragment that completes that sentence rather than a
    sentence of its own.

    Background results are SPOKEN, so the raw
    "ConnectionError: HTTPConnectionPool(host='localhost', port=5678)"
    is read out verbatim, port number and all. What the listener needs
    is which part of the system was unreachable and what to go and look
    at; the full detail is already on the console and in the log.
    """
    name = type(exc).__name__
    detail = str(exc)
    lowered = f"{name} {detail}".lower()

    # Matched on the exception NAME, not the message: requests, httpx and
    # urllib3 all word connection failures differently, but they agree on
    # what they call the class.
    if "connection" in lowered or "refused" in lowered or "unreachable" in lowered:
        return (
            "I couldn't reach the automation server - "
            "please check that n8n is running"
        )
    if "timeout" in lowered or "timedout" in lowered:
        return "it took too long to respond"
    if name in ("PermissionError", "Forbidden", "Unauthorized"):
        return "the request was refused - the credentials look wrong"
    if name == "FileNotFoundError":
        return "something it needed was not on this machine"

    # Unrecognised: say plainly that it failed and keep the class name,
    # which is short enough to speak and is the one word worth having
    # when the user reads the console afterwards.
    print(f"[Dispatcher] Unhandled task error: {name}: {detail}")
    return f"it reported a {name}"


@dataclass
class TaskRecord:
    """One dispatched task, from submission to announcement."""

    id: int
    name: str
    label: str
    started: float
    finished: Optional[float] = None
    result: str = ""
    error: str = ""
    announced: bool = False

    @property
    def running(self) -> bool:
        return self.finished is None

    @property
    def elapsed(self) -> float:
        return (self.finished or time.monotonic()) - self.started

    def announcement(self) -> str:
        """The sentence spoken when this task's result is drained."""
        if self.error:
            return f"The {self.label} failed, {self.error}."
        if not self.result:
            return f"The {self.label} finished."
        return f"About the {self.label}: {self.result}"


class TaskDispatcher:
    """
    Runs work off the listening thread and collects the results.

    Never raises out of submit() or drain(): a dispatcher that throws
    would take down the session loop it exists to protect.
    """

    def __init__(self, task_timeout: float = DEFAULT_TASK_TIMEOUT) -> None:
        self._task_timeout = task_timeout
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._tasks: dict[int, TaskRecord] = {}
        self._pending: list[TaskRecord] = []      # finished, not yet spoken
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._started = threading.Event()

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        """Spins up the event loop thread. Idempotent."""
        if self._thread is not None:
            return

        def run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            # Bound the executor: unbounded threads plus a hung webhook
            # is how a "background task" becomes a memory leak.
            loop.set_default_executor(
                __import__("concurrent.futures").futures.ThreadPoolExecutor(
                    max_workers=MAX_WORKERS, thread_name_prefix="jarvis-task"
                )
            )
            self._loop = loop
            self._started.set()
            try:
                loop.run_forever()
            finally:
                try:
                    loop.close()
                except Exception:  # noqa: BLE001 - teardown must stay quiet
                    pass

        self._thread = threading.Thread(target=run, name="jarvis-dispatcher", daemon=True)
        self._thread.start()
        # Bounded wait: if the loop cannot start, submit() degrades to
        # running work inline rather than hanging here.
        if not self._started.wait(5.0):
            print("[Tasks] Dispatcher loop failed to start; work will run inline.")

    def shutdown(self, timeout: float = 3.0) -> None:
        """Stops the loop. Gives up rather than hanging the exit path."""
        loop, self._loop = self._loop, None
        if loop is None:
            return

        def cancel_all() -> None:
            # Cancel before stopping. Tasks still pending when a loop is
            # closed print "Task was destroyed but it is pending!" to
            # stderr - harmless, but it is the last thing a user sees on
            # exit, and it reads like a crash.
            for task in asyncio.all_tasks(loop):
                task.cancel()

        try:
            loop.call_soon_threadsafe(cancel_all)
            # A beat for the cancellations to be delivered, then stop.
            # Bounded: a task that refuses to die is abandoned, not
            # waited on - shutdown must never hang the exit path.
            time.sleep(0.15)
            loop.call_soon_threadsafe(loop.stop)
        except Exception:  # noqa: BLE001 - already stopping
            pass
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                print("[Tasks] Dispatcher thread did not stop in time; abandoning it.")
        self._thread = None

    # -- submission ----------------------------------------------------

    def submit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        name: str = "task",
        label: Optional[str] = None,
        **kwargs: Any,
    ) -> TaskRecord:
        """
        Schedules `fn` off this thread and returns immediately.

        `fn` may be a coroutine function (awaited) or an ordinary
        blocking one (run in the executor). `label` is the human phrase
        used when the result is announced - "subtrack log workflow"
        rather than "trigger_n8n_webhook".
        """
        record = TaskRecord(
            id=next(self._ids),
            name=name,
            label=label or name.replace("_", " "),
            started=time.monotonic(),
        )
        with self._lock:
            self._tasks[record.id] = record

        loop = self._loop
        if loop is None or loop.is_closed():
            # No loop (start() failed, or shutdown already ran). Running
            # inline is slower but correct; silently dropping the work
            # would be a lie to the user.
            print(f"[Tasks] No dispatcher loop; running {record.name} inline.")
            self._finish(record, *self._call_sync(fn, args, kwargs))
            return record

        print(f"[Tasks] #{record.id} {record.name} dispatched to the background.")
        try:
            asyncio.run_coroutine_threadsafe(self._run(record, fn, args, kwargs), loop)
        except Exception as exc:  # noqa: BLE001 - loop died between checks
            print(f"[Tasks] Could not dispatch {record.name}: {exc}")
            self._finish(record, *self._call_sync(fn, args, kwargs))
        return record

    async def _run(self, record: TaskRecord, fn, args, kwargs) -> None:
        """The task body, on the dispatcher loop. Cannot raise upward."""
        loop = asyncio.get_running_loop()
        try:
            if asyncio.iscoroutinefunction(fn):
                coro = fn(*args, **kwargs)
            else:
                coro = loop.run_in_executor(None, lambda: fn(*args, **kwargs))
            result = await asyncio.wait_for(coro, timeout=self._task_timeout)
            self._finish(record, str(result) if result is not None else "", "")
        except asyncio.TimeoutError:
            self._finish(
                record,
                "",
                f"it was still running after {self._task_timeout:.0f} seconds, so I stopped waiting",
            )
        except Exception as exc:  # noqa: BLE001 - every failure is reportable
            self._finish(record, "", humanise_exception(exc))

    @staticmethod
    def _call_sync(fn, args, kwargs) -> tuple[str, str]:
        """Inline fallback. Returns (result, error)."""
        try:
            value = fn(*args, **kwargs)
            return (str(value) if value is not None else ""), ""
        except Exception as exc:  # noqa: BLE001
            return "", humanise_exception(exc)

    def _finish(self, record: TaskRecord, result: str, error: str) -> None:
        record.finished = time.monotonic()
        record.result = result
        record.error = error
        with self._lock:
            self._pending.append(record)
        state = "failed" if error else "done"
        print(f"[Tasks] #{record.id} {record.name} {state} in {record.elapsed:.1f}s.")

    # -- collection ----------------------------------------------------

    @property
    def running_count(self) -> int:
        with self._lock:
            return sum(1 for t in self._tasks.values() if t.running)

    def running_labels(self) -> list[str]:
        with self._lock:
            return [t.label for t in self._tasks.values() if t.running]

    def has_announcements(self) -> bool:
        with self._lock:
            return bool(self._pending)

    def drain_announcements(self) -> list[str]:
        """
        Returns the sentences for every task that finished since the last
        drain, and clears the queue.

        Called by session_manager at turn boundaries - the points where
        the microphone is known to be closed, so speaking is safe.
        """
        with self._lock:
            finished, self._pending = self._pending, []
        for record in finished:
            record.announced = True
        return [record.announcement() for record in finished]

    def status_line(self) -> str:
        """One line for the console/status tool."""
        with self._lock:
            running = [t for t in self._tasks.values() if t.running]
            waiting = len(self._pending)
        if not running and not waiting:
            return "No background tasks are running."
        parts = []
        if running:
            parts.append(
                f"{len(running)} running ({', '.join(t.label for t in running)})"
            )
        if waiting:
            parts.append(f"{waiting} finished, not yet reported")
        return "Background tasks: " + "; ".join(parts) + "."


# The process-wide dispatcher. One loop thread per process, shared by
# the brain's tools and anything else that needs to get off the mic
# thread. Created lazily so importing this module costs nothing.
_DISPATCHER: Optional[TaskDispatcher] = None
_DISPATCHER_LOCK = threading.Lock()


def dispatcher() -> TaskDispatcher:
    """The shared dispatcher, started on first use."""
    global _DISPATCHER
    with _DISPATCHER_LOCK:
        if _DISPATCHER is None:
            _DISPATCHER = TaskDispatcher()
            _DISPATCHER.start()
        return _DISPATCHER


def shutdown() -> None:
    """Stops the shared dispatcher, if one was ever created."""
    global _DISPATCHER
    with _DISPATCHER_LOCK:
        if _DISPATCHER is not None:
            _DISPATCHER.shutdown()
            _DISPATCHER = None


if __name__ == "__main__":
    import random

    d = dispatcher()

    def slow(seconds: float, label: str) -> str:
        time.sleep(seconds)
        if label == "boom":
            raise RuntimeError("this one was always going to fail")
        return f"{label} finished after {seconds:.1f}s"

    async def slow_async(seconds: float) -> str:
        await asyncio.sleep(seconds)
        return f"the async one finished after {seconds:.1f}s"

    print("Submitting four tasks; the main thread must stay responsive.\n")
    d.submit(slow, 1.0, "webhook", name="trigger_n8n_webhook", label="subtrack log workflow")
    d.submit(slow, 2.0, "shutdown", name="stop_subtrack_env", label="Subtrack shutdown")
    d.submit(slow, 0.5, "boom", name="broken_tool", label="broken tool")
    d.submit(slow_async, 1.5, name="async_tool", label="async tool")

    for tick in range(8):
        print(f"  t={tick * 0.5:.1f}s  {d.status_line()}")
        for line in d.drain_announcements():
            print(f"    -> SPEAK: {line}")
        time.sleep(0.5)

    print("\nFinal:", d.status_line())
    shutdown()
