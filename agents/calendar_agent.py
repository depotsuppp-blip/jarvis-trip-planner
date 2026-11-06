"""
The Head of Schedule: the owner's Google Calendar, through Google's official API client
(googleapiclient, "calendar" v3), signed in as the owner with token.json
(utils/google_auth.py, scope calendar.events).

    list_upcoming_events(days_ahead=7)
        read: the owner's events from now until `days_ahead` days ahead, on the primary
        calendar, in time order - title, start and end in the user's time zone, all-day or
        not, place, a short description, a link
    create_event(title, start_time, end_time="", description="")
        write: one event on the primary calendar. start_time / end_time are ISO 8601:
        "2026-11-07T10:00" (in the user's time zone unless it carries an offset) or a date
        "2026-11-07" for an all-day event; no end_time means one hour (a day, all-day)

Each returns a compact JSON string for the language model and never raises: a problem
comes back as {"error": "..."} worded for the model to pass on - most often "not
connected yet: run python utils/google_auth.py".

WRITING NEEDS A YES, UPSTREAM. create_event writes when it is called. The brain calls it
only after the owner approved that exact event - the web hub's Approval Card, or the
spoken yes/no by voice (llm_brain: delegate_to_calendar is confirmed per call when its
action is create_event, with confirmation() below as the question). Who may ask at all is
the brain's rule too: the owner only. This module only talks to Google, and it checks the
event (check_event) the same way for the question and for the write, so what was
approved is what is written. The same event asked for twice within SAME_EVENT_SECONDS is
not written twice.

The time zone is the user's (JARVIS_TIMEZONE, default Asia/Bangkok - as everywhere in
Jarvis): times are shown in it and a time without an offset is read in it.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from utils.google_auth import SCOPES_BY_SERVICE, SIGN_IN_COMMAND, GoogleAuthError, load_credentials, token_file

SCOPES = SCOPES_BY_SERVICE["calendar"]
CALENDAR_ID = "primary"  # the owner's own calendar
SERVICE_NAME = "Google Calendar"
AUTH_HINT = f"Ask the owner to run:  {SIGN_IN_COMMAND}"

DEFAULT_DAYS = 7
MAX_DAYS = 60
MAX_EVENTS = 50
MAX_TITLE = 200
MAX_DESCRIPTION_IN = 4000   # what may be written into an event
MAX_DESCRIPTION_OUT = 300   # what is read back to the model per event
MAX_LENGTH = timedelta(days=31)
DEFAULT_LENGTH = timedelta(hours=1)
SAME_EVENT_SECONDS = 120
HTTP_TIMEOUT = 20           # seconds per request; the manager's whole turn has a budget too
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_lock = threading.Lock()
_cached: dict[str, Any] = {"service": None, "token_mtime": None}
_recent: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}  # (title, start, end) -> (when, event)


class CalendarError(Exception):
    """A failure the model should hear about, worded for it."""


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _zone() -> Any:
    """The user's time zone (JARVIS_TIMEZONE, else Bangkok) - the one the brain tells the model it is in."""
    try:
        from llm_brain import DEFAULT_TIMEZONE, _zone as zone_for  # lazy, as in agents/base.py

        return zone_for(os.environ.get("JARVIS_TIMEZONE", "").strip() or DEFAULT_TIMEZONE)
    except Exception:  # noqa: BLE001 - the brain cannot be imported here
        return timezone(timedelta(hours=7), "ICT")


def _zone_name() -> str:
    return os.environ.get("JARVIS_TIMEZONE", "").strip() or "Asia/Bangkok"


# ---------------------------------------------------------------- signing in


def _service() -> Any:
    """The Calendar client, built once per sign-in (rebuilt when token.json changes)."""
    path = token_file()
    mtime = path.stat().st_mtime if path.is_file() else None
    with _lock:
        if _cached["service"] is not None and _cached["token_mtime"] == mtime:
            return _cached["service"]
    try:
        creds = load_credentials(SCOPES, SERVICE_NAME, AUTH_HINT)
    except GoogleAuthError as exc:
        raise CalendarError(str(exc)) from None
    try:
        import google_auth_httplib2
        import httplib2
        from googleapiclient.discovery import build
    except ImportError:
        raise CalendarError("The Google API client is not installed. Ask the owner to run: pip install -r requirements.txt") from None
    http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=HTTP_TIMEOUT))
    # static_discovery: the API description ships with the library, so building needs no network.
    service = build("calendar", "v3", http=http, cache_discovery=False, static_discovery=True)
    with _lock:
        _cached.update(service=service, token_mtime=path.stat().st_mtime if path.is_file() else None)
    return service


