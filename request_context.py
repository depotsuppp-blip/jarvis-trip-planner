"""
Who is asking, for a brain that answers more than one kind of caller.

The voice assistant (main.py) has one user, at a microphone, and a tool may
speak to them through LINE. The web hub (api_server.py) serves a signed-in
person at a text chat: a tool should return what that person needs to SEE
(a link) instead of pushing it elsewhere, and should act for THAT person
(their Taste Profile, their trip). Nothing in a tool's JSON arguments can say
which of the two it is in, and the model must not be trusted to - so the
caller states it here, once per request, and the tools read it.

A ContextVar, not a global: api_server.py handles each request on its own
worker thread, and a tool runs on the thread that asked the model (foreground
tools - the only ones that read this - are never handed to the task
dispatcher), so each request sees exactly its own context and an unrelated one
can never leak into it. Dependency-free on purpose, like tool_spec.py, so the
brain, the plugins and the server can all import it without a cycle.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

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
