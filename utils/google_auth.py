"""
One Google sign-in for everything Jarvis does as the owner: Google Classroom
(agents/classroom_agent.py, read-only) and Google Calendar (agents/calendar_agent.py,
the Head of Schedule: read and add events).

    python utils/google_auth.py            sign in (again only if a scope is missing), check it works
    python utils/google_auth.py --force    sign in again even if the saved token has every scope
    python utils/google_auth.py --check    only check the saved token

It reads credentials.json - an OAuth 2.0 client of type "Desktop app" from the Google
Cloud project where the APIs are enabled - opens the browser to sign in to Google, and
saves what Google gives back to token.json. Both sit in the project folder (or where
GOOGLE_OAUTH_CREDENTIALS_FILE / GOOGLE_OAUTH_TOKEN_FILE say - the older
GOOGLE_CLASSROOM_* names still work), both are gitignored, and token.json is as private
as a password: whoever has it can read your classes and your calendar, and add events.

THE SCOPES (SCOPES_BY_SERVICE). Classroom is read-only; Calendar is calendar.events:
read events and create them - the narrowest scope that can add an event. It cannot
change calendar settings or sharing. Jarvis writes an event only after the owner says
yes to it (the Approval Card in the web hub, or out loud).

ONE TOKEN, EVERY SCOPE. A token made before a scope was added (token.json from the
Classroom-only sign-in) lacks it; running this script again notices and signs in again
with all of them. The agents check the scopes a token holds before using it and say
which command to run. load_credentials refreshes an expired token with every scope it
holds, so one agent's refresh never narrows the token for another.

Getting credentials.json, once:
  1. https://console.cloud.google.com/ -> a project (new or existing).
  2. APIs & Services -> Library -> enable "Google Calendar API" (and "Google
     Classroom API" if you use Classroom).
  3. APIs & Services -> OAuth consent screen: External (or Internal on a Workspace
     domain); while the app is in Testing, add your own Google account under Test
     users. Data access: add the scopes below (or let the sign-in ask for them).
  4. APIs & Services -> Credentials -> Create credentials -> OAuth client ID ->
     Application type "Desktop app" -> Download JSON -> save it as credentials.json
     in the project folder.
  While the consent screen is in Testing, Google expires the sign-in after 7 days:
  run this script again then (or publish the app to Production).
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Iterable, Optional

ROOT = Path(__file__).resolve().parent.parent

SCOPES_BY_SERVICE: dict[str, list[str]] = {
    "classroom": [
        "https://www.googleapis.com/auth/classroom.courses.readonly",
        "https://www.googleapis.com/auth/classroom.coursework.me.readonly",
    ],
    "calendar": [
        "https://www.googleapis.com/auth/calendar.events",
    ],
}
# What the sign-in asks for: every service's scopes, once each, in order.
SCOPES: list[str] = list(dict.fromkeys(scope for scopes in SCOPES_BY_SERVICE.values() for scope in scopes))

SIGN_IN_COMMAND = "python utils/google_auth.py"
SETUP_STEPS = __doc__.split("Getting credentials.json, once:", 1)[1].strip()

_lock = threading.Lock()  # reading and rewriting token.json


class GoogleAuthError(Exception):
    """Why the saved sign-in cannot be used, worded for the language model to pass on."""


def _path(variables: Iterable[str], default: str) -> Path:
    """The first file an env variable names (relative to the project folder unless absolute), or the default."""
    for variable in variables:
        value = os.environ.get(variable, "").strip()
        if value:
            path = Path(value)
            return path if path.is_absolute() else ROOT / path
    return ROOT / default


def credentials_file() -> Path:
    return _path(("GOOGLE_OAUTH_CREDENTIALS_FILE", "GOOGLE_CLASSROOM_CREDENTIALS_FILE"), "credentials.json")


def token_file() -> Path:
    return _path(("GOOGLE_OAUTH_TOKEN_FILE", "GOOGLE_CLASSROOM_TOKEN_FILE"), "token.json")


def client_type(path: Path) -> str:
    """'installed' for a Desktop-app client, 'web' for a Web one, '' if the file is not an OAuth client."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    for kind in ("installed", "web"):
        if isinstance(data.get(kind), dict) and data[kind].get("client_id"):
            return kind
    return ""


