"""
Approval of confirmation-gated tools from the web hub.

A tool with requires_confirmation=True (the n8n webhook, starting and stopping the
Subtrack servers, the manager's send_line_notification, create_new_tool) has to be
authorised by a human before it runs. By voice that is the spoken yes/no gate
(voice_engine.confirm). In the web hub there is no microphone, so the question is
put to the person on the page as an inline Approval Card, and the tool waits for
their answer:

    tool call (worker thread)           api_server.py            the hub (browser)
    -------------------------           -------------            -----------------
    LLMBrain._execute
      -> confirm(question) = confirm()
           registers a pending approval
           emits  action_required  ----> SSE event ------------> Approval Card
           waits (checks Stop every 0.25 s)                       [Allow] [Deny]
                                         POST /approvals/<id> <-- the person's choice
           resolve() wakes the wait
      <- True (Allow) / False (Deny, timeout, Stop)
    the tool runs, or is reported as declined, exactly as by voice

The run's state machine does not change: the Plan -> Delegate_to_Agent/Use_Tools node
is simply inside _execute for as long as the person takes to answer, with the web
app's turn timeout (120 s, lib/jarvisBrain.ts) as the outer limit. That is why the
wait is capped (JARVIS_APPROVAL_TIMEOUT, default 60 s, at most 100 s): an unanswered
card ends as a denial, never as a stuck turn.

WHO MAY APPROVE. These tools have effects on the machine Jarvis runs on and outside
it (a LINE push, dev servers, code written into plugins/), and the web hub serves
everyone who signs in. So only the OWNER can be asked - the web account named in
JARVIS_OWNER_USER_ID, the same rule as Google Classroom and the memory store
(mcp_client.caller_scope). For anyone else the tool is declined without a card, as it
always was. And the answer must come from the person the card was sent to: resolve()
checks the user id, so a different signed-in user cannot answer someone else's card.

FAIL CLOSED. Everything that is not a clear Allow is a denial: no one to ask (the voice
assistant, the web /voice door, a run nobody follows), not the owner, the wait ran out,
the page went away (the run is cancelled and the wait ends with it).

Dependency-free apart from request_context and mcp_client (which imports nothing heavy).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import mcp_client
import request_context

DEFAULT_TIMEOUT_SECONDS = 60.0
# The web app abandons a turn after 120 s (BRAIN_TIMEOUT_MS in lib/jarvisBrain.ts): the card has to
# be answered, or time out, well inside that.
MAX_TIMEOUT_SECONDS = 100.0
POLL_SECONDS = 0.25

MAX_DETAILS = 12
MAX_VALUE_CHARS = 6000
MAX_PROMPT_CHARS = 600
# An argument whose name says it is a credential is never sent to the page.
_SECRET_NAME = re.compile(r"token|secret|password|passwd|api[_-]?key|authorization|credential", re.IGNORECASE)

# How the page's answer comes back to the wait (resolve's return values).
RESOLVED = "ok"
NOT_FOUND = "not_found"  # unknown, already answered, or timed out
FORBIDDEN = "forbidden"  # not the person the card was sent to


def timeout_seconds() -> float:
    """How long a card waits for an answer: JARVIS_APPROVAL_TIMEOUT (seconds), 5 to MAX_TIMEOUT_SECONDS."""
    raw = os.environ.get("JARVIS_APPROVAL_TIMEOUT", "").strip()
    try:
        value = float(raw) if raw else DEFAULT_TIMEOUT_SECONDS
    except ValueError:
        value = DEFAULT_TIMEOUT_SECONDS
    return max(5.0, min(value, MAX_TIMEOUT_SECONDS))


@dataclass
class _Pending:
    id: str
    user_id: str
    tool: str
    answered: threading.Event = field(default_factory=threading.Event)
    allow: bool = False


_pending: dict[str, _Pending] = {}
_lock = threading.Lock()


def _text(value: Any) -> str:
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(value)
    return text if len(text) <= MAX_VALUE_CHARS else text[: MAX_VALUE_CHARS - 1].rstrip() + "…"


def describe_arguments(args: Optional[dict[str, Any]]) -> list[dict[str, str]]:
    """The tool's arguments as the card shows them: name and value, long values cut, credentials hidden."""
    rows: list[dict[str, str]] = []
    for name, value in (args or {}).items():
        if len(rows) >= MAX_DETAILS:
            break
        name = str(name)[:64]
        rows.append({"name": name, "value": "(hidden)" if _SECRET_NAME.search(name) else _text(value)})
    return rows


