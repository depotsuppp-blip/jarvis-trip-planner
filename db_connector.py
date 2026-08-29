"""
Module 7: Read-Only Database Connector (PostgreSQL)

Answers spoken questions about the DEMP Postgres database out loud:

    "check demp database status"        -> check_demp_status()
        -> SELECT count(*) FROM energy_logs
        -> "The DEMP database holds 1,204 energy log records."
        -> or, with nothing listening on the other end:
           "I am currently unable to connect to the DEMP database."
        -> or, before the schema exists:
           "I reached the DEMP database, but the table behind the demp
            status query doesn't exist yet."

    "check database"                    -> check_system_status()
        -> SELECT 1
        -> "The DEMP database is online and responding."

Nothing here raises. A database that is offline, half-configured, or
not built yet is the expected state during development, and it must
produce a spoken sentence rather than a traceback - the assistant has
no other way to tell the user what went wrong.

READ-ONLY BY CONSTRUCTION
-------------------------
Jarvis is voice-driven, and speech-to-text misheard a word is a normal
Tuesday. A misheard word must never be able to write to a database, so
this module refuses to be a general SQL runner. Four independent layers
enforce that, any one of which would be enough:

    1. No free-form SQL API. Callers pass a *query name*; the SQL text
       lives only in SAFE_QUERIES below and never comes from a caller,
       let alone from a transcript.
    2. Every registered statement is validated before it runs - it must
       start with SELECT or WITH, contain no semicolon (so nothing can
       be stapled onto the end), and contain no writing keyword.
    3. The session is opened read-only, so the server itself rejects any
       write that somehow got past 1 and 2.
    4. A statement timeout caps how long any query can hold the
       connection, so a heavy query can't hang the assistant.

The right long-term control is still a Postgres role with SELECT and
nothing else - see .env.example. These layers are defence in depth, not
a substitute for that.

The one exception is housekeeping Jarvis does on its own behalf, at the
bottom of this file: init_system_tables creates chat_history and
action_logs if they are missing, and log_action appends to the latter.
Every statement involved is a literal in this file and every value is a
bound parameter, so the property that matters - a transcript can never
be executed as SQL - is unchanged. Nothing on the query path writes.

Requires:
    pip install psycopg2-binary python-dotenv
"""

from __future__ import annotations

import os
import re
from typing import Any, Optional, Sequence

from os_executor import match_name

# psycopg2 is optional at import time - a machine with no database
# configured should still boot Jarvis. Absence is reported when a query
# is actually run, not at startup.
try:
    import psycopg2
    from psycopg2 import errors as pg_errors
except ImportError:  # pragma: no cover - environment-dependent
    psycopg2 = None  # type: ignore[assignment]
    pg_errors = None  # type: ignore[assignment]

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - environment-dependent
    pass


# ---------------------------------------------------------------------
# Query registry
# ---------------------------------------------------------------------
#
# The complete set of statements Jarvis is allowed to run. Add a query
# by adding an entry here - there is deliberately no way to run SQL that
# isn't in this dictionary.
#
#   sql     : the statement. SELECT or WITH only, no trailing semicolon.
#   spoken  : sentence template. {value} is the single scalar result,
#             {rows} the row count. Optional - a generic summary is
#             produced when it is missing.
#   aliases : extra spoken forms that should resolve to this entry.
#
# Table names are hardcoded here on purpose. Reading one from the
# environment would mean building SQL from a string that arrives at
# runtime, which is exactly the shape this module exists to avoid - so
# `energy_logs` and `farmers` are placeholders to EDIT, not to configure.
#
# "health" is the fallback for a bare "check database" because SELECT 1
# is the only statement guaranteed to work against any schema, including
# one where DEMP's tables don't exist yet.