def _forget_service() -> None:
    with _lock:
        _cached.update(service=None, token_mtime=None)


def _api_error(exc: Exception, what: str) -> CalendarError:
    """googleapiclient's HttpError (or a network error) -> one sentence for the model."""
    status = getattr(getattr(exc, "resp", None), "status", None)
    text = str(exc)
    if status == 401:
        _forget_service()
        return CalendarError(f"Google rejected the saved sign-in. {AUTH_HINT}")
    if status == 403:
        if "SERVICE_DISABLED" in text or "accessNotConfigured" in text or "has not been used" in text:
            return CalendarError(
                "The Google Calendar API is not enabled in the Google Cloud project that made credentials.json. "
                "Enable it there (APIs & Services -> Library -> Google Calendar API), then try again."
            )
        if "insufficient" in text.lower() or "scope" in text.lower():
            return CalendarError(f"The saved Google sign-in does not allow {what}. {AUTH_HINT}")
        return CalendarError(f"Google Calendar does not allow this account to {what}.")
    if status == 400:
        reason = _google_message(exc)
        return CalendarError(f"Google Calendar refused to {what}" + (f": {reason}" if reason else "."))
    if status == 404:
        return CalendarError("Google Calendar could not find the calendar.")
    if status == 429:
        return CalendarError("Google Calendar is limiting requests right now. Try again in a minute.")
    if isinstance(status, int) and status >= 500:
        return CalendarError("Google Calendar had a problem answering. Try again shortly.")
    return CalendarError("Could not reach Google Calendar. Check the internet connection and try again.")


def _google_message(exc: Exception) -> str:
    """Google's own error message from an HttpError body, cut short; "" when there is none."""
    content = getattr(exc, "content", b"")
    try:
        body = json.loads(content.decode("utf-8") if isinstance(content, bytes) else content)
        message = str(body.get("error", {}).get("message", "")).strip()
    except (ValueError, AttributeError):
        message = ""
    return message[:200]


# --------------------------------------------------------------- the event shape


def _moment(value: Any, zone: Any) -> tuple[Optional[datetime], Optional[date]]:
    """A start or end from Google ({"dateTime": ...} or {"date": ...}) as (moment in `zone`, None) or (None, day)."""
    if not isinstance(value, dict):
        return None, None
    if value.get("dateTime"):
        try:
            return datetime.fromisoformat(str(value["dateTime"]).replace("Z", "+00:00")).astimezone(zone), None
        except ValueError:
            return None, None
    if value.get("date"):
        try:
            return None, date.fromisoformat(str(value["date"]))
        except ValueError:
            return None, None
    return None, None