def _read_token(path: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """The saved token as a dict, or None when there is none or it is unreadable."""
    path = path or token_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def granted_scopes(token: Optional[dict[str, Any]] = None) -> set[str]:
    """The scopes the saved token holds (its own "scopes" field), empty when there is no token."""
    data = token if token is not None else _read_token()
    scopes = data.get("scopes") if isinstance(data, dict) else None
    if isinstance(scopes, str):
        scopes = scopes.split()
    return {str(scope) for scope in scopes} if isinstance(scopes, list) else set()


def missing_scopes(required: Iterable[str] = SCOPES, token: Optional[dict[str, Any]] = None) -> list[str]:
    """Which of `required` the saved token lacks - all of them when there is no token."""
    granted = granted_scopes(token)
    return [scope for scope in required if scope not in granted]


def _write_token(path: Path, data: dict[str, Any]) -> None:
    """Writes token.json in one step (a temporary file, then a rename), so a reader never sees half of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data), encoding="utf-8")
    os.replace(temporary, path)


def load_credentials(required: Iterable[str], service: str, hint: str = "") -> Any:
    """
    The owner's Google credentials for `service` ("Google Calendar", ...), refreshed when
    expired. Raises GoogleAuthError, worded for the model, when there is no sign-in, it lacks
    a scope in `required`, or it cannot be renewed. The credentials keep EVERY scope the token
    holds (not just `required`), so the token written back after a refresh still serves the
    other services.
    """
    hint = hint or f"Ask the owner to run:  {SIGN_IN_COMMAND}"
    try:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError:
        raise GoogleAuthError("The Google client libraries are not installed. Ask the owner to run: pip install -r requirements.txt") from None

    path = token_file()
    with _lock:
        saved = _read_token(path)
        if saved is None:
            if path.is_file():
                raise GoogleAuthError(f"The saved Google sign-in (token.json) is unreadable. {hint}")
            raise GoogleAuthError(f"{service} is not connected yet. {hint}")
        missing = missing_scopes(required, saved)
        if missing:
            raise GoogleAuthError(
                f"The saved Google sign-in does not include {service} access yet (it was made before it was "
                f"added). {hint} - it signs in again and keeps the access it already had."
            )
        try:
            creds = Credentials.from_authorized_user_info(saved)  # with the token's own scopes, all of them
        except (ValueError, TypeError, KeyError, AttributeError):
            raise GoogleAuthError(f"The saved Google sign-in (token.json) is unreadable. {hint}") from None
        if creds.valid:
            return creds
        if not creds.refresh_token:
            raise GoogleAuthError(f"The Google sign-in has expired. {hint}")
        try:
            creds.refresh(Request())
        except RefreshError:
            raise GoogleAuthError(f"The Google sign-in was revoked or has expired. {hint}") from None
        except Exception:  # noqa: BLE001 - a network problem while refreshing
            raise GoogleAuthError("Could not reach Google to renew the sign-in. Check the internet connection and try again.") from None
        try:
            refreshed = json.loads(creds.to_json())
            refreshed["scopes"] = sorted(granted_scopes(saved))  # exactly what it held before
            _write_token(path, refreshed)
        except OSError:
            pass  # a read-only mount (Docker): the refreshed token still works for this process
        return creds


# ------------------------------------------------------------------- signing in


def sign_in(force: bool = False) -> int:
    source = credentials_file()
    if not source.is_file():
        print(f"No OAuth client file at {source}.\n\nCreate one like this:\n{SETUP_STEPS}")
        return 1
    kind = client_type(source)
    if kind == "web":
        print(
            f"{source.name} is a 'Web application' OAuth client. This sign-in needs a 'Desktop app' "
            "client (Credentials -> Create credentials -> OAuth client ID -> Desktop app)."
        )
        return 1
    if kind != "installed":
        print(f"{source.name} is not an OAuth client file (it should hold an \"installed\" section).\n\n{SETUP_STEPS}")
        return 1

    target = token_file()
    saved = _read_token(target)
    missing = missing_scopes(SCOPES, saved)
    if saved is not None and not missing and not force:
        print(f"The saved sign-in ({target.name}) already has every scope Jarvis uses. Checking it...")
        return check()
    if saved is not None and missing:
        print("The saved sign-in is missing access Jarvis now uses:")
        for scope in missing:
            print(f"  - {scope}")
        print("Signing in again for all of it (what it already had stays).")

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Missing package. Install the requirements first:  pip install -r requirements.txt")
        return 1

    # Google's consent screen lets a person untick a permission; oauthlib would then raise
    # "Scope has changed". Relaxed, the sign-in finishes and the check below says what is missing.
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    flow = InstalledAppFlow.from_client_secrets_file(str(source), SCOPES)
    print("Opening your browser to sign in to Google. If nothing opens, use the link printed below.")
    # port=0: any free local port, which Desktop-app clients allow without registering it.
    creds = flow.run_local_server(port=0, open_browser=True, prompt="consent")
    data = json.loads(creds.to_json())
    granted = getattr(creds, "granted_scopes", None)
    if granted:
        data["scopes"] = sorted(set(granted))  # what Google granted, not just what was asked
    with _lock:
        _write_token(target, data)
    print(f"Signed in. Saved {target} (keep it private - it is gitignored).")
    still = missing_scopes(SCOPES, data)
    if still:
        print("Google did not grant all of the access asked for (a box was unticked?):")
        for scope in still:
            print(f"  - {scope}")
        print(f"Run {SIGN_IN_COMMAND} --force and leave every box ticked.")
    return check()


def check() -> int:
    """Asks each service for something small with the saved token, the same way Jarvis will."""
    sys.path.insert(0, str(ROOT))
    from agents.calendar_agent import list_upcoming_events
    from agents.classroom_agent import get_courses

    ok = True
    calendar = json.loads(list_upcoming_events(1))
    if "error" in calendar:
        ok = False
        print(f"Google Calendar: not working yet - {calendar['error']}")
    else:
        print(f"Google Calendar works: {calendar['count']} event(s) in the next day.")
    classroom = json.loads(get_courses())
    if "error" in classroom:
        ok = False
        print(f"Google Classroom: not working yet - {classroom['error']}")
    else:
        print(f"Google Classroom works: {classroom['count']} active course(s).")
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    if "--check" in argv[1:]:
        return check()
    return sign_in(force="--force" in argv[1:])


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    sys.exit(main(sys.argv))