SAFE_QUERIES: dict[str, dict[str, Any]] = {
    "demp status": {
        "sql": "SELECT count(*) FROM energy_logs",
        "spoken": "The DEMP database holds {value} energy log records.",
        "aliases": (
            "demp",
            "demp database status",
            "energy logs",
            "energy log count",
            "damp status",  # Vosk mishearing of "DEMP"
            "temp status",  # Vosk mishearing of "DEMP"
        ),
    },
    "health": {
        "sql": "SELECT 1",
        "spoken": "The DEMP database is online and responding.",
        "aliases": ("status", "connection", "are you connected", "ping"),
    },
    "recent energy logs": {
        "sql": (
            "SELECT count(*) FROM energy_logs "
            "WHERE created_at >= now() - interval '24 hours'"
        ),
        "spoken": "There are {value} energy log records from the last day.",
        "aliases": ("energy logs today", "recent logs", "logs today"),
    },
    "farmer count": {
        "sql": "SELECT count(*) FROM farmers",
        "spoken": "There are {value} registered farmers.",
        "aliases": ("farmers", "how many farmers", "registered farmers"),
    },
    "table count": {
        "sql": (
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'public'"
        ),
        "spoken": "The public schema has {value} tables.",
        "aliases": ("tables", "how many tables", "schema size"),
    },
    "database size": {
        "sql": "SELECT pg_size_pretty(pg_database_size(current_database()))",
        "spoken": "The database is {value} on disk.",
        "aliases": ("size", "how big is the database", "disk usage"),
    },
}

# Used when the user just says "check database" with no query named.
_DEFAULT_QUERY = "health"

# What Jarvis calls this database out loud. Overridable so the spoken
# error messages stay right if the platform is ever renamed.
DB_LABEL = os.getenv("DB_LABEL", "DEMP").strip() or "DEMP"

# Minimum similarity for matching a spoken query name.
_FUZZY_CUTOFF = 0.70

# Hard caps. A spoken answer can only carry a sentence or two, so there
# is no point fetching - or waiting for - more than this.
_MAX_ROWS = 5
_STATEMENT_TIMEOUT_MS = 8000
_CONNECT_TIMEOUT_S = 5

# Statements must begin with one of these.
_ALLOWED_PREFIXES = ("select", "with")

# Any of these appearing as a word disqualifies a statement outright.
_FORBIDDEN_KEYWORDS = (
    "insert", "update", "delete", "drop", "alter", "create", "truncate",
    "grant", "revoke", "copy", "merge", "call", "do", "vacuum", "analyze",
    "reindex", "cluster", "refresh", "comment", "lock", "listen", "notify",
    "prepare", "execute", "commit", "rollback", "savepoint", "begin", "set",
    "into", "pg_read_file", "pg_sleep", "dblink",
)


# ---------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------


def _validate_sql(sql: str) -> Optional[str]:
    """
    Returns None if `sql` is a safe single read statement, or a short
    reason why it isn't. Runs on every query before execution, so a
    careless edit to SAFE_QUERIES fails loudly instead of quietly
    writing to the database.
    """
    stripped = sql.strip()
    if not stripped:
        return "the statement is empty"

    lowered = stripped.lower()

    if not lowered.startswith(_ALLOWED_PREFIXES):
        return "only SELECT statements are allowed"

    # Blocks "SELECT 1; DROP TABLE users" outright: one statement only.
    if ";" in stripped:
        return "the statement contains a semicolon"

    if "--" in stripped or "/*" in stripped:
        return "the statement contains a comment"

    for keyword in _FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{re.escape(keyword)}\b", lowered):
            return f"the statement contains the keyword '{keyword}'"

    return None


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------


def _credentials() -> dict[str, Any]:
    """
    Connection settings, read from the environment on every call so that
    editing .env and restarting is all it takes to point Jarvis at a
    different database.

    DB_PASS is the documented name; DB_PASSWORD is accepted as a synonym
    because it is the more common spelling elsewhere and getting silently
    ignored would look exactly like a wrong password.
    """
    return {
        "host": os.getenv("DB_HOST", "localhost").strip(),
        "port": os.getenv("DB_PORT", "5432").strip() or "5432",
        "dbname": os.getenv("DB_NAME", "").strip(),
        "user": os.getenv("DB_USER", "").strip(),
        "password": os.getenv("DB_PASS") or os.getenv("DB_PASSWORD", ""),
        "sslmode": os.getenv("DB_SSLMODE", "prefer").strip() or "prefer",
    }


