"""
Today's agenda from Google Calendar, read-only.

    get_todays_agenda()
        -> "Agenda for Tuesday, 27 October 2026 (Asia/Bangkok): 3 events.
            - 09:00-10:00 Team stand-up (at: Zoom)
            - 13:30-15:00 Design review (at: Office 4F)
            - all day: Mom's birthday"

The Head of Productivity's get_todays_agenda tool (agents/productivity_agent.py)
is this function; it turns the list into the daily summary it sends to LINE.

HOW IT SIGNS IN
---------------
A Google Cloud service account, with the read-only Calendar scope. A service
account is a robot Google account with a key file and no password prompt, so
Jarvis can read the calendar at any time without anyone signing in. It sees a
calendar only once that calendar is SHARED with it: in Google Calendar, Settings
-> your calendar -> "Share with specific people" -> the service account's email
(client_email in the key file), "See all event details". Its own calendar is
empty, which is why GOOGLE_CALENDAR_ID is required rather than defaulting to
"primary".

The Calendar API is called directly over HTTPS (events.list) through google-auth,
which signs in with the key and refreshes the access token: no Google API client
library is needed.

CONFIGURATION (.env)
---------------------
    GOOGLE_SERVICE_ACCOUNT_FILE  path to the service account's JSON key - keep it
                                 outside the repo, or in .secrets/ (git ignores
                                 it); a relative path is read from the Jarvis
                                 folder
    GOOGLE_CALENDAR_ID           which calendar: your Gmail address for your main
                                 calendar (Settings -> Integrate calendar ->
                                 Calendar ID)
    JARVIS_TIMEZONE              "today" is midnight to midnight in this zone
                                 (default Asia/Bangkok, as everywhere in Jarvis)

Only an event's title, times and place are read (the request's `fields` list):
no descriptions, no attendee names or emails, no attachments. Events the owner
declined and Google's "working location" markers are left out.

Never raises: every failure is one sentence starting with "ERROR:" that says
what to fix, and the details (HTTP status, Google's reason) go to the console.
The key file's contents are never printed or returned.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

try:  # Loading .env here as well keeps the module usable standalone.
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - environment-dependent
    pass

CALENDAR_API = "https://www.googleapis.com/calendar/v3"
SCOPES = ("https://www.googleapis.com/auth/calendar.readonly",)
KEY_FILE_ENV = "GOOGLE_SERVICE_ACCOUNT_FILE"
CALENDAR_ENV = "GOOGLE_CALENDAR_ID"
DEFAULT_TIMEZONE = "Asia/Bangkok"
# Thailand has had no daylight saving since 1920, so a fixed offset is exact. It is the
# fallback when the IANA database is missing (Windows has none without the tzdata package),
# as in llm_brain.py.
_FIXED_ZONES = {"Asia/Bangkok": timezone(timedelta(hours=7), "ICT")}

# (connect, read) seconds. The agent is waiting on this inside a chat reply.
TIMEOUT = (5.0, 10.0)
MAX_EVENTS = 50
_MAX_TITLE = 120
_MAX_PLACE = 80
_FIELDS = (
    "timeZone,nextPageToken,"
    "items(status,eventType,summary,location,start,end,attendees(self,responseStatus))"
)
# A relative GOOGLE_SERVICE_ACCOUNT_FILE is read from the Jarvis folder, where .env is.
_BASE_DIR = Path(__file__).resolve().parent.parent

_session_lock = threading.Lock()
_session: Optional[tuple[tuple[str, float], Any, str]] = None  # ((path, mtime), AuthorizedSession, client_email)


class CalendarError(Exception):
    """A failure with the sentence the caller gets (`str(exc)`) and the detail the console gets."""

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def _zone(name: str) -> tzinfo:
    """The IANA zone `name`, its fixed-offset stand-in, or this machine's own zone."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - ZoneInfoNotFoundError, a bad name, no database
        fixed = _FIXED_ZONES.get(name)
        if fixed is not None:
            return fixed
        local = datetime.now().astimezone().tzinfo
        return local if local is not None else timezone.utc


def _zone_name() -> str:
    return (os.getenv("JARVIS_TIMEZONE") or "").strip() or DEFAULT_TIMEZONE


