"""
LINE push notifications: one text message to the owner's LINE, through the
Messaging API.

    push_line_notification("Today: 09:00-10:00 Stand-up ...")
        -> POST https://api.line.me/v2/bot/message/push
        -> "The LINE notification was sent."

Used by the Head of Productivity's push_line_notification tool
(agents/productivity_agent.py), the manager's send_line_notification tool
(llm_brain.py, while the agent team is off) and the Trip Planner's links
(plugins/trip_planner.py, through plugins/line_notifier.py, which re-exports
this module).

CONFIGURATION (.env)
---------------------
    LINE_CHANNEL_ACCESS_TOKEN   long-lived token from the LINE Developers
                                console, Messaging API channel settings
    LINE_TARGET_USER_ID         who receives the message: the owner's LINE user
                                ID (not their display name) - found via a
                                webhook event or the channel's "Your user ID"
                                panel. LINE_USER_ID, the older name, is read
                                when this one is empty.

The recipient comes from .env only: no caller - and no model - can choose who
receives a message, so this can only ever message the owner.

Neither value is ever written into a returned string or a log line: the token
is a bearer credential (anyone holding it can message the linked account) and
the user ID identifies a real person, so both stay out of anything that could
end up on the console, in a transcript, or spoken aloud.
"""

from __future__ import annotations

import os
from typing import Any

# requests is optional at import time, same as every other module here that
# reaches the network: a machine with no notification channel configured
# should still boot Jarvis. Absence is reported when the tool is used.
try:
    import requests
except ImportError:  # pragma: no cover - environment-dependent
    requests = None  # type: ignore[assignment]

try:  # Loading .env here as well keeps the module usable standalone.
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - environment-dependent
    pass


LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"

# (connect timeout, read timeout) in seconds - always passed explicitly,
# because requests has no default timeout at all and a hung LINE API call
# would otherwise block the assistant indefinitely. Five seconds each matches
# the ceiling n8n_trigger.py uses for the same reason: this is a voice loop,
# and silence longer than that reads as a crash.
_CONNECT_TIMEOUT = 5.0
_READ_TIMEOUT = 5.0

# LINE caps a single text message at 5000 characters; anything past that is a
# 400 the API would otherwise reject with no clue which field was the problem.
MAX_MESSAGE_LENGTH = 5000

TOKEN_ENV = "LINE_CHANNEL_ACCESS_TOKEN"
TARGET_ENV = "LINE_TARGET_USER_ID"
LEGACY_TARGET_ENV = "LINE_USER_ID"


def _target() -> str:
    """The recipient: LINE_TARGET_USER_ID, or the older LINE_USER_ID."""
    return (os.getenv(TARGET_ENV) or "").strip() or (os.getenv(LEGACY_TARGET_ENV) or "").strip()


def configured() -> bool:
    """True when both the token and a recipient are set (says nothing about whether they work)."""
    return bool((os.getenv(TOKEN_ENV) or "").strip() and _target())


def _summarise_error_body(response: "requests.Response") -> str:
    """
    Pulls a short, speakable reason out of a LINE error response.

    LINE's error body is {"message": "...", "details": [...]}; the top-level
    message is almost always the useful part ("Invalid user ID", "The
    property, 'messages[0].text', is required"), so it wins over reading the
    raw body aloud.
    """
    try:
        body: Any = response.json()
    except ValueError:
        text = (response.text or "").strip()
        return text[:200] if text else f"HTTP {response.status_code}"

    if isinstance(body, dict):
        message = body.get("message")
        if isinstance(message, str) and message.strip():
            detail = message.strip()
            extra = body.get("details")
            if isinstance(extra, list) and extra:
                first = extra[0]
                if isinstance(first, dict) and isinstance(first.get("message"), str):
                    detail = f"{detail}: {first['message'].strip()}"
            return detail[:200]

    return f"HTTP {response.status_code}"


def push_line_notification(message: str) -> str:
    """
    Pushes `message` to the owner's LINE (LINE_TARGET_USER_ID) as a text message.

    Returns one sentence describing what happened; a failure starts with
    "ERROR:". Never raises: every failure mode - missing config, missing
    package, network error, a LINE-side rejection - becomes a string the caller
    can read aloud or hand back to the model, never an exception that would
    take down the supervisor loop.
    """
    message = (message or "").strip()
    if not message:
        return "ERROR: there was no message text to send, so nothing was pushed to LINE."

    token = (os.getenv(TOKEN_ENV) or "").strip()
    user_id = _target()
    missing = [name for name, value in ((TOKEN_ENV, token), (TARGET_ENV, user_id)) if not value]
    if missing:
        verb = "is" if len(missing) == 1 else "are"
        return (
            f"ERROR: LINE isn't configured - {' and '.join(missing)} {verb} not "
            "set in the .env file. Nothing was sent."
        )

    if requests is None:
        return "ERROR: the requests package is not installed, so no LINE notification was sent."

    if len(message) > MAX_MESSAGE_LENGTH:
        # Truncated rather than rejected outright: a slightly clipped reminder
        # is still useful, and a hard failure here would throw away a message
        # the user is waiting on for a formatting reason they cannot see.
        message = message[: MAX_MESSAGE_LENGTH - 1] + "…"

    payload = {"to": user_id, "messages": [{"type": "text", "text": message}]}
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    try:
        response = requests.post(
            LINE_PUSH_URL,
            json=payload,
            headers=headers,
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
        )
    except requests.exceptions.ConnectTimeout:
        print("[LINE] Push failed: the API did not answer in time.")
        return "ERROR: the LINE API didn't answer in time, so the notification wasn't sent."
    except requests.exceptions.ReadTimeout:
        # Deliberately not phrased as outright failure: LINE may have accepted
        # the push and just been slow to confirm it. Saying "it failed" here
        # would invite a retry that double-sends.
        print("[LINE] Push sent but not confirmed in time.")
        return (
            "ERROR: I sent the notification, but LINE took too long to confirm it. "
            "It may still have gone through."
        )
    except requests.exceptions.ConnectionError:
        print("[LINE] Push failed: could not reach the API.")
        return "ERROR: I couldn't reach the LINE API, so the notification wasn't sent."
    except requests.exceptions.RequestException as exc:
        # The exception detail goes to the console only - "MaxRetryError,
        # caused by NewConnectionError" is not something anyone wants read to
        # them, and it is not needed to fix the problem either.
        print(f"[LINE] Request error: {type(exc).__name__}: {exc}")
        return "ERROR: something went wrong talking to the LINE API, so nothing was sent."

    if response.status_code == 200:
        print(f"[LINE] Push sent ({len(message)} characters).")
        return "The LINE notification was sent."

    reason = _summarise_error_body(response)
    print(f"[LINE] Push failed: HTTP {response.status_code}: {reason}")

    if response.status_code == 401:
        return "ERROR: LINE rejected the request - the channel access token looks wrong."
    if response.status_code == 400:
        return f"ERROR: LINE rejected the notification - {reason}"
    if response.status_code == 429:
        # Also what LINE answers when the channel's monthly message quota is used up.
        return "ERROR: LINE is rate-limiting this channel (or its monthly message quota is used up), so the notification wasn't sent."
    if response.status_code >= 500:
        return "ERROR: LINE's API had a server error of its own, so the notification wasn't sent."
    return f"ERROR: LINE rejected the notification ({reason})."


# The name the older callers use (llm_brain.py's send_line_notification tool, plugins/trip_planner.py).
send_line_notification = push_line_notification


if __name__ == "__main__":
    import sys

    text = " ".join(sys.argv[1:]) or "Test notification from Jarvis."
    print(push_line_notification(text))
