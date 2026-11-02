"""
The Classroom agent: the owner's Google Classroom, read-only, through Google's
official API client (googleapiclient, "classroom" v1).

    get_courses()               the active courses: id, name, section, room, and
                                whether the owner teaches it or is enrolled
    get_coursework(course_id)   one course's published assignments: title, due
                                date (local time), how far off it is, type,
                                points, a short description and a link

Each returns a compact JSON string for the language model and never raises: a
problem comes back as {"error": "..."} worded for the model to pass on - most
often "not signed in yet: run python utils/classroom_auth.py".

It signs in as the owner with token.json, made once by utils/classroom_auth.py
from credentials.json (an OAuth "Desktop app" client). The scopes are read-only
(classroom.courses.readonly, classroom.coursework.me.readonly): nothing here can
post, submit, grade or change anything. An expired access token is refreshed with
the saved refresh token, and the refreshed token is written back when the file
is writable (in Docker it is mounted read-only; the refresh then just lives in
memory).

Who may ask is decided by the caller (llm_brain's Classroom tools: the owner
only), not here - this module only talks to Google.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional

from utils.classroom_auth import SCOPES, token_file

MAX_COURSES = 100
MAX_COURSEWORK = 40          # newest first by due date; more is not useful in one answer
MAX_DESCRIPTION = 200
PAGE_SIZE = 50
HTTP_TIMEOUT = 20            # seconds per request; the manager's whole turn has a budget too
AUTH_HINT = "Ask the owner to run:  python utils/classroom_auth.py"
_COURSE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")  # numeric ids, or "d:<alias>" / "p:<alias>"

_lock = threading.Lock()
_cached: dict[str, Any] = {"service": None, "token_mtime": None}


class ClassroomError(Exception):
    """A failure the model should hear about, worded for it."""


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------- signing in


def _credentials() -> Any:
    try:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError:
        raise ClassroomError("The Google client libraries are not installed. Ask the owner to run: pip install -r requirements.txt") from None

    path = token_file()
    if not path.is_file():
        raise ClassroomError(f"Google Classroom is not connected yet. {AUTH_HINT}")
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        creds = Credentials.from_authorized_user_info(saved, SCOPES)
    except (ValueError, OSError, TypeError, AttributeError):
        raise ClassroomError(f"The saved Google sign-in (token.json) is unreadable. {AUTH_HINT}") from None
    # What the token was granted is in the file's own "scopes" (google-auth does not check it
    # against the scopes passed above). A token made for something else cannot read Classroom.
    granted = saved.get("scopes") if isinstance(saved, dict) else None
    if isinstance(granted, str):
        granted = granted.split()
    if granted and not set(SCOPES) <= set(granted):
        raise ClassroomError(f"The saved Google sign-in does not include Classroom access. {AUTH_HINT}")
    if not creds.valid:
        if not creds.refresh_token:
            raise ClassroomError(f"The Google sign-in has expired. {AUTH_HINT}")
        try:
            creds.refresh(Request())
        except RefreshError:
            raise ClassroomError(f"The Google sign-in was revoked or has expired. {AUTH_HINT}") from None
        except Exception:  # noqa: BLE001 - a network problem while refreshing
            raise ClassroomError("Could not reach Google to renew the sign-in. Check the internet connection and try again.") from None
        try:
            path.write_text(creds.to_json(), encoding="utf-8")
        except OSError:
            pass  # a read-only mount: the refreshed token still works for this process
    return creds


def _service() -> Any:
    """The Classroom client, built once per sign-in (rebuilt when token.json changes)."""
    path = token_file()
    mtime = path.stat().st_mtime if path.is_file() else None
    with _lock:
        if _cached["service"] is not None and _cached["token_mtime"] == mtime:
            return _cached["service"]
        creds = _credentials()
        try:
            import google_auth_httplib2
            import httplib2
            from googleapiclient.discovery import build
        except ImportError:
            raise ClassroomError("The Google API client is not installed. Ask the owner to run: pip install -r requirements.txt") from None
        http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=HTTP_TIMEOUT))
        # static_discovery: the API description ships with the library, so building needs no network.
        service = build("classroom", "v1", http=http, cache_discovery=False, static_discovery=True)
        _cached.update(service=service, token_mtime=path.stat().st_mtime if path.is_file() else None)
        return service


def _api_error(exc: Exception, what: str) -> ClassroomError:
    """googleapiclient's HttpError (or a network error) -> one sentence for the model."""
    status = getattr(getattr(exc, "resp", None), "status", None)
    text = str(exc)
    if status == 404:
        return ClassroomError(f"Google Classroom has no {what} - check the id (list the courses first).")
    if status == 401:
        _forget_service()
        return ClassroomError(f"Google rejected the saved sign-in. {AUTH_HINT}")
    if status == 403:
        if "SERVICE_DISABLED" in text or "accessNotConfigured" in text or "has not been used" in text:
            return ClassroomError(
                "The Google Classroom API is not enabled in the Google Cloud project that made credentials.json. "
                "Enable it there (APIs & Services -> Library -> Google Classroom API), then try again."
            )
        return ClassroomError(f"Google Classroom does not allow this account to see that {what}.")
    if status == 429:
        return ClassroomError("Google Classroom is limiting requests right now. Try again in a minute.")
    if isinstance(status, int) and status >= 500:
        return ClassroomError("Google Classroom had a problem answering. Try again shortly.")
    return ClassroomError("Could not reach Google Classroom. Check the internet connection and try again.")


def _forget_service() -> None:
    with _lock:
        _cached.update(service=None, token_mtime=None)


# ------------------------------------------------------------------- reading