def _clean(text: Any, limit: int) -> str:
    value = " ".join(_CONTROL.sub(" ", str(text or "")).split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _when(start: tuple[Optional[datetime], Optional[date]], end: tuple[Optional[datetime], Optional[date]]) -> str:
    """"Fri 7 Nov 10:00-11:00", "Fri 7 Nov (all day)", "Fri 7 Nov 22:00 - Sat 8 Nov 01:00"."""
    (start_at, start_day), (end_at, end_day) = start, end
    if start_day is not None:
        last = (end_day - timedelta(days=1)) if end_day and end_day > start_day else start_day
        first = f"{start_day:%a} {start_day.day} {start_day:%b}"
        return f"{first} (all day)" if last == start_day else f"{first} - {last:%a} {last.day} {last:%b} (all day)"
    if start_at is None:
        return ""
    first = f"{start_at:%a} {start_at.day} {start_at:%b} {start_at:%H:%M}"
    if end_at is None:
        return first
    if end_at.date() == start_at.date():
        return f"{first}-{end_at:%H:%M}"
    return f"{first} - {end_at:%a} {end_at.day} {end_at:%b} {end_at:%H:%M}"


def _event_row(event: dict[str, Any], zone: Any) -> dict[str, Any]:
    start, end = _moment(event.get("start"), zone), _moment(event.get("end"), zone)
    all_day = start[1] is not None
    row: dict[str, Any] = {
        "id": event.get("id"),
        "title": _clean(event.get("summary"), MAX_TITLE) or "(no title)",
        "when": _when(start, end),
        "all_day": all_day,
        "start": (start[1].isoformat() if all_day else start[0].isoformat()) if (start[0] or start[1]) else None,
        "end": (end[1].isoformat() if all_day and end[1] else end[0].isoformat() if end[0] else None),
    }
    for field, limit in (("location", 200), ("description", MAX_DESCRIPTION_OUT)):
        value = _clean(event.get(field), limit)
        if value:
            row[field] = value
    if event.get("htmlLink"):
        row["link"] = event["htmlLink"]
    return row


def _declined(event: dict[str, Any]) -> bool:
    """True when the owner said no to this invitation."""
    for person in event.get("attendees") or []:
        if isinstance(person, dict) and person.get("self") and person.get("responseStatus") == "declined":
            return True
    return False


# ------------------------------------------------------------------------ read


def list_upcoming_events(days_ahead: Any = DEFAULT_DAYS, service: Any = None, now: Optional[datetime] = None) -> str:
    """
    The owner's events from now until `days_ahead` days ahead (1 to MAX_DAYS), as JSON:
    {"time_zone", "from", "to", "count", "more", "events": [{id, title, when, all_day, start,
    end, location?, description?, link?}]}. Events already under way are included; ones the
    owner declined, cancelled ones and "working location" markers are not.
    """
    try:
        days = int(float(days_ahead)) if days_ahead not in (None, "") else DEFAULT_DAYS
    except (TypeError, ValueError):
        return _dump({"error": "days_ahead must be a whole number of days, e.g. 7."})
    days = max(1, min(days, MAX_DAYS))
    zone = _zone()
    start = (now or datetime.now(timezone.utc)).astimezone(zone)
    end = start + timedelta(days=days)
    try:
        api = service or _service()
        response = api.events().list(
            calendarId=CALENDAR_ID,
            timeMin=start.astimezone(timezone.utc).isoformat(),
            timeMax=end.astimezone(timezone.utc).isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=MAX_EVENTS,
            fields="items(id,summary,description,location,start,end,status,htmlLink,eventType,"
            "attendees(self,responseStatus)),nextPageToken",
        ).execute(num_retries=1)
    except CalendarError as exc:
        return _dump({"error": str(exc)})
    except Exception as exc:  # noqa: BLE001 - HttpError, socket errors
        return _dump({"error": str(_api_error(exc, "read the calendar"))})
    events = [
        _event_row(event, zone)
        for event in response.get("items", []) or []
        if event.get("status") != "cancelled" and event.get("eventType") != "workingLocation" and not _declined(event)
    ]
    return _dump({
        "time_zone": _zone_name(),
        "from": start.isoformat(timespec="minutes"),
        "to": end.isoformat(timespec="minutes"),
        "count": len(events),
        "more": bool(response.get("nextPageToken")),
        "events": events,
    })


# ----------------------------------------------------------------------- write


@dataclass(frozen=True)
class EventPlan:
    """A checked event, ready to show for approval and to write (check_event)."""

    title: str
    description: str
    start: str  # ISO: a date for an all-day event, else a date-time with its offset
    end: str
    all_day: bool
    when: str  # "Friday 7 November 2026, 10:00-11:00 (Asia/Bangkok)"

    def body(self) -> dict[str, Any]:
        """The events.insert request body."""
        if self.all_day:
            start, end = {"date": self.start}, {"date": self.end}
        else:
            start, end = {"dateTime": self.start, "timeZone": _zone_name()}, {"dateTime": self.end, "timeZone": _zone_name()}
        body: dict[str, Any] = {"summary": self.title, "start": start, "end": end}
        if self.description:
            body["description"] = self.description
        return body


def _parse_time(value: Any, zone: Any, what: str) -> tuple[Optional[datetime], Optional[date]]:
    """An ISO 8601 date-time (offset optional: the user's zone) or a date. Raises ValueError naming `what`."""
    text = str(value or "").strip()
    if not text:
        return None, None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return None, date.fromisoformat(text)
        except ValueError:
            raise ValueError(f"{what} {text!r} is not a real date.") from None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        raise ValueError(
            f"{what} {text!r} is not an ISO 8601 time: write it like 2026-11-07T10:00 (or 2026-11-07 for all day)."
        ) from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=zone)
    return moment.astimezone(zone), None