def day_bounds(day: date, zone: tzinfo) -> tuple[datetime, datetime]:
    """Midnight to midnight of `day` in `zone`, as aware datetimes."""
    start = datetime.combine(day, time.min, tzinfo=zone)
    return start, datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)


def _parse_moment(value: Any, zone: tzinfo) -> Optional[datetime]:
    """An RFC 3339 dateTime from the API, in `zone`; None when it is not one."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=zone)
    return moment.astimezone(zone)


def _parse_day(value: Any) -> Optional[date]:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _short_day(day: date) -> str:
    return f"{day:%a} {day.day} {day:%b}"


# ---------------------------------------------------------------------------
# Events -> lines
# ---------------------------------------------------------------------------


def _clean(text: Any, limit: int) -> str:
    """One line of calendar text: whitespace collapsed, cut to `limit` characters."""
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _declined(event: dict[str, Any]) -> bool:
    attendees = event.get("attendees")
    if not isinstance(attendees, list):
        return False
    return any(isinstance(a, dict) and a.get("self") and a.get("responseStatus") == "declined" for a in attendees)


def describe_event(event: dict[str, Any], day: date, zone: tzinfo) -> Optional[str]:
    """
    One event as an agenda line ("09:00-10:00 Stand-up (at: Zoom)"), or None for one that
    does not belong on it (cancelled, declined, a working-location marker, unreadable times).
    """
    if not isinstance(event, dict) or event.get("status") == "cancelled":
        return None
    if event.get("eventType") == "workingLocation" or _declined(event):
        return None
    start, end = event.get("start") or {}, event.get("end") or {}
    if not isinstance(start, dict) or not isinstance(end, dict):
        return None

    # A calendar shared as "free/busy only" gives times without titles.
    title = _clean(event.get("summary"), _MAX_TITLE) or "(busy - no title)"
    place = _clean(event.get("location"), _MAX_PLACE)
    suffix = f" (at: {place})" if place else ""

    if "date" in start:  # an all-day event; its end date is exclusive
        first, after = _parse_day(start.get("date")), _parse_day(end.get("date"))
        if first is None:
            return None
        last = (after - timedelta(days=1)) if after and after > first else first
        if last > day:
            return f"all day, until {_short_day(last)}: {title}{suffix}"
        return f"all day: {title}{suffix}"

    begins, ends = _parse_moment(start.get("dateTime"), zone), _parse_moment(end.get("dateTime"), zone)
    if begins is None:
        return None
    day_start, day_end = day_bounds(day, zone)
    left = f"{begins:%H:%M}" if begins >= day_start else f"(from {_short_day(begins.date())}) {begins:%H:%M}"
    if ends is None or ends == begins:
        return f"{left} {title}{suffix}"
    right = f"{ends:%H:%M}" if ends <= day_end else f"{ends:%H:%M} on {_short_day(ends.date())}"
    return f"{left}-{right} {title}{suffix}"


def format_agenda(events: list[dict[str, Any]], day: date, zone_name: str, zone: tzinfo, more: bool = False) -> str:
    """The tool's answer: a heading line, then one line per event, in the calendar's order."""
    lines = [line for line in (describe_event(e, day, zone) for e in events) if line]
    heading = f"Agenda for {day:%A}, {day.day} {day:%B %Y} ({zone_name})"
    if not lines:
        return f"{heading}: no events today."
    count = f"{len(lines)} event" + ("" if len(lines) == 1 else "s")
    note = f" (the first {MAX_EVENTS}; the calendar has more)" if more else ""
    return f"{heading}: {count}{note}.\n" + "\n".join(f"- {line}" for line in lines)


# ---------------------------------------------------------------------------
# Signing in
# ---------------------------------------------------------------------------


def _config() -> tuple[str, str]:
    key_file = (os.getenv(KEY_FILE_ENV) or "").strip().strip('"')
    calendar_id = (os.getenv(CALENDAR_ENV) or "").strip()
    missing = [name for name, value in ((KEY_FILE_ENV, key_file), (CALENDAR_ENV, calendar_id)) if not value]
    if missing:
        verb = "is" if len(missing) == 1 else "are"
        raise CalendarError(
            f"ERROR: Google Calendar isn't configured - {' and '.join(missing)} {verb} not set in the "
            ".env file. Nothing was read."
        )
    return key_file, calendar_id


def configured() -> bool:
    """True when both settings are present (says nothing about whether they work)."""
    return bool((os.getenv(KEY_FILE_ENV) or "").strip() and (os.getenv(CALENDAR_ENV) or "").strip())


def _key_path(key_file: str) -> Path:
    path = Path(os.path.expandvars(os.path.expanduser(key_file)))
    return path if path.is_absolute() else _BASE_DIR / path


def _read_key(path: Path) -> dict[str, Any]:
    """The key file's JSON, checked to be a service account key (not an OAuth client file)."""
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CalendarError(
            f"ERROR: the Google service account key file ({KEY_FILE_ENV}) was not found, so the calendar "
            "was not read.",
            f"no file at {path}",
        ) from None
    except (OSError, ValueError) as exc:
        raise CalendarError(
            f"ERROR: the Google service account key file ({KEY_FILE_ENV}) could not be read as JSON, so the "
            "calendar was not read.",
            f"{path}: {type(exc).__name__}",
        ) from None
    if not isinstance(info, dict):
        raise CalendarError(f"ERROR: {KEY_FILE_ENV} is not a service account key file.", "not a JSON object")
    if "installed" in info or "web" in info:
        raise CalendarError(
            f"ERROR: {KEY_FILE_ENV} points at an OAuth client file, not a service account key. Create a "
            "service account in the Google Cloud console and download its JSON key instead.",
            "OAuth client JSON",
        )
    if info.get("type") != "service_account" or not info.get("client_email") or not info.get("private_key"):
        raise CalendarError(
            f"ERROR: {KEY_FILE_ENV} is not a service account key file (it needs type, client_email and "
            "private_key).",
            f"type={info.get('type')!r}",
        )
    return info