def pending_count() -> int:
    """Cards waiting for an answer right now (for tests and the console)."""
    with _lock:
        return len(_pending)


def resolve(approval_id: str, user_id: str, allow: bool) -> str:
    """
    The person's answer to a card. RESOLVED when it reached the waiting tool; NOT_FOUND for a card
    that is unknown, already answered or timed out; FORBIDDEN when `user_id` is not the person the
    card was sent to. Never raises.
    """
    with _lock:
        item = _pending.get(str(approval_id))
        if item is None or item.answered.is_set():
            return NOT_FOUND
        if not user_id or item.user_id != user_id.strip():
            return FORBIDDEN
        item.allow = bool(allow)
        item.answered.set()
    print(f"[Approvals] {item.tool}: {'allowed' if allow else 'denied'} by the person on the page.")
    return RESOLVED


def confirm(question: str) -> bool:
    """
    The web hub's confirm callback (LLMBrain(confirm=...)): asks the person on the page and waits.
    True only for a clear Allow from the owner. Runs on the tool's worker thread; raises
    request_context.Cancelled, like any step of a run, when the person presses Stop meanwhile.
    """
    context = request_context.current()
    subject = request_context.confirmation_subject()
    tool, args = subject if subject else ("", {})
    label = tool or "an action"

    if context is None or not context.is_web or not request_context.can_emit():
        # The voice assistant has its own gate; the web /voice door and a run nobody follows cannot show a card.
        print(f"[Approvals] {label}: declined - there is no page to ask on.")
        return False

    if mcp_client.caller_scope() != "owner":
        print(f"[Approvals] {label}: declined - only the owner can approve it (set JARVIS_OWNER_USER_ID to your user id).")
        request_context.report_progress("That needs the owner's approval, so it was declined")
        return False

    wait = timeout_seconds()
    approval = _Pending(id=uuid.uuid4().hex, user_id=context.user_id.strip(), tool=tool)
    with _lock:
        _pending[approval.id] = approval

    sent = request_context.emit_event(
        {
            "type": "action_required",
            "approval_id": approval.id,
            "tool": tool,
            "prompt": str(question or "").strip()[:MAX_PROMPT_CHARS] or f"Do you authorize running {label}?",
            "details": describe_arguments(args),
            "expires_in": int(wait),
        }
    )
    outcome = "deny"
    try:
        if not sent:
            print(f"[Approvals] {label}: declined - the approval request could not be sent.")
        else:
            print(f"[Approvals] {label}: waiting up to {wait:.0f} s for the person on the page.")
            deadline = time.monotonic() + wait
            outcome = "timeout"
            while time.monotonic() < deadline:
                if approval.answered.wait(POLL_SECONDS):
                    break
                request_context.check_cancelled()  # Stop pressed, or the page went away
    finally:
        with _lock:
            _pending.pop(approval.id, None)
            # An answer that landed between the last poll and here still counts.
            if approval.answered.is_set():
                outcome = "allow" if approval.allow else "deny"

    if sent:
        if outcome == "timeout":
            print(f"[Approvals] {label}: no answer in {wait:.0f} s; declined.")
        request_context.emit_event({"type": "action_resolved", "approval_id": approval.id, "decision": outcome})
    return outcome == "allow"