def is_configured() -> bool:
    """True if psycopg2 is installed and a database and user are set."""
    if psycopg2 is None:
        return False
    creds = _credentials()
    return bool(creds["dbname"] and creds["user"])


def available_queries() -> list[str]:
    """Names of every query Jarvis is allowed to run."""
    return sorted(SAFE_QUERIES)


def resolve_query(spoken_name: str) -> Optional[str]:
    """
    Resolves a spoken query name to a registry key, tolerating STT
    mishearings. An empty name resolves to the default health check, so
    a bare "check database" always has somewhere to go.
    """
    if not spoken_name or not spoken_name.strip():
        return _DEFAULT_QUERY

    pool: dict[str, str] = {name: name for name in SAFE_QUERIES}
    for name, spec in SAFE_QUERIES.items():
        for alias in spec.get("aliases", ()):
            pool[alias] = name

    return match_name(spoken_name, pool, cutoff=_FUZZY_CUTOFF)


# ---------------------------------------------------------------------
# Result formatting
# ---------------------------------------------------------------------


def _speak_value(value: Any) -> str:
    """Renders one cell as something a TTS engine reads naturally."""
    if value is None:
        return "nothing"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    return str(value)


def _format_result(
    query_name: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
) -> str:
    """
    Turns a result set into one spoken sentence.

    The common case by far is a single scalar (a count), which is why
    the registry's `spoken` template exists. Wider or taller results get
    a generic summary rather than being read out cell by cell - nobody
    wants five columns recited at them.
    """
    template = SAFE_QUERIES.get(query_name, {}).get("spoken")

    if not rows:
        return f"The {query_name} query returned no rows."

    # Single scalar - the shape almost every registered query has.
    if len(rows) == 1 and len(rows[0]) == 1:
        value = _speak_value(rows[0][0])
        if template:
            return template.format(value=value, rows=1)
        return f"The {query_name} query returned {value}."

    # Single row, several columns - read them as "column is value".
    if len(rows) == 1:
        parts = [
            f"{column.replace('_', ' ')} is {_speak_value(cell)}"
            for column, cell in zip(columns, rows[0])
        ]
        return f"For {query_name}: " + ", ".join(parts) + "."

    # Several rows - report the shape, not the contents. The template is
    # only reused if it actually talks about row counts; a scalar
    # template like "There are {value} users" would otherwise announce
    # the first cell as though it were the answer.
    if template and "{rows}" in template:
        return template.format(value=_speak_value(rows[0][0]), rows=len(rows))
    return (
        f"The {query_name} query returned {len(rows)} rows. "
        f"The first is {_speak_value(rows[0][0])}."
    )


# ---------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------