def _authorized_session() -> tuple[Any, str]:
    """(AuthorizedSession, client_email), built once per key file and reused; the token refreshes itself."""
    global _session
    key_file, _ = _config()
    path = _key_path(key_file)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = -1.0
    with _session_lock:
        if _session is not None and _session[0] == (str(path), mtime):
            return _session[1], _session[2]
        info = _read_key(path)
        try:
            from google.auth.transport.requests import AuthorizedSession
            from google.oauth2 import service_account
        except ImportError as exc:
            raise CalendarError(
                "ERROR: the google-auth package is not installed (pip install -r requirements.txt), so the "
                "calendar was not read.",
                f"{type(exc).__name__}: {exc}",
            ) from None
        try:
            credentials = service_account.Credentials.from_service_account_info(info, scopes=list(SCOPES))
        except (ValueError, KeyError) as exc:
            raise CalendarError(
                f"ERROR: the key in {KEY_FILE_ENV} could not be loaded - download a fresh JSON key for the "
                "service account.",
                f"{type(exc).__name__}",
            ) from None
        session = AuthorizedSession(credentials)
        _session = ((str(path), mtime), session, str(info.get("client_email")))
        return session, _session[2]


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------


def _google_reason(response: Any) -> tuple[str, str]:
    """(reason, message) from a Google API error body, e.g. ("notFound", "Not Found")."""
    try:
        error = response.json().get("error") or {}
    except (ValueError, AttributeError):
        return "", (getattr(response, "text", "") or "")[:200]
    reason = ""
    errors = error.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        reason = str(errors[0].get("reason") or "")
    details = error.get("details")
    if not reason and isinstance(details, list):
        for item in details:
            if isinstance(item, dict) and item.get("reason"):
                reason = str(item["reason"])
                break
    return reason, str(error.get("message") or "")[:200]


