"""
Who is asking, for a brain that answers more than one kind of caller.

The voice assistant (main.py) has one user, at a microphone, and a tool may
speak to them through LINE. The web hub (api_server.py) serves a signed-in
person at a text chat: a tool should return what that person needs to SEE
(a link) instead of pushing it elsewhere, and should act for THAT person
(their Taste Profile, their trip). Nothing in a tool's JSON arguments can say
which of the two it is in, and the model must not be trusted to - so the
caller states it here, once per request, and the tools read it.

A ContextVar, not a global: api_server.py runs each request as its own asyncio
task, and a tool runs on a worker thread started from that task with a copy of
its context (asyncio.to_thread; foreground tools - the only ones that read this -
are never handed to the task dispatcher), so each request sees exactly its own
context and an unrelated one can never leak into it. Dependency-free on purpose,
like tool_spec.py, so the brain, the plugins and the server can all import it
without a cycle.

RUNS (cancellation and progress). The same module also carries what one run of the
brain's state machine (llm_brain.py) shares with the code it calls on worker threads:
whether the run was stopped, and where a step reports what it is doing. A web request
whose person pressed Stop sets the run's cancel event; code that can stop between two
steps (a specialist agent's loop, the Canva browser, a tool call that has not started)
calls check_cancelled() and gets Cancelled. The voice assistant's runs are never
cancelled and report progress nowhere, so for it both are no-ops.

EVENTS AND APPROVALS. A run can also push a structured event to whoever follows it
(emit_event): the web hub uses it to ask the person to approve a confirmation-gated
tool (approvals.py). confirming() names the tool being confirmed for the length of
the question, so the approval can say WHAT it is asking about without the confirm
callback (which only ever receives a sentence) having to be told.
"""

from __future__ import annotations

import contextvars
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional

CHANNEL_WEB = "web"


@dataclass(frozen=True)
class RequestContext:
    """One request's caller. Absent (None) means the voice assistant."""

    channel: str
    # TripPlanner_Web User.id of the signed-in person, "" when not known.
    user_id: str = ""
    # Origin of the TripPlanner_Web instance that sent the request, e.g.
    # "http://localhost:3000"; "" when not known.
    app_url: str = ""
    # The person has voice replies on: a reply of at most rich_reply.VOICE_MAX_CHARS
    # characters is read aloud to them (reply_audio.py), so the brain is asked to keep
    # conversational answers that short.
    voice_reply: bool = False
    # Widgets the tools asked the page to draw during THIS request (see rich_reply.py).
    # A mutable list on a frozen dataclass: the context is replaced per request, the list is
    # filled by tools and read once by the brain when it has the final reply.
    widgets: list[dict[str, Any]] = field(default_factory=list, compare=False, repr=False)

    @property
    def is_web(self) -> bool:
        return self.channel == CHANNEL_WEB


_CURRENT: contextvars.ContextVar[Optional[RequestContext]] = contextvars.ContextVar(
    "jarvis_request_context", default=None
)


def current() -> Optional[RequestContext]:
    """The context of the request being served, or None outside one."""
    return _CURRENT.get()


def add_widget(widget: dict[str, Any]) -> bool:
    """
    Asks the web page to draw `widget` with this reply. True when the request is a
    web chat (which can draw it), False for the voice assistant or outside a request -
    the caller then puts the information in its spoken result instead.
    """
    context = current()
    if context is None or not context.is_web:
        return False
    context.widgets.append(widget)
    return True


def take_widgets() -> list[dict[str, Any]]:
    """The widgets added so far in this request, handed over once (the list is emptied)."""
    context = current()
    if context is None or not context.widgets:
        return []
    taken = list(context.widgets)
    context.widgets.clear()
    return taken


@contextmanager
def use(context: RequestContext) -> Iterator[RequestContext]:
    """Makes `context` current for the duration of the with-block."""
    token = _CURRENT.set(context)
    try:
        yield context
    finally:
        _CURRENT.reset(token)