def _pages(request_for: Any, key: str, limit: int) -> list[dict[str, Any]]:
    """Every item of a paged list call, up to `limit`."""
    items: list[dict[str, Any]] = []
    token: Optional[str] = None
    while len(items) < limit:
        response = request_for(token).execute(num_retries=1)
        items.extend(response.get(key, []) or [])
        token = response.get("nextPageToken")
        if not token:
            break
    return items[:limit]


def get_courses(service: Any = None) -> str:
    """The owner's ACTIVE courses, as JSON: {"count", "courses": [{id, name, section, room, role}]}."""
    try:
        api = service or _service()
        courses = api.courses()
        fields = "courses(id,name,section,room),nextPageToken"
        every = _pages(lambda t: courses.list(courseStates=["ACTIVE"], pageSize=PAGE_SIZE, pageToken=t, fields=fields), "courses", MAX_COURSES)
        taught = {
            c.get("id")
            for c in _pages(lambda t: courses.list(courseStates=["ACTIVE"], teacherId="me", pageSize=PAGE_SIZE, pageToken=t, fields="courses(id),nextPageToken"), "courses", MAX_COURSES)
        }
    except ClassroomError as exc:
        return _dump({"error": str(exc)})
    except Exception as exc:  # noqa: BLE001 - HttpError, socket errors
        return _dump({"error": str(_api_error(exc, "course list"))})
    rows = []
    for course in every:
        row = {"id": course.get("id"), "name": course.get("name") or "(untitled)"}
        for field in ("section", "room"):
            if course.get(field):
                row[field] = course[field]
        row["role"] = "teacher" if course.get("id") in taught else "student"
        rows.append(row)
    return _dump({"count": len(rows), "courses": rows})


def _zone() -> Any:
    """Where due dates are shown: JARVIS_TIMEZONE (default Asia/Bangkok), like the brain's clock."""
    name = os.environ.get("JARVIS_TIMEZONE", "").strip() or "Asia/Bangkok"
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - no tz database on Windows without tzdata
        return timezone(timedelta(hours=7), "ICT") if name == "Asia/Bangkok" else timezone.utc


def _due(work: dict[str, Any], now: datetime) -> tuple[Optional[datetime], Optional[str]]:
    """The due moment (aware, local) and how far off it is, or (None, None) with no due date."""
    day = work.get("dueDate") or {}
    if not all(isinstance(day.get(k), int) for k in ("year", "month", "day")):
        return None, None
    # Classroom stores due dates in UTC and leaves zero fields out: {"hours": 16} is 16:00
    # and {} is midnight. Only a missing dueTime (it should come with every dueDate) is
    # read as the end of that day.
    if "dueTime" in work:
        clock = work.get("dueTime") or {}
        hours, minutes = int(clock.get("hours", 0)), int(clock.get("minutes", 0))
    else:
        hours, minutes = 23, 59
    try:
        moment = datetime.combine(
            date(day["year"], day["month"], day["day"]), time(hours, minutes), tzinfo=timezone.utc
        ).astimezone(now.tzinfo)
    except (ValueError, TypeError):
        return None, None
    days = (moment.date() - now.date()).days
    if moment < now:
        when = "overdue" if days >= -1 else f"overdue by {-days} days"
    elif days == 0:
        when = "due today"
    elif days == 1:
        when = "due tomorrow"
    else:
        when = f"due in {days} days"
    return moment, when


def get_coursework(course_id: str, service: Any = None, now: Optional[datetime] = None) -> str:
    """
    One course's PUBLISHED assignments, as JSON: {"course_id", "count", "coursework": [...]},
    upcoming first (soonest due), then the ones with no due date, then past ones (latest first).
    """
    course_id = str(course_id or "").strip()
    if not _COURSE_ID.match(course_id):
        return _dump({"error": "Give a course_id from the course list (call the courses tool first)."})
    try:
        api = service or _service()
        coursework = api.courses().courseWork()
        fields = "courseWork(id,title,description,dueDate,dueTime,workType,maxPoints,alternateLink),nextPageToken"
        items = _pages(
            lambda t: coursework.list(courseId=course_id, courseWorkStates=["PUBLISHED"], pageSize=PAGE_SIZE, pageToken=t, fields=fields),
            "courseWork",
            200,
        )
    except ClassroomError as exc:
        return _dump({"error": str(exc)})
    except Exception as exc:  # noqa: BLE001
        return _dump({"error": str(_api_error(exc, "course with that id"))})

    now = (now or datetime.now(timezone.utc)).astimezone(_zone())
    upcoming, undated, past = [], [], []
    for work in items:
        moment, when = _due(work, now)
        row: dict[str, Any] = {"id": work.get("id"), "title": work.get("title") or "(untitled)"}
        if moment is not None:
            row["due"] = moment.strftime("%a %d %b %Y %H:%M")
            row["when"] = when
        if work.get("workType"):
            row["type"] = str(work["workType"]).lower().replace("_", " ")
        if work.get("maxPoints") is not None:
            row["points"] = work["maxPoints"]
        description = re.sub(r"\s+", " ", work.get("description") or "").strip()
        if description:
            row["about"] = description if len(description) <= MAX_DESCRIPTION else description[: MAX_DESCRIPTION - 1] + "…"
        if work.get("alternateLink"):
            row["link"] = work["alternateLink"]
        (upcoming if moment and moment >= now else past if moment else undated).append((moment, row))
    upcoming.sort(key=lambda item: item[0])
    past.sort(key=lambda item: item[0], reverse=True)
    ordered = [row for _, row in upcoming + undated + past][:MAX_COURSEWORK]
    payload: dict[str, Any] = {"course_id": course_id, "count": len(items), "coursework": ordered}
    if len(items) > len(ordered):
        payload["note"] = f"Showing {len(ordered)} of {len(items)}: the upcoming ones first."
    return _dump(payload)
