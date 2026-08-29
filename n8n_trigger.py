"""
Module 6: Automation Bridge (n8n webhooks)

Turns a spoken command into an HTTP POST against an n8n Production
Webhook, and turns the outcome back into a single sentence that
voice_engine.py can read aloud.

    "log a subscription, Netflix, 15 dollars, entertainment"
        -> POST $N8N_SUBTRACK_WEBHOOK
           {"item_name": "Netflix", "amount": 15, "category": "entertainment"}
        -> "Subtrack log triggered successfully."

    ...or, when n8n is unreachable:
        -> "I couldn't reach the automation server, so subtrack log didn't
            run."

DESIGN NOTES
------------
* No URLs live in this file. Every webhook is read from the environment
  (see .env.example), because a Production Webhook URL is a bearer
  credential - anyone holding it can run the workflow.

* WORKFLOW_ENV_VARS is the registry: it maps the spoken workflow name
  onto the environment variable holding its URL. Adding a workflow your
  voice can name is one line there plus one line in .env.

* Workflows can also be added without touching this file at all: any
  environment variable named N8N_WEBHOOK_<NAME> registers a workflow
  called "<name>", underscores read as spaces. WORKFLOW_ALIASES covers
  spoken variants that don't map cleanly onto either name.

* Nothing is retried. A webhook POST is not idempotent - an n8n workflow
  that charges a card or sends mail must not run twice because the read
  timed out. A timeout is reported honestly as "I couldn't confirm it",
  not as a failure, because the workflow may well have started.

* Every return value is a finished English sentence. Callers speak it
  verbatim; they never have to interpret a status code. Return strings
  deliberately never contain the URL, so a webhook secret can't end up
  in the console log or coming out of the speakers.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

from os_executor import match_name

# requests is optional at import time: a machine with no automation
# configured should still boot Jarvis. Absence is reported when a
# workflow is actually triggered, not at startup.
try:
    import requests
except ImportError:  # pragma: no cover - environment-dependent
    requests = None  # type: ignore[assignment]

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - environment-dependent
    pass


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

# THE REGISTRY: spoken workflow name -> environment variable holding its
# Production Webhook URL. This is the file's main configuration surface;
# everything else here is plumbing.
#
# The name on the left is what the user says and what Jarvis says back,
# so keep it a natural noun phrase - it appears verbatim in the
# confirmation prompt ("Do you authorize running the subtrack log
# workflow?") and in the spoken result.
WORKFLOW_ENV_VARS: dict[str, str] = {
    # The primary automation: Jarvis extracts the fields out of the
    # spoken request and posts them here for n8n to write into the
    # Subtrack database.
    "subtrack log": "N8N_SUBTRACK_WEBHOOK",
    # Catch-all for "run automation" when no workflow is named.
    "automation": "N8N_WEBHOOK_URL",
}

# Prefix for the open-ended form: N8N_WEBHOOK_<NAME> registers "<name>"
# without needing an entry above.
_ENV_PREFIX = "N8N_WEBHOOK_"

# Variables that share the prefix but are settings, not workflows.
_ENV_RESERVED = {"N8N_WEBHOOK_URL", "N8N_WEBHOOK_TOKEN", "N8N_WEBHOOK_TIMEOUT"}

# Run when the user names no workflow at all ("run automation"). It is
# registered under an ordinary name rather than special-cased, so it
# goes through the same lookup, confirmation, and error handling as
# everything else - there is no second, simpler code path to keep in
# sync. If it has no URL but exactly one other workflow does, that one
# is used instead; with a single webhook configured there is nothing
# else "run automation" could reasonably mean.
DEFAULT_WORKFLOW = "automation"

# (connect timeout, read timeout) in seconds - always passed explicitly,
# because requests has no default timeout at all and a hung webhook
# would otherwise block the assistant indefinitely.
#
# Five seconds each is a deliberate ceiling: this is a voice loop, and
# silence longer than that reads as a crash to whoever is standing
# there. Workflows that do real work before responding should answer the
# webhook immediately and run asynchronously ("Respond to Webhook" node
# first) rather than having this number raised.
_CONNECT_TIMEOUT = 5.0
_DEFAULT_READ_TIMEOUT = 5.0

# Minimum similarity for matching a spoken workflow name.
_FUZZY_CUTOFF = 0.70

# Spoken forms that should map onto a registered workflow name. Offline
# STT mangles product nouns, so give it help here - same pattern as
# PROJECT_ALIASES in os_executor.py.
# "subtrack" is the risky word here: Vosk has no domain vocabulary and
# reliably hears it as "sub track", "subtract", or "sub tract".
WORKFLOW_ALIASES: dict[str, str] = {
    "subtrack": "subtrack log",
    "sub track": "subtrack log",
    "sub track log": "subtrack log",
    "subtract log": "subtrack log",
    "sub tract log": "subtrack log",
    "log to subtrack": "subtrack log",
    "log in subtrack": "subtrack log",
    "subtrack logging": "subtrack log",
    "log expense": "subtrack log",
    "log subscription": "subtrack log",
    "default": DEFAULT_WORKFLOW,
    "the automation": DEFAULT_WORKFLOW,
    "automations": DEFAULT_WORKFLOW,
}


def _env_name_to_workflow(env_key: str) -> str:
    """N8N_WEBHOOK_DATA_SYNC -> 'data sync'."""
    return env_key[len(_ENV_PREFIX) :].replace("_", " ").strip().lower()


def _discover_webhooks() -> dict[str, str]:
    """
    Builds the intent-name -> webhook-URL map from the environment.

    Read on every call rather than cached at import, so editing .env and
    restarting is the only step needed to add a workflow - and so tests
    can monkeypatch os.environ without reloading the module.
    """
    # Registered names first, so they appear even when unconfigured -
    # "the subtrack log has no URL" is a far better answer than "I don't
    # know a workflow called that".
    webhooks: dict[str, str] = {
        name: os.getenv(env_var, "").strip()
        for name, env_var in WORKFLOW_ENV_VARS.items()
    }

    for key, value in os.environ.items():
        if not key.startswith(_ENV_PREFIX) or key in _ENV_RESERVED:
            continue
        name = _env_name_to_workflow(key)
        if name:
            webhooks[name] = value.strip()

    return webhooks


def configured_workflows() -> list[str]:
    """Names of workflows that actually have a URL behind them."""
    return sorted(name for name, url in _discover_webhooks().items() if url)


def is_configured() -> bool:
    """True if at least one webhook URL is set and requests is installed."""
    return requests is not None and bool(configured_workflows())


def _read_timeout() -> float:
    """
    Read timeout in seconds (default 5), overridable via
    N8N_WEBHOOK_TIMEOUT. A malformed value falls back to the default
    rather than raising - a typo in .env must not break voice commands.
    """
    raw = os.getenv("N8N_WEBHOOK_TIMEOUT", "").strip()
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_READ_TIMEOUT
    return value if value > 0 else _DEFAULT_READ_TIMEOUT


def _auth_headers() -> dict[str, str]:
    """
    Optional shared-secret header. n8n's "Header Auth" credential on a
    Webhook node checks this; without it, anyone who learns the URL can
    fire the workflow.
    """
    headers = {"Content-Type": "application/json", "User-Agent": "Jarvis/1.0"}
    token = os.getenv("N8N_WEBHOOK_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


# ---------------------------------------------------------------------
# Intent resolution
# ---------------------------------------------------------------------


def resolve_workflow(spoken_name: str) -> Optional[str]:
    """
    Resolves a spoken workflow name to a registered one, tolerating the
    mishearings offline STT produces. Returns None if nothing matched
    closely enough - an ambiguous match must never fire a workflow.

    An empty name means the user said "run automation" without naming
    anything, which resolves to N8N_WEBHOOK_URL when that is configured.
    """
    webhooks = _discover_webhooks()

    if not spoken_name or not spoken_name.strip():
        if webhooks.get(DEFAULT_WORKFLOW):
            return DEFAULT_WORKFLOW
        # Exactly one webhook configured - "run automation" can only
        # mean that one, so don't make the user name it.
        configured = [name for name, url in webhooks.items() if url]
        return configured[0] if len(configured) == 1 else None

    pool: dict[str, str] = {name: name for name in webhooks}
    pool.update(WORKFLOW_ALIASES)
    return match_name(spoken_name, pool, cutoff=_FUZZY_CUTOFF)


# ---------------------------------------------------------------------
# Response handling
# ---------------------------------------------------------------------


def _summarise_body(response: "requests.Response") -> Optional[str]:
    """
    Pulls a short, speakable message out of an n8n response.

    A "Respond to Webhook" node commonly returns something like
    {"message": "Synced 42 records"}; that is far more useful to hear
    than "triggered successfully", so it wins when present. Anything
    long, nested, or binary is ignored rather than read aloud.
    """
    try:
        body: Any = response.json()
    except ValueError:
        text = (response.text or "").strip()
        return text if 0 < len(text) <= 200 else None

    if isinstance(body, list) and body:
        body = body[0]

    if isinstance(body, dict):
        for key in ("message", "status", "result", "summary", "text"):
            value = body.get(key)
            if isinstance(value, (str, int, float)) and str(value).strip():
                spoken = str(value).strip()
                return spoken if len(spoken) <= 200 else None

    if isinstance(body, str) and 0 < len(body.strip()) <= 200:
        return body.strip()

    return None


# ---------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------


def _log_trigger(workflow: str, status: str, details: str) -> None:
    """
    Records one fired webhook in the action_logs table, best-effort.

    db_connector is imported here rather than at module scope for two
    reasons: this module works perfectly well with no database behind
    it, and importing it lazily keeps the startup cost of that module -
    it prepares its tables on import - out of the path of anyone who
    only wants to trigger a workflow.

    Every failure is swallowed. An audit trail is never a reason for a
    workflow not to run, or for its result not to be spoken.
    """
    try:
        import db_connector

        db_connector.log_action(f"n8n.{workflow}", status, details)
    except Exception as exc:  # noqa: BLE001 - must never break a trigger
        print(f"[Jarvis] Could not record the n8n action log: {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------
# Reading back from Subtrack
# ---------------------------------------------------------------------

# The published (non-test) Production Webhook. n8n exposes two URLs per
# webhook - /webhook-test/... only accepts one call while the editor is
# open with "Listen for test event" clicked, which is why a workflow
# that works in the browser can fail from here. This is the published
# one; do not put "-test" back.
SUBTRACK_QUERY_URL = "http://127.0.0.1:5678/webhook/subtrack-log"

# The webhook answers with the last node's output: the 5 most recent
# rows from Supabase. That goes to the model verbatim, so it is capped -
# a runaway workflow returning the whole table would otherwise push the
# conversation out of its context window.
SUBTRACK_QUERY_MAX_CHARS = 6000


def subtrack_query_url() -> str:
    """The URL the read-back POST goes to. Overridable for a remote n8n."""
    return os.getenv("N8N_SUBTRACK_QUERY_URL", "").strip() or SUBTRACK_QUERY_URL


def fetch_subtrack_records(query_string: str = "") -> str:
    """
    POSTs to the Subtrack webhook and returns its rows as JSON text.

    Unlike trigger_workflow(), the return value here is NOT a spoken
    sentence: it is data for the model to read and summarise, so it is
    handed back as compact JSON. Failures are still sentences, prefixed
    so the model can tell "here are the records" from "I could not get
    them" without parsing anything.

    `query_string` is what the user actually asked for. It is passed
    along in the body so the workflow can use it if it wants to; the
    current workflow ignores it and always returns the latest 5 rows.
    """
    if requests is None:
        return (
            "ERROR: the requests package is not installed, so no query ran. "
            "Tell the user plainly; do not invent any records."
        )

    url = subtrack_query_url()
    body = {"action": "fetch_recent", "query": query_string or "recent records"}
    print(f"[Jarvis] Fetching recent Subtrack records from {url}...")

    try:
        response = requests.post(
            url,
            json=body,
            headers=_auth_headers(),
            timeout=(_CONNECT_TIMEOUT, _read_timeout()),
        )
    except requests.exceptions.ConnectionError:
        _log_trigger("subtrack query", "error", "connection error")
        return (
            "ERROR: I couldn't reach the automation server, so no records were "
            "read. Tell the user to check that n8n is running. Do not invent "
            "any records."
        )
    except requests.exceptions.Timeout:
        _log_trigger("subtrack query", "error", "timeout")
        return (
            "ERROR: the automation server didn't answer in time, so no records "
            "were read. Tell the user that plainly; do not invent any records."
        )
    except requests.exceptions.RequestException as exc:
        print(f"[Jarvis] Subtrack query error: {type(exc).__name__}: {exc}")
        _log_trigger("subtrack query", "error", f"{type(exc).__name__}: {exc}")
        return (
            "ERROR: something went wrong talking to the automation server, so "
            "no records were read. Do not invent any."
        )

    status = response.status_code
    _log_trigger("subtrack query", "ok" if status < 400 else "error", f"HTTP {status}")

    if status == 404:
        return (
            "ERROR: the automation server doesn't recognise the Subtrack "
            "webhook. The workflow may be switched off, or set to respond on "
            "the test URL only. Tell the user; do not invent any records."
        )
    if status >= 400:
        return (
            f"ERROR: the automation server rejected the request with status "
            f"{status}, so no records were read. Do not invent any."
        )

    try:
        payload = response.json()
    except ValueError:
        # Not JSON. A "Respond to Webhook" node set to plain text does
        # this; the text is still worth handing over.
        text = (response.text or "").strip()
        if not text:
            return (
                "EMPTY: the workflow returned nothing at all. Tell the user "
                "there are no records to report; do not invent any."
            )
        return f"RECORDS (plain text, not JSON): {text[:SUBTRACK_QUERY_MAX_CHARS]}"

    if payload in (None, [], {}):
        return (
            "EMPTY: the workflow ran but returned no records. Say there is "
            "nothing recent to report; do not invent any."
        )

    # Compact separators: this is read by a model, not a person, and the
    # whitespace in a pretty-printed dump is pure token cost.
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(encoded) > SUBTRACK_QUERY_MAX_CHARS:
        encoded = encoded[:SUBTRACK_QUERY_MAX_CHARS]
        print(f"[Jarvis] Subtrack response truncated to {SUBTRACK_QUERY_MAX_CHARS} chars.")
        return (
            "RECORDS (JSON, truncated - summarise what is here and say the "
            f"list was cut short): {encoded}"
        )

    count = len(payload) if isinstance(payload, list) else 1
    print(f"[Jarvis] Subtrack returned {count} record(s), {len(encoded)} chars.")
    return f"RECORDS (JSON): {encoded}"


def trigger_workflow(
    workflow_name: str,
    payload: Optional[dict[str, Any]] = None,
) -> str:
    """
    Fires the webhook for `workflow_name` and returns one spoken sentence.

    `workflow_name` may be the raw transcript ("data synch"); it is
    resolved against the registry first. Never raises: every failure
    mode - missing package, unknown name, unconfigured URL, timeout,
    refused connection, HTTP error - comes back as a sentence, because
    the caller's only output device is a speaker.
    """
    if requests is None:
        return (
            "I can't reach n8n because the requests package isn't installed. "
            "Run pip install -r requirements.txt."
        )

    resolved = resolve_workflow(workflow_name)
    if resolved is None:
        known = configured_workflows()
        if not known:
            return (
                "I don't have any workflows configured yet. "
                "Add an n8n webhook address to the environment file."
            )
        # No name given and no default configured - name the options
        # rather than reading back an empty string.
        if not workflow_name or not workflow_name.strip():
            return f"Which workflow? I can run: {', '.join(known)}."
        return (
            f"I don't know a workflow called {workflow_name}. "
            f"I can run: {', '.join(known)}."
        )

    url = _discover_webhooks().get(resolved, "")
    if not url:
        return (
            f"The {resolved} workflow has no webhook URL configured. "
            "Add it to the environment file."
        )

    body = {
        "source": "jarvis",
        "workflow": resolved,
        "requested_at": datetime.now(timezone.utc).isoformat(),
    }
    if payload:
        body.update(payload)

    print(f"[Jarvis] Triggering n8n workflow '{resolved}'...")

    try:
        response = requests.post(
            url,
            json=body,
            headers=_auth_headers(),
            timeout=(_CONNECT_TIMEOUT, _read_timeout()),
        )
    except requests.exceptions.ConnectTimeout:
        # Every sentence below is spoken aloud, so it is phrased for a
        # person standing in the room, not for a log file: what did not
        # happen, and the one thing they can go and check.
        _log_trigger(resolved, "error", "connect timeout")
        return (
            f"The automation server didn't answer in time, so {resolved} didn't run. "
            "Please check whether n8n is running."
        )
    except requests.exceptions.ReadTimeout:
        # Deliberately not reported as a failure: the workflow was
        # accepted and may still be running. Saying "it failed" here
        # would invite the user to fire it a second time.
        _log_trigger(resolved, "unconfirmed", "read timeout - n8n may still be running it")
        return (
            f"I started the {resolved} workflow, but n8n took too long to confirm it. "
            "Check the n8n executions list."
        )
    except requests.exceptions.ConnectionError:
        _log_trigger(resolved, "error", "connection error")
        return (
            f"I couldn't reach the automation server, so {resolved} didn't run. "
            "Please check if n8n is running."
        )
    except requests.exceptions.RequestException as exc:
        # The exception detail goes to the console and the log, never
        # into the returned sentence: "MaxRetryError, caused by
        # NewConnectionError" is not something anyone wants read to them.
        print(f"[Jarvis] n8n request error: {type(exc).__name__}: {exc}")
        _log_trigger(resolved, "error", f"{type(exc).__name__}: {exc}")
        return (
            f"Something went wrong talking to the automation server, "
            f"so {resolved} didn't run."
        )

    status = response.status_code

    # One line per fired webhook. Logged here rather than in each
    # branch below because the request has already left the machine -
    # what follows only decides how to phrase what n8n did with it.
    _log_trigger(resolved, "ok" if status < 400 else "error", f"HTTP {status}")

    if status == 404:
        return (
            f"The automation server doesn't recognise the {resolved} webhook. "
            "The workflow may be switched off, or pointed at the test URL."
        )
    if status in (401, 403):
        return (
            f"The automation server refused the {resolved} request. "
            "The webhook token looks wrong."
        )
    if status >= 500:
        return (
            f"The automation server hit an error of its own, "
            f"so {resolved} may not have completed."
        )
    if status >= 400:
        return f"The automation server rejected the {resolved} request."

    message = _summarise_body(response)
    if message:
        return f"{resolved.capitalize()}: {message}."

    # No "workflow" suffix: the registered names are already noun
    # phrases ("subtrack log"), and appending it reads
    # as a stutter when spoken aloud.
    return f"{resolved.capitalize()} triggered successfully."


if __name__ == "__main__":
    print("n8n workflow registry:")
    for name, url in sorted(_discover_webhooks().items()):
        env_var = WORKFLOW_ENV_VARS.get(name, f"{_ENV_PREFIX}{name.upper().replace(' ', '_')}")
        state = "configured" if url else "NOT configured"
        print(f"  {name!r:34} <- {env_var:28} {state}")

    print("\nName resolution check:")
    samples = (
        "subtrack log",
        "log expense",
        "subtrack",
        "sub track log",              # Vosk mishearing
        "subtract log",               # Vosk mishearing
        "",                           # bare "run automation"
        "weather",                    # must not match
    )
    for phrase in samples:
        print(f"  {phrase!r:34} -> {resolve_workflow(phrase)!r}")