def check_event(title: Any, start_time: Any, end_time: Any = "", description: Any = "") -> EventPlan:
    """
    The event as it will be written, or ValueError with what to change. Used for the approval
    question and again for the write, so what the owner approved is exactly what is written.
    """
    name = _clean(title, MAX_TITLE + 1)
    if not name:
        raise ValueError("The event needs a title.")
    if len(name) > MAX_TITLE:
        raise ValueError(f"The title is too long (at most {MAX_TITLE} characters).")
    notes = _CONTROL.sub(" ", str(description or "")).strip()
    if len(notes) > MAX_DESCRIPTION_IN:
        raise ValueError(f"The description is too long (at most {MAX_DESCRIPTION_IN} characters).")
    zone = _zone()
    start_at, start_day = _parse_time(start_time, zone, "start_time")
    if start_at is None and start_day is None:
        raise ValueError("The event needs a start_time, like 2026-11-07T10:00 (or 2026-11-07 for all day).")
    end_at, end_day = _parse_time(end_time, zone, "end_time")

    if start_day is not None:  # all day
        if end_at is not None:
            raise ValueError("An all-day event (a date as start_time) needs a date as end_time too, or none.")
        last = end_day or start_day
        if last < start_day:
            raise ValueError("end_time is before start_time.")
        end_exclusive = last + timedelta(days=1)  # Google's all-day end is the day after the last one
        if end_exclusive - start_day > MAX_LENGTH:
            raise ValueError("The event is longer than 31 days.")
        when = f"{start_day:%A} {start_day.day} {start_day:%B %Y}" + (
            "" if last == start_day else f" to {last:%A} {last.day} {last:%B %Y}"
        ) + " (all day)"
        return EventPlan(name, notes, start_day.isoformat(), end_exclusive.isoformat(), True, when)

    assert start_at is not None
    if end_day is not None:
        raise ValueError("A timed event (a time as start_time) needs a time as end_time too, or none.")
    finish = end_at or (start_at + DEFAULT_LENGTH)
    if finish <= start_at:
        raise ValueError("end_time must be after start_time.")
    if finish - start_at > MAX_LENGTH:
        raise ValueError("The event is longer than 31 days.")
    span = f"{start_at:%H:%M}-{finish:%H:%M}" if finish.date() == start_at.date() else (
        f"{start_at:%H:%M} to {finish:%A} {finish.day} {finish:%B} {finish:%H:%M}"
    )
    when = f"{start_at:%A} {start_at.day} {start_at:%B %Y}, {span} ({_zone_name()})"
    return EventPlan(name, notes, start_at.isoformat(timespec="minutes"), finish.isoformat(timespec="minutes"), False, when)


def confirmation(params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """
    The question the owner answers before an event is written, and the details the web hub's
    Approval Card lists under it. ValueError (from check_event) when the event cannot be made.
    """
    plan = check_event(params.get("title"), params.get("start_time"), params.get("end_time"), params.get("description"))
    question = f"Add \"{plan.title}\" to your Google Calendar on {plan.when}?"
    details: dict[str, Any] = {"title": plan.title, "when": plan.when}
    if plan.description:
        details["description"] = plan.description
    if not str(params.get("end_time") or "").strip():
        details["length"] = "1 hour (no end time was given)" if not plan.all_day else "1 day"
    return question, details


def create_event(title: Any, start_time: Any, end_time: Any = "", description: Any = "", service: Any = None) -> str:
    """Writes one event on the owner's primary calendar. JSON: {"status": "created", "event": {...}} or {"error"}."""
    try:
        plan = check_event(title, start_time, end_time, description)
    except ValueError as exc:
        return _dump({"error": f"Nothing was added: {exc}"})
    key = (plan.title, plan.start, plan.end)
    now = time.monotonic()
    with _lock:
        for old, (when, _event) in list(_recent.items()):
            if now - when > SAME_EVENT_SECONDS:
                del _recent[old]
        again = _recent.get(key)
    if again is not None:
        return _dump({"status": "already_created", "note": "This exact event was added moments ago, so it was not added twice.", "event": again[1]})
    try:
        api = service or _service()
        created = api.events().insert(calendarId=CALENDAR_ID, body=plan.body(), sendUpdates="none").execute(num_retries=0)
    except CalendarError as exc:
        return _dump({"error": f"Nothing was added: {exc}"})
    except Exception as exc:  # noqa: BLE001 - HttpError, socket errors
        return _dump({"error": f"Nothing was added: {_api_error(exc, 'add the event')}"})
    row = _event_row(created, _zone())
    with _lock:
        _recent[key] = (time.monotonic(), row)
    print(f"[Agent:schedule] Added to Google Calendar: {plan.when}.")
    return _dump({"status": "created", "event": row})


def signed_in() -> bool:
    """True when the saved sign-in holds the Calendar scope (no network call)."""
    from utils.google_auth import missing_scopes

    return token_file().is_file() and not missing_scopes(SCOPES)


if __name__ == "__main__":
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    print(list_upcoming_events(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DAYS))