def run_query(query_name: str = "") -> str:
    """
    Runs a registered read-only query and returns one spoken sentence.

    `query_name` may be the raw transcript ("how many users"); it is
    resolved against the registry first, and an empty string runs the
    default health check. Never raises: missing driver, missing
    credentials, unreachable server, missing table, and timeouts all
    come back as sentences, because the caller's only output device is
    a speaker.
    """
    if psycopg2 is None:
        return (
            "I can't reach the database because psycopg2 isn't installed. "
            "Run pip install -r requirements.txt."
        )

    if not is_configured():
        return (
            "My database credentials aren't configured. "
            "Set DB_NAME and DB_USER in the environment file."
        )

    resolved = resolve_query(query_name)
    if resolved is None:
        return (
            f"I don't have a query called {query_name}. "
            f"I can check: {', '.join(available_queries())}."
        )

    sql = SAFE_QUERIES[resolved]["sql"]
    problem = _validate_sql(sql)
    if problem:
        # A registry entry failed the safety check - a bug in the code,
        # not in what the user said. Refuse rather than run it.
        print(f"[Jarvis] BLOCKED unsafe query '{resolved}': {problem}.")
        return f"I refused to run the {resolved} query because {problem}."

    creds = _credentials()
    connection = None

    print(f"[Jarvis] Querying the database: {resolved}...")

    try:
        connection = psycopg2.connect(
            connect_timeout=_CONNECT_TIMEOUT_S,
            # Enforced by the server, not by this process: even a bug
            # here cannot turn the session into a writable one.
            options=(
                f"-c statement_timeout={_STATEMENT_TIMEOUT_MS} "
                "-c default_transaction_read_only=on"
            ),
            application_name="jarvis",
            **creds,
        )
        connection.set_session(readonly=True, autocommit=True)

        with connection.cursor() as cursor:
            cursor.execute(sql)
            columns = [desc[0] for desc in (cursor.description or [])]
            rows = cursor.fetchmany(_MAX_ROWS)

        spoken = _format_result(resolved, columns, rows)
        # Opens its own short-lived connection, because this one is
        # read-only at the server and could not carry an INSERT.
        log_action(f"db.{resolved}", "ok", spoken)
        return spoken

    except psycopg2.OperationalError as exc:
        # The expected failure while the database is still being stood
        # up: refused connections, bad credentials, unreachable host, or
        # the server cancelling a statement that blew the timeout. The
        # real diagnostic goes to the console; the speaker gets a
        # sentence, because "OperationalError" is not a thing to say to
        # someone standing in a room.
        detail = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
        print(f"[Jarvis] Database connection error: {detail}")
        if "timeout" in detail.lower() or "canceling" in detail.lower():
            return f"The {resolved} query took too long and was cancelled."
        if "password" in detail.lower() or "authentication" in detail.lower():
            return (
                f"The {DB_LABEL} database refused my credentials. "
                "Check the database user and password."
            )
        return (
            f"I am currently unable to connect to the {DB_LABEL} database. "
            "Check that PostgreSQL is running."
        )

    except Exception as exc:  # noqa: BLE001 - must return speech, never raise
        if pg_errors is not None and isinstance(exc, pg_errors.UndefinedTable):
            # The expected answer until the DEMP schema is built: Postgres
            # is up and reachable, the table simply isn't there yet. Worth
            # saying both halves - it's good news and bad news at once.
            log_action(f"db.{resolved}", "error", "table does not exist")
            return (
                f"I reached the {DB_LABEL} database, but the table behind "
                f"the {resolved} query doesn't exist yet."
            )
        if pg_errors is not None and isinstance(exc, pg_errors.InsufficientPrivilege):
            log_action(f"db.{resolved}", "error", "insufficient privilege")
            return f"My database user isn't allowed to read that. The {resolved} query was refused."
        print(f"[Jarvis] Database error: {type(exc).__name__}: {exc}")
        # Reached on a live connection, unlike the OperationalError
        # branch above - there the database is the thing that is down,
        # and logging the failure would stall on a second timeout.
        log_action(
            f"db.{resolved}", "error", f"{type(exc).__name__}: {_error_detail(exc)}"
        )
        return f"The {resolved} query failed. See the console for details."

    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:  # noqa: BLE001 - closing must never mask the result
                pass


def check_demp_status() -> str:
    """
    The spoken answer to "check demp database status".

    Runs SELECT count(*) FROM energy_logs and reports the number aloud.
    Every failure the half-built stack can produce - driver missing,
    credentials unset, Postgres down, `energy_logs` not created yet -
    comes back as a sentence rather than an exception, because the
    caller's only output device is a speaker.
    """
    return run_query("demp status")


def check_system_status() -> str:
    """
    Connectivity only: a bare SELECT 1, the one statement guaranteed to
    work against any schema. This is what a generic "check database"
    runs, and it is the right check while DEMP's tables are still being
    built - it answers "can I reach Postgres at all" without depending
    on any table existing.
    """
    return run_query(_DEFAULT_QUERY)