# ---------------------------------------------------------------------------
# Runs: cancellation and progress (see RUNS at the top)
# ---------------------------------------------------------------------------


class Cancelled(BaseException):
    """
    The run this code works for was stopped (the person pressed Stop, or the web
    request went away). A BaseException, like asyncio.CancelledError, so the
    `except Exception` that turns a tool's failure into a result the model reads
    does not catch it: a stopped run must end, not be told about it.
    """


@dataclass(frozen=True)
class RunScope:
    """One run's cancel event and progress reporter. Absent (None) outside a run."""

    cancel: threading.Event
    # Called with a short sentence ("Head of Research: searching the web..."); thread-safe,
    # never raises. None when nobody is listening (the voice assistant).
    progress: Optional[Callable[[str], None]] = None
    # Called with one structured event (a dict, e.g. an approval request); thread-safe.
    # None when the caller cannot answer one (the voice assistant, the web /voice door).
    emit: Optional[Callable[[dict[str, Any]], None]] = None


_RUN: contextvars.ContextVar[Optional[RunScope]] = contextvars.ContextVar("jarvis_run_scope", default=None)


@contextmanager
def run_scope(
    cancel: threading.Event,
    progress: Optional[Callable[[str], None]] = None,
    emit: Optional[Callable[[dict[str, Any]], None]] = None,
) -> Iterator[RunScope]:
    """Makes a run's cancel event, progress reporter and event channel current for the with-block."""
    scope = RunScope(cancel=cancel, progress=progress, emit=emit)
    token = _RUN.set(scope)
    try:
        yield scope
    finally:
        _RUN.reset(token)


def cancel_event() -> Optional[threading.Event]:
    """The current run's cancel event, or None outside a run."""
    scope = _RUN.get()
    return scope.cancel if scope is not None else None


def cancelled() -> bool:
    """True when the current run was stopped."""
    scope = _RUN.get()
    return scope is not None and scope.cancel.is_set()


def check_cancelled() -> None:
    """Raises Cancelled when the current run was stopped; call it between two steps of work."""
    if cancelled():
        raise Cancelled()


def report_progress(text: str) -> None:
    """Tells whoever follows the current run what is happening now. A no-op when nobody does."""
    scope = _RUN.get()
    if scope is None or scope.progress is None or not text:
        return
    try:
        scope.progress(text)
    except Exception:  # noqa: BLE001 - a status line must never cost the work its result
        pass


def can_emit() -> bool:
    """True when the current run has someone who can be sent a structured event (and answer it)."""
    scope = _RUN.get()
    return scope is not None and scope.emit is not None


def emit_event(event: dict[str, Any]) -> bool:
    """
    Sends one structured event to whoever follows the current run. True when it was handed
    over; False when nobody follows the run in a way that can take it, or the hand-over failed.
    """
    scope = _RUN.get()
    if scope is None or scope.emit is None:
        return False
    try:
        scope.emit(event)
    except Exception:  # noqa: BLE001 - a status event must never cost the work its result
        return False
    return True


# ---------------------------------------------------------------------------
# What a confirmation is about
# ---------------------------------------------------------------------------

_SUBJECT: contextvars.ContextVar[Optional[tuple[str, dict[str, Any]]]] = contextvars.ContextVar(
    "jarvis_confirmation_subject", default=None
)


@contextmanager
def confirming(tool: str, args: Optional[dict[str, Any]]) -> Iterator[None]:
    """For the with-block, the confirmation being asked is about `tool` called with `args`."""
    token = _SUBJECT.set((tool, dict(args or {})))
    try:
        yield
    finally:
        _SUBJECT.reset(token)


def confirmation_subject() -> Optional[tuple[str, dict[str, Any]]]:
    """(tool name, arguments) the current confirmation question is about, or None outside one."""
    return _SUBJECT.get()