def _http_error(response: Any, client_email: str) -> CalendarError:
    status = getattr(response, "status_code", 0)
    reason, message = _google_reason(response)
    detail = f"HTTP {status} {reason or '-'}: {message}"
    if status == 403 and (reason in ("accessNotConfigured", "SERVICE_DISABLED") or "has not been used" in message or "disabled" in message):
        return CalendarError(
            "ERROR: the Google Calendar API is not enabled in the service account's Google Cloud project. "
            "Enable it (APIs & Services -> Library -> Google Calendar API), then try again.",
            detail,
        )
    if status in (403, 404):
        return CalendarError(
            f"ERROR: Google Calendar would not show that calendar - check {CALENDAR_ENV}, and that the calendar "
            "is shared with the service account's email (client_email in the key file; it is printed on "
            "Jarvis's console) with \"See all event details\".",
            f"{detail} (service account: {client_email}; share the calendar with it)",
        )
    if status == 401:
        return CalendarError("ERROR: Google rejected the service account's sign-in, so the calendar was not read.", detail)
    if status == 429 or reason in ("rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"):
        return CalendarError("ERROR: Google Calendar is rate-limiting these requests right now. Try again in a minute.", detail)
    if status >= 500:
        return CalendarError("ERROR: Google Calendar had a server error of its own, so the agenda could not be read.", detail)
    return CalendarError(f"ERROR: Google Calendar refused the request (HTTP {status}).", detail)


def fetch_events(day: date, zone_name: str, zone: tzinfo) -> tuple[list[dict[str, Any]], bool]:
    """(events overlapping `day`, more_than_shown). Raises CalendarError."""
    _, calendar_id = _config()
    session, client_email = _authorized_session()
    day_start, day_end = day_bounds(day, zone)
    url = f"{CALENDAR_API}/calendars/{quote(calendar_id, safe='')}/events"
    params = {
        "timeMin": day_start.isoformat(),
        "timeMax": day_end.isoformat(),
        "singleEvents": "true",  # each repeat of a recurring event, at its own time
        "orderBy": "startTime",
        "maxResults": str(MAX_EVENTS),
        "timeZone": zone_name,
        "fields": _FIELDS,
    }
    try:
        import requests
        from google.auth.exceptions import RefreshError, TransportError
    except ImportError as exc:
        raise CalendarError("ERROR: the requests or google-auth package is missing, so the calendar was not read.", str(exc)) from None
    try:
        response = session.get(url, params=params, timeout=TIMEOUT)
    except RefreshError as exc:
        raise CalendarError(
            "ERROR: Google refused the service account's key (it may have been deleted or disabled), so the "
            "calendar was not read. Download a new key for the service account.",
            f"RefreshError: {str(exc)[:200]}",
        ) from None
    except (requests.exceptions.Timeout, TimeoutError):
        raise CalendarError("ERROR: Google Calendar didn't answer in time, so the agenda could not be read.", "timeout") from None
    except (requests.exceptions.ConnectionError, TransportError) as exc:
        raise CalendarError("ERROR: I couldn't reach Google Calendar, so the agenda could not be read.", type(exc).__name__) from None
    except requests.exceptions.RequestException as exc:
        raise CalendarError("ERROR: something went wrong talking to Google Calendar.", f"{type(exc).__name__}: {exc}") from None

    if response.status_code != 200:
        raise _http_error(response, client_email)
    try:
        body = response.json()
    except ValueError:
        raise CalendarError("ERROR: Google Calendar's answer could not be read.", "the body is not JSON") from None
    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(items, list):
        raise CalendarError("ERROR: Google Calendar's answer could not be read.", "no items list")
    return items, bool(body.get("nextPageToken"))


def get_todays_agenda(now: Optional[datetime] = None) -> str:
    """
    Today's events (in JARVIS_TIMEZONE) as a heading line and one line per event, or one
    "ERROR: ..." sentence. Never raises.
    """
    zone_name = _zone_name()
    zone = _zone(zone_name)
    today = (now or datetime.now(timezone.utc)).astimezone(zone).date()
    try:
        events, more = fetch_events(today, zone_name, zone)
    except CalendarError as exc:
        print(f"[Calendar] Not read: {exc.detail or exc}")
        return str(exc)
    except Exception as exc:  # noqa: BLE001 - a tool must not kill the loop
        print(f"[Calendar] Unexpected failure: {type(exc).__name__}: {exc}")
        return "ERROR: reading Google Calendar failed unexpectedly, so there is no agenda. Do not guess one."
    agenda = format_agenda(events, today, zone_name, zone, more)
    shown = agenda.count("\n- ")
    print(f"[Calendar] Read {today.isoformat()}: {shown} event(s) on the agenda ({len(events)} returned).")
    return agenda


if __name__ == "__main__":
    # .venv\Scripts\python -m utils.calendar_reader   - checks the setup and prints today's agenda.
    print(get_todays_agenda())