# Earlier name for the same thing, kept so existing callers don't break.
check_connection = check_system_status


# ---------------------------------------------------------------------
# System tables: bootstrap and action logging
# ---------------------------------------------------------------------
#
# The only writing this module does, and the exception that has to earn
# its place against the read-only rule at the top of the file.
#
# That rule is really "a misheard word must never reach the database as
# SQL", and it still holds here, for reasons that do not depend on
# anyone being careful later:
#
#   1. Every statement below is a literal in this file. None of it is
#      assembled from an argument, a transcript, or the environment.
#   2. log_action binds its arguments as query parameters, so a
#      transcript arrives as data and can never be read as SQL.
#   3. The writable session is opened only by the two functions here and
#      closed immediately. run_query is untouched and still opens a
#      read-only session for everything the voice path asks.
#   4. Neither statement can destroy anything: CREATE TABLE IF NOT
#      EXISTS is a no-op against an existing table, and INSERT appends.
#
# These statements are not put through _validate_sql - that check exists
# to keep the *query* registry read-only, and would reject these two by
# design. They are reviewed here instead, and they are the whole of the
# write surface.

SYSTEM_TABLES: dict[str, str] = {
    "chat_history": """
        CREATE TABLE IF NOT EXISTS chat_history (
            id         SERIAL PRIMARY KEY,
            role       VARCHAR(50),
            content    TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )
    """,
    "action_logs": """
        CREATE TABLE IF NOT EXISTS action_logs (
            id          SERIAL PRIMARY KEY,
            intent_name VARCHAR(100),
            status      VARCHAR(50),
            details     TEXT,
            executed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )
    """,
}

_LOG_INSERT = (
    "INSERT INTO action_logs (intent_name, status, details) VALUES (%s, %s, %s)"
)

# Mirrored from the DDL above. Postgres rejects an over-long value
# outright, and losing a log line to a length error would be a worse
# outcome than storing a clipped one.
_INTENT_NAME_MAX = 100
_STATUS_MAX = 50

# Flipped once the database has proved unreachable, so a dead Postgres
# costs the assistant one connect timeout for the whole session instead
# of one per spoken command. Jarvis answers out loud in real time; five
# silent seconds per utterance is not an acceptable price for an audit
# trail.
_logging_disabled = False


def _error_detail(exc: BaseException) -> str:
    """The first line of an exception message - psycopg2's are multi-line."""
    text = str(exc).strip()
    return text.splitlines()[0] if text else exc.__class__.__name__


def _connect_writable():
    """
    Opens a normal (writable) session for the two system-table functions.

    Deliberately separate from the connection run_query opens: that one
    is read-only at the server, and must stay that way.
    """
    connection = psycopg2.connect(
        connect_timeout=_CONNECT_TIMEOUT_S,
        options=f"-c statement_timeout={_STATEMENT_TIMEOUT_MS}",
        application_name="jarvis",
        **_credentials(),
    )
    connection.set_session(autocommit=True)
    return connection


