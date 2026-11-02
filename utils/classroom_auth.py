"""
One-time Google sign-in for the Classroom agent (agents/classroom_agent.py).

    python utils/classroom_auth.py           sign in, save token.json, check it works
    python utils/classroom_auth.py --check   only check the saved token

It reads credentials.json - an OAuth 2.0 client of type "Desktop app" from the
Google Cloud project where the Classroom API is enabled - opens the browser to
sign in to Google, and saves what Google gives back to token.json. Both files
sit in the project folder (or wherever GOOGLE_CLASSROOM_CREDENTIALS_FILE and
GOOGLE_CLASSROOM_TOKEN_FILE say, relative to it), both are gitignored, and
token.json is as private as a password: whoever has it can read your classes.

READ-ONLY. The two scopes below can list courses and see course work, nothing
else: Jarvis cannot post, submit, grade or change anything in Classroom. Adding
a scope later means signing in again (run this script once more).

Getting credentials.json, once:
  1. https://console.cloud.google.com/ -> a project (new or existing).
  2. APIs & Services -> Library -> "Google Classroom API" -> Enable.
  3. APIs & Services -> OAuth consent screen: External (or Internal on a
     Workspace domain); while the app is in Testing, add your own Google
     account under Test users.
  4. APIs & Services -> Credentials -> Create credentials -> OAuth client ID
     -> Application type "Desktop app" -> Download JSON -> save it as
     credentials.json in the project folder.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Read-only, on purpose (see the module docstring). The Classroom agent asks for
# exactly these, so a token made here always fits it.
SCOPES = [
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.coursework.me.readonly",
]

SETUP_STEPS = __doc__.split("Getting credentials.json, once:", 1)[1].strip()


def _path(variable: str, default: str) -> Path:
    """A file the env names (relative to the project folder unless absolute), or the default."""
    value = os.environ.get(variable, "").strip()
    path = Path(value) if value else Path(default)
    return path if path.is_absolute() else ROOT / path


def credentials_file() -> Path:
    return _path("GOOGLE_CLASSROOM_CREDENTIALS_FILE", "credentials.json")


def token_file() -> Path:
    return _path("GOOGLE_CLASSROOM_TOKEN_FILE", "token.json")


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


def sign_in() -> int:
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

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Missing package. Install the requirements first:  pip install -r requirements.txt")
        return 1

    flow = InstalledAppFlow.from_client_secrets_file(str(source), SCOPES)
    print("Opening your browser to sign in to Google. If nothing opens, use the link printed below.")
    # port=0: any free local port, which Desktop-app clients allow without registering it.
    creds = flow.run_local_server(port=0, open_browser=True, prompt="consent")
    target = token_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(creds.to_json(), encoding="utf-8")
    print(f"Signed in. Saved {target} (keep it private - it is gitignored).")
    return check()


def check() -> int:
    """Asks Classroom for the active courses with the saved token, the same way Jarvis will."""
    sys.path.insert(0, str(ROOT))
    from agents.classroom_agent import get_courses

    result = json.loads(get_courses())
    if "error" in result:
        print(f"Not working yet: {result['error']}")
        return 1
    print(f"Google Classroom works: {result['count']} active course(s) visible to Jarvis.")
    return 0


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    return check() if "--check" in argv[1:] else sign_in()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