def init_system_tables() -> bool:
    """
    Creates the tables Jarvis owns - chat_history and action_logs - if
    they aren't there yet. Returns True if the schema is ready.

    Runs automatically at import (see _auto_init at the bottom of this
    file), so a fresh clone with credentials in .env needs no manual
    setup step. Safe to call repeatedly: CREATE TABLE IF NOT EXISTS does
    nothing when the table already exists, and nothing here alters one
    that does, so an existing chat_history keeps both its rows and its
    columns.

    Never raises. A missing driver, absent credentials, an unreachable
    server, and a database user without CREATE rights are all reported
    to the console and returned as False - none of them is a reason for
    the assistant to fail to start.
    """
    global _logging_disabled

    if psycopg2 is None:
        print("[Jarvis] Skipping system table setup: psycopg2 isn't installed.")
        return False

    if not is_configured():
        print("[Jarvis] Skipping system table setup: no database credentials set.")
        return False

    connection = None
    try:
        connection = _connect_writable()
        with connection.cursor() as cursor:
            for ddl in SYSTEM_TABLES.values():
                cursor.execute(ddl)
        print(f"[Jarvis] System tables ready: {', '.join(SYSTEM_TABLES)}.")
        return True

    except Exception as exc:  # noqa: BLE001 - startup must survive this
        detail = _error_detail(exc)
        if psycopg2 is not None and isinstance(exc, psycopg2.OperationalError):
            # Postgres isn't answering at all. Every log_action this
            # session would pay the same timeout, so stop before the
            # first one rather than after it.
            _logging_disabled = True
            print(
                f"[Jarvis] Could not reach the {DB_LABEL} database to prepare "
                f"the system tables: {detail}"
            )
            print("[Jarvis] Action logging is off for this session.")
        else:
            print(
                f"[Jarvis] Could not create the system tables "
                f"({type(exc).__name__}): {detail}"
            )
            print(
                "[Jarvis] Grant the database user CREATE on the schema, or set "
                "DB_AUTO_INIT=0 and create them by hand."
            )
        return False

    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:  # noqa: BLE001 - closing must not mask the result
                pass


def log_action(intent_name: str, status: str, details: str = "") -> bool:
    """
    Appends one row to action_logs and returns True if it landed.

    Best-effort by design, and called for its side effect: an audit
    trail is never a reason for a spoken answer to fail, so every
    failure is printed and swallowed rather than raised, and callers can
    ignore the return value.

    All three values are bound as query parameters, never interpolated,
    so `details` may safely carry a raw transcript.
    """
    global _logging_disabled

    if _logging_disabled or psycopg2 is None or not is_configured():
        return False

    intent = (intent_name or "unknown").strip()[:_INTENT_NAME_MAX]
    state = (status or "unknown").strip()[:_STATUS_MAX]
    text = (details or "").strip() or None

    connection = None
    try:
        connection = _connect_writable()
        with connection.cursor() as cursor:
            cursor.execute(_LOG_INSERT, (intent, state, text))
        return True

    except Exception as exc:  # noqa: BLE001 - logging must never raise
        detail = _error_detail(exc)
        if psycopg2 is not None and isinstance(exc, psycopg2.OperationalError):
            _logging_disabled = True
            print(f"[Jarvis] Action logging is off for this session: {detail}")
        elif pg_errors is not None and isinstance(exc, pg_errors.UndefinedTable):
            print(
                "[Jarvis] action_logs doesn't exist yet - "
                "call db_connector.init_system_tables()."
            )
        else:
            print(
                f"[Jarvis] Could not record the action log for {intent!r} "
                f"({type(exc).__name__}): {detail}"
            )
        return False

    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:  # noqa: BLE001 - closing must not mask the result
                pass


def _auto_init() -> None:
    """
    Prepares the system tables when this module is first imported.

    Only when credentials are actually configured, so importing
    db_connector on a machine with no database stays free. When they
    are, this costs one connection at startup - bounded by
    _CONNECT_TIMEOUT_S even if the host is unreachable.

    Set DB_AUTO_INIT=0 to skip it, which is the right setting once the
    schema is managed elsewhere, or once the database user is the
    SELECT-only role .env.example recommends.
    """
    if os.getenv("DB_AUTO_INIT", "1").strip().lower() in ("0", "false", "no", "off"):
        return
    if psycopg2 is None or not is_configured():
        return
    init_system_tables()


_auto_init()



if __name__ == "__main__":
    print("Registered queries:")
    for name in available_queries():
        verdict = _validate_sql(SAFE_QUERIES[name]["sql"]) or "safe"
        print(f"  {name!r:18} -> {verdict}")

    print(f"\nConfigured: {is_configured()}")

    print("\nName resolution check:")
    samples = (
        "",                    # bare "check database"
        "demp status",
        "demp database status",
        "damp status",         # Vosk mishearing
        "energy logs",
        "how many farmers",
        "delete everything",   # must not match
    )
    for phrase in samples:
        print(f"  {phrase!r:22} -> {resolve_query(phrase)!r}")
