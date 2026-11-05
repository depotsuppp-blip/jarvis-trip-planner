"""
The Head of Database: read-only SQL on a live PostgreSQL database.

    manager (llm_brain) --delegate_to_database(sql_query)--> DatabaseAgent.run_query
        -> check_sql: one SELECT (or WITH ... SELECT) statement, nothing that writes
        -> asyncpg: one connection, BEGIN READ ONLY, a statement timeout, at most MAX_ROWS rows
        <- a JSON array of records, [{"column": value, ...}, ...]
           or "ERROR: <PostgreSQL's own message>" so the manager can fix the SQL and call again

WHICH DATABASE. JARVIS_DATABASE_URL when it is set, else DATABASE_URL (.env). In this
project DATABASE_URL is the Trip Planner web app's own database, so by default Jarvis
reads the app's users, trips, polls and chats. JARVIS_DATABASE_URL lets Jarvis use a
different role - best of all a read-only one (.env.example) - without touching the URL
the web app writes with. Read on every call: edit .env and restart, nothing else.

READ-ONLY, IN LAYERS. Any one of them stops a write; together they are the point:

    1. check_sql reads the statement the way PostgreSQL's lexer does - comments, 'strings',
       E'escaped strings', $$dollar quoted$$ text and "quoted names" are told apart from
       the SQL around them - and refuses it unless it is ONE statement that starts with
       SELECT or WITH. It refuses INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE and GRANT
       anywhere in the SQL, and the other commands that write or change the session
       (FORBIDDEN_KEYWORDS), SELECT ... INTO, row locks (FOR UPDATE / FOR SHARE), and
       functions that act on the server rather than read it: ending other sessions,
       settings, sleeping, advisory locks, sequences, NOTIFY, dblink, files, large
       objects, running a query held in a string (DENIED_FUNCTIONS). A keyword inside a
       string or a quoted name is data, not a command - WHERE action = 'delete' is a
       read - which is why the statement is parsed rather than searched.
    2. PostgreSQL itself: every query runs inside BEGIN READ ONLY, which the server
       enforces (SQLSTATE 25006 for any write, whatever got past layer 1), and the
       transaction is always rolled back, never committed.
    3. Limits: SET LOCAL statement_timeout and lock_timeout, a client-side timeout on top,
       at most MAX_ROWS rows fetched through a cursor, and an answer cut to fit
       MAX_RESULT_CHARS - what comes back is read by a language model, every character
       of it billed and resent on each later step of the question.
    4. Secrets stay out of the chat: a column whose name says it holds a secret (a
       token, a password, an API or private key, a hash) comes back as "[hidden]", also
       inside JSON values, and a query that names one is refused (is_secret_name). A best
       effort against accidents - SELECT * on a table that has one - not against someone
       determined to read it: casting a whole row to text gets past it. The hard control
       is a role that cannot see those columns (JARVIS_DATABASE_URL).
    5. Who may ask: the brain lets only the owner reach this tool (llm_brain,
       DATABASE_OWNER_ONLY) - the database holds every web user's data.

A DIRECT CONNECTION PER QUERY, not a pool. The brain runs a tool on a fresh worker thread
for every call (asyncio.to_thread in a web run, the caller's thread by voice), and an
asyncpg pool belongs to the one event loop that made it; each query here runs in its own
asyncio.run. One connection costs a few round trips - a few hundred milliseconds to Neon
in Singapore, up to some seconds while a suspended Neon compute wakes - which nobody waits
on for a question that already took a model call. Neon's pooled URL (PgBouncer, "-pooler")
works: asyncpg's statement cache is off and everything happens inside one transaction.

THE URL. asyncpg reads a postgresql:// URL much as libpq does, but sends every query
parameter it does not know to the server as a setting, and the server refuses the
connection over it. So the parameters only libpq or Prisma know are taken out first
(connection_settings): connect_timeout becomes the connect timeout, Prisma's schema= the
search_path, and channel_binding=require - asyncpg cannot do channel binding - becomes a
certificate checked against this computer's trusted authorities, which protects against
the same man-in-the-middle that channel binding does.

THE ANSWER. Records as JSON - dates and times as ISO text (a time with a zone in the
user's own, JARVIS_TIMEZONE; one without, such as Prisma's UTC DateTime, as stored),
NUMERIC as a number when that loses nothing (else text), json/jsonb columns as JSON,
binary data as "<n bytes>". Two
columns with one name get _2, _3. A line starting "NOTE:" comes first when there is
something to know: more rows than were sent, text cut short, columns hidden, no rows.
An undefined table or column is answered with PostgreSQL's message plus the tables and
columns there are, so the next try can be right.

FOR A SPREADSHEET (export, QueryResult.table) the same query fetches up to EXPORT_MAX_ROWS
rows with longer text, and the brain hands them to the Head of Analytics itself
(llm_brain, delegate_to_database's `spreadsheet` argument): a model asked to copy rows
into another tool call re-types them, and in a live test it turned a Thai name into a
romanised one. Rows that never pass through a model cannot change.

NEVER RAISES, except request_context.Cancelled when the person pressed Stop: the query in
flight is cancelled on the server and the connection closed.

    python -m agents.database_agent --check        connects, runs SELECT 1, counts the tables
    python -m agents.database_agent "SELECT ..."   runs one query and prints the tool's answer
"""

from __future__ import annotations

import asyncio
import contextvars
import datetime as dt
import decimal
import ipaddress
import json
import math
import os
import re
import ssl
import sys
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import request_context

try:
    import asyncpg

    _IMPORT_ERROR = ""
except ImportError as exc:  # pragma: no cover - requirements.txt has it; an old environment may not
    asyncpg = None  # type: ignore[assignment]
    _IMPORT_ERROR = str(exc)

# Asked for in this order: a role meant for Jarvis, then the web app's own URL.
URL_VARIABLES = ("JARVIS_DATABASE_URL", "DATABASE_URL")
APPLICATION_NAME = "jarvis-database-agent"  # what pg_stat_activity shows for these connections

MAX_SQL_CHARS = 20_000
MAX_ROWS = 200
MAX_RESULT_CHARS = 24_000
MAX_CELL_CHARS = 2_000
# For a spreadsheet (export): the rows go into a file, not to a model - the Head of
# Analytics' own limits (10,000 rows, Excel's 32,767 characters a cell).
EXPORT_MAX_ROWS = 10_000
EXPORT_CELL_CHARS = 32_000
MAX_SCHEMA_CHARS = 6_000
STATEMENT_TIMEOUT_SECONDS = 15
LOCK_TIMEOUT_SECONDS = 3
CONNECT_TIMEOUT_SECONDS = 15
SCHEMA_CACHE_SECONDS = 600
HIDDEN = "[hidden]"

# The spec's list first: refused anywhere outside strings, comments and quoted names.
FORBIDDEN_KEYWORDS: tuple[str, ...] = (
    "insert", "update", "delete", "drop", "alter", "truncate", "grant",
    # The other commands that write, change the schema, the session or the transaction.
    # None can start a SELECT, so these only ever turn away a statement that is not one.
    "revoke", "create", "merge", "copy", "call", "do", "execute", "prepare", "deallocate",
    "set", "reset", "lock", "vacuum", "analyze", "analyse", "reindex", "cluster", "refresh",
    "comment", "listen", "notify", "unlisten", "begin", "commit", "rollback", "savepoint",
    "discard", "import", "load", "checkpoint", "security",
    # SELECT ... INTO new_table creates a table.
    "into",
)
_SPEC_KEYWORDS = frozenset(FORBIDDEN_KEYWORDS[:7])

# Functions that act on the server instead of reading data, matched on the name however it
# is written (pg_catalog.x, "x"): end or cancel other sessions (the web app's included),
# change settings, sleep, hold locks, move sequences, NOTIFY, reach other databases
# (dblink opens a connection that is NOT read-only), read files, large objects, run a
# query held in a string (query_to_xml, ts_stat), WAL and replication admin.
DENIED_FUNCTIONS = re.compile(
    r"(?:pg_terminate_backend|pg_cancel_backend|pg_reload_conf|pg_rotate_logfile|pg_promote"
    r"|pg_switch_wal|pg_create_restore_point|pg_backup_\w+|pg_start_backup|pg_stop_backup"
    r"|pg_wal_replay_\w+|pg_log_backend_memory_contexts|pg_log_standby_snapshot"
    r"|set_config|pg_sleep\w*|pg_advisory\w*|pg_try_advisory\w*|nextval|setval|pg_notify"
    r"|dblink\w*|lo_\w+|pg_read_file|pg_read_binary_file|pg_ls_\w+|pg_stat_file|pg_file_\w+"
    r"|pg_logdir_ls|pg_import_system_collations|pg_create_\w+|pg_drop_replication_slot"
    r"|pg_replication_\w+|pg_logical_\w+|pg_copy_\w+|pg_stat_reset\w*|pg_stat_statements_reset"
    r"|query_to_\w+|cursor_to_\w+|table_to_\w+|schema_to_\w+|database_to_\w+|ts_stat"
    r"|pg_export_snapshot|txid_current\w*|pg_current_xact_id\w*)"
)
_ROW_LOCK = re.compile(r"\bfor\s+(?:no\s+key\s+update|key\s+share|update|share)\b", re.IGNORECASE)

# A name that says it holds a secret. "token", "password" ... anywhere in the name
# (adminTokenHash, access_token, passwordHash); "hash", "salt", "pwd" only as a word of it
# (pin_hash, but not hashtags).
_SECRET_PARTS = (
    "password", "passwd", "secret", "token", "apikey", "privatekey", "credential", "voterkey",
    "accesskey", "signingkey", "encryptionkey",
)
_SECRET_WORDS = frozenset({"hash", "hashed", "salt", "pwd"})

_WORD = re.compile(r"[^\W\d][\w$]*")
_DOLLAR_TAG = re.compile(r"\$(?:[^\W\d]\w*)?\$")
_FIRST_WORD = re.compile(r"[\s(]*([^\W\d][\w$]*)")
_SIMPLE_NAME = re.compile(r"[a-z_][a-z0-9_]*")
# Lower-case names that still need quotes, as a table or column name in a schema listing.
_RESERVED = frozenset(
    "all analyse analyze and any array as asc asymmetric both case cast check collate column "
    "constraint create current_catalog current_date current_role current_time current_timestamp "
    "current_user default deferrable desc distinct do else end except false fetch for foreign "
    "from grant group having in initially intersect into lateral leading limit localtime "
    "localtimestamp not null offset on only or order placing primary references returning "
    "select session_user some symmetric table then to trailing true union unique user using "
    "variadic when where window with".split()
)

_LIBPQ_ONLY = frozenset({
    "channel_binding", "connect_timeout", "gssencmode", "gssdelegation", "keepalives",
    "keepalives_idle", "keepalives_interval", "keepalives_count", "tcp_user_timeout",
    "requiressl", "sslcompression", "sslsni", "sslcertmode", "requirepeer", "require_auth",
    "load_balance_hosts", "replication", "fallback_application_name", "client_encoding",
    "hostaddr",
})
_PRISMA_ONLY = frozenset({
    "schema", "pgbouncer", "connection_limit", "pool_timeout", "socket_timeout", "sslaccept",
    "sslidentity", "statement_cache_size", "max_connection_lifetime", "max_idle_connection_lifetime",
})

_SCHEMA_SQL = (
    "SELECT c.table_schema::text, c.table_name::text, "
    "array_agg(c.column_name::text ORDER BY c.ordinal_position) "
    "FROM information_schema.columns c "
    "WHERE c.table_schema NOT IN ('pg_catalog', 'information_schema') AND c.table_schema NOT LIKE 'pg\\_%' "
    "GROUP BY c.table_schema, c.table_name "
    "ORDER BY c.table_schema <> 'public', c.table_schema, c.table_name"
)
_SCHEMA_SQLSTATES = frozenset({"42P01", "42703", "3F000"})  # undefined table, column, schema


# =====================================================================
# Checking the SQL
# =====================================================================


@dataclass
class _Scan:
    """The statement with every string, quoted name and comment blanked out, and those names."""

    code: str = ""
    quoted: list[str] = field(default_factory=list)
    problem: str = ""


def _ident_char(ch: str) -> bool:
    return ch.isalnum() or ch in "_$"


def _scan(sql: str) -> _Scan:
    """
    Splits `sql` the way PostgreSQL's lexer does, with standard_conforming_strings on
    (which the query's own transaction sets, so the server reads it the same way).
    """
    out: list[str] = []
    quoted: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        before = sql[i - 1] if i else ""
        if ch == "-" and nxt == "-":
            end = sql.find("\n", i)
            i = n if end == -1 else end
            out.append(" ")
            continue
        if ch == "/" and nxt == "*":
            depth, j = 1, i + 2
            while j < n and depth:
                if sql.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif sql.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            if depth:
                return _Scan(problem="a /* comment is never closed")
            out.append(" ")
            i = j
            continue
        if ch in "uU" and nxt == "&" and i + 2 < n and sql[i + 2] in "'\"" and not _ident_char(before):
            return _Scan(problem="U& escapes are not accepted: write the text or the name as it is")
        if ch == "'":
            # E'...' takes backslash escapes; the E is its own word, not the end of a name.
            escapes = before in ("e", "E") and (i < 2 or not _ident_char(sql[i - 2]))
            j = i + 1
            while True:
                if j >= n:
                    return _Scan(problem="a 'string' is never closed")
                c = sql[j]
                if escapes and c == "\\":
                    j += 2
                    continue
                if c == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            out.append(" '' ")
            i = j + 1
            continue
        if ch == '"':
            j, name = i + 1, []
            while True:
                if j >= n:
                    return _Scan(problem='a "quoted name" is never closed')
                if sql[j] == '"':
                    if j + 1 < n and sql[j + 1] == '"':
                        name.append('"')
                        j += 2
                        continue
                    break
                name.append(sql[j])
                j += 1
            quoted.append("".join(name))
            out.append(' "" ')
            i = j + 1
            continue
        if ch == "$" and not _ident_char(before):
            tag = _DOLLAR_TAG.match(sql, i)
            if tag:
                end = sql.find(tag.group(0), tag.end())
                if end == -1:
                    return _Scan(problem=f"a {tag.group(0)} quoted string is never closed")
                out.append(" '' ")
                i = end + len(tag.group(0))
                continue
        out.append(ch)
        i += 1
    return _Scan(code="".join(out), quoted=quoted)


def _name_words(name: str) -> list[str]:
    """adminTokenHash -> admin, token, hash; access_token -> access, token."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return [word for word in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if word]


def is_secret_name(name: str) -> bool:
    """True for a column (or table) name that says it holds a secret."""
    flat = re.sub(r"[^a-z0-9]", "", name.lower())
    if any(part in flat for part in _SECRET_PARTS):
        return True
    return any(word in _SECRET_WORDS for word in _name_words(name))


def check_sql(sql: Any) -> str:
    """
    "" when `sql` may run, else why not - one sentence the manager can act on. Only one
    SELECT (or WITH ... SELECT) statement passes; see the module docstring for the rest.
    """
    if not isinstance(sql, str) or not sql.strip():
        return "the query is empty: send one SELECT statement as sql_query"
    if len(sql) > MAX_SQL_CHARS:
        return f"the query is {len(sql):,} characters long; the limit is {MAX_SQL_CHARS:,}"
    if "\x00" in sql:
        return "the query contains a NUL character"
    scan = _scan(sql)
    if scan.problem:
        return scan.problem
    code = re.sub(r"[;\s]+$", "", scan.code)
    if ";" in code:
        return "only one statement can run per call: remove the ';' and everything after it"
    first = _FIRST_WORD.match(code)
    if first is None or first.group(1).lower() not in ("select", "with"):
        word = first.group(1).upper() if first else "that"
        return f"only SELECT statements are permitted (a WITH ... SELECT is fine); this one starts with {word}"
    if _ROW_LOCK.search(code):
        return "row locks (FOR UPDATE / FOR SHARE) are not available on a read-only connection: leave them out"
    words = _WORD.findall(code)
    lowered = [word.lower() for word in words]
    for word in lowered:
        if word in FORBIDDEN_KEYWORDS:
            what = word.upper()
            if word == "into":
                return "SELECT ... INTO creates a table, and Jarvis's access is read-only: leave out the INTO"
            reason = (
                "only SELECT statements are permitted, so Jarvis cannot add, change or remove data"
                if word in _SPEC_KEYWORDS
                else "Jarvis's access is read-only: only SELECT statements are permitted"
            )
            return f"the query contains {what}: {reason}. If {what} is the name of a column or table, write it in double quotes"
    for name in lowered + [name.lower() for name in scan.quoted]:
        bare = name.rsplit(".", 1)[-1]
        if DENIED_FUNCTIONS.fullmatch(bare):
            return (
                f"the query calls {bare}, which changes the server's state, reaches outside this database "
                "or runs other SQL - Jarvis only reads data"
            )
    for name in words + scan.quoted:
        if is_secret_name(name):
            return (
                f'the query names "{name}", which looks like it holds a secret (a token, password, key or hash). '
                f"Jarvis does not read secrets: leave it out. SELECT * is fine - such columns come back as {HIDDEN}"
            )
    return ""


# =====================================================================
# The connection
# =====================================================================


@dataclass(frozen=True)
class ConnectionSettings:
    """What asyncpg.connect gets, worked out from the URL (connection_settings)."""

    dsn: str
    host: str
    port: Optional[int]
    database: str
    user: str
    password: str
    ssl: Any = None  # None: the URL's sslmode decides; an SSLContext: a checked certificate
    connect_timeout: float = CONNECT_TIMEOUT_SECONDS
    search_path: str = ""  # Prisma's ?schema=, quoted; "" for the server's own
    dropped: tuple[str, ...] = ()  # the URL parameters taken out
    verified_tls: bool = False

    def where(self) -> str:
        """host/database as user, for the console; never the password."""
        port = f":{self.port}" if self.port else ""
        return f"{self.host or 'localhost'}{port}/{self.database or '(default)'} as {self.user or '(default user)'}"


def database_url() -> tuple[str, str]:
    """(the URL, the variable it came from); ("", "") when neither is set."""
    for name in URL_VARIABLES:
        value = os.environ.get(name, "").strip().strip('"').strip("'")
        if value:
            return value, name
    return "", ""


def quote_ident(name: str) -> str:
    """A name as SQL would have to write it: bare when it can be, else in double quotes."""
    # A FORBIDDEN_KEYWORDS name ("comment") in quotes too: check_sql refuses it bare.
    if _SIMPLE_NAME.fullmatch(name) and name not in _RESERVED and name not in FORBIDDEN_KEYWORDS:
        return name
    return '"' + name.replace('"', '""') + '"'


def connection_settings(url: str) -> ConnectionSettings:
    """
    Reads a postgresql:// URL for asyncpg; raises ValueError naming what is wrong. Takes out
    the parameters asyncpg would send the server as settings (see THE URL above).
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("postgres", "postgresql"):
        raise ValueError(f"the database URL must start with postgresql:// (it starts with {parts.scheme or 'nothing'}://)")
    pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    kept: list[tuple[str, str]] = []
    dropped: list[str] = []
    options: dict[str, str] = {}
    for key, value in pairs:
        if key in _LIBPQ_ONLY or key in _PRISMA_ONLY:
            dropped.append(key)
            options[key] = value
        else:
            kept.append((key, value))

    timeout = float(CONNECT_TIMEOUT_SECONDS)
    if options.get("connect_timeout", "").strip().isdigit():
        timeout = float(min(60, max(2, int(options["connect_timeout"]))))
    search_path = ""
    if options.get("schema", "").strip():
        search_path = quote_ident(options["schema"].strip())
    context: Any = None
    verified = False
    if options.get("channel_binding", "").strip().lower() == "require":
        context = ssl.create_default_context()  # the system's trusted authorities, host name checked
        verified = True

    dsn = urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(kept), parts.fragment)
    )
    try:
        port = parts.port
    except ValueError:
        port = None
    return ConnectionSettings(
        dsn=dsn,
        host=parts.hostname or "",
        port=port,
        database=urllib.parse.unquote(parts.path.lstrip("/")),
        user=urllib.parse.unquote(parts.username or ""),
        password=urllib.parse.unquote(parts.password or ""),
        ssl=context,
        connect_timeout=timeout,
        search_path=search_path,
        dropped=tuple(dropped),
        verified_tls=verified,
    )


# =====================================================================
# The answer
# =====================================================================


@dataclass
class QueryResult:
    """One query's outcome. to_text() is what the manager reads."""

    ok: bool
    records: list[dict[str, Any]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    more: bool = False  # the query had more rows than were fetched
    hidden: list[str] = field(default_factory=list)  # secret-looking names whose values were hidden
    cut_cells: int = 0  # text values cut to MAX_CELL_CHARS
    error: str = ""
    seconds: float = 0.0

    def table(self, sheet_name: str = "") -> dict[str, Any]:
        """The rows as the Head of Analytics reads them: a JSON value or list in a cell becomes its JSON text."""
        rows = [
            {
                key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value
                for key, value in record.items()
            }
            for record in self.records
        ]
        return {"sheet_name": sheet_name or "Query", "data": rows}

    def to_text(self, limit: int = MAX_RESULT_CHARS) -> str:
        """The JSON array of records, after a NOTE: line when there is something to know; or ERROR: ..."""
        if not self.ok:
            return f"ERROR: {self.error}"
        rows = self.records
        body = json.dumps(rows, ensure_ascii=False, default=str)
        notes: list[str] = []
        shown = len(rows)
        if len(body) > limit:
            pieces = [json.dumps(row, ensure_ascii=False, default=str) for row in rows]
            total, shown = 2, 0
            for piece in pieces:
                if total + len(piece) + 2 > limit:
                    break
                total += len(piece) + 2
                shown += 1
            body = "[" + ", ".join(pieces[:shown]) + "]"
            notes.append(
                f"only the first {shown} of {len(rows)}{'+' if self.more else ''} rows fit in the answer. "
                "Ask for fewer columns, aggregate in SQL (count, sum, GROUP BY) or add a LIMIT for the rest."
            )
        elif self.more:
            notes.append(
                f"only the first {len(rows)} rows are shown; the query returned more. "
                "Aggregate in SQL (count, sum, GROUP BY) or add a WHERE or LIMIT for the rest."
            )
        if self.hidden:
            names = ", ".join(self.hidden[:8])
            notes.append(f"{names} look{'s' if len(self.hidden) == 1 else ''} like {'a secret' if len(self.hidden) == 1 else 'secrets'}: shown as {HIDDEN}.")
        if self.cut_cells:
            notes.append(f"{self.cut_cells} text value{'s were' if self.cut_cells != 1 else ' was'} longer than {MAX_CELL_CHARS:,} characters and cut short.")
        if not rows:
            columns = ", ".join(self.columns[:30]) or "none"
            notes.append(f"the query returned no rows (its columns: {columns}).")
        return (f"NOTE: {' '.join(notes)}\n" if notes else "") + body


_IP_TYPES = (
    ipaddress.IPv4Address, ipaddress.IPv6Address, ipaddress.IPv4Network, ipaddress.IPv6Network,
    ipaddress.IPv4Interface, ipaddress.IPv6Interface,
)


def _cut(text: str, limit: int = MAX_CELL_CHARS) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return f"{text[:limit]}… [{len(text) - limit:,} more characters]", True


def _user_zone() -> Any:
    """The user's time zone (JARVIS_TIMEZONE, else Bangkok), the one the brain tells the model it is in."""
    try:
        from llm_brain import DEFAULT_TIMEZONE, _zone  # lazy, as in agents/base.py: llm_brain imports agents

        return _zone(os.environ.get("JARVIS_TIMEZONE", "").strip() or DEFAULT_TIMEZONE)
    except Exception:  # noqa: BLE001 - the brain cannot be imported here: times stay in UTC
        return dt.timezone.utc


class _Shaper:
    """
    Turns asyncpg values into JSON values, hiding secret-looking keys and counting cut text.
    A time with a zone (timestamptz) is shown in the user's zone; one without (timestamp, how
    Prisma stores DateTime - in UTC) exactly as stored.
    """

    def __init__(self, cell_chars: int = MAX_CELL_CHARS) -> None:
        self.hidden: list[str] = []
        self.cut = 0
        self.cell_chars = cell_chars
        self._zone: Any = None

    def hide(self, name: str) -> None:
        if name not in self.hidden:
            self.hidden.append(name)

    def value(self, value: Any, depth: int = 0) -> Any:
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, str):
            text, cut = _cut(value, self.cell_chars)
            self.cut += cut
            return text
        if isinstance(value, float):
            return value if math.isfinite(value) else str(value)
        if isinstance(value, decimal.Decimal):
            if not value.is_finite():
                return str(value)
            if value == value.to_integral_value() and abs(value) < 2**53:
                return int(value)
            as_float = float(value)
            return as_float if decimal.Decimal(repr(as_float)) == value else str(value)
        if isinstance(value, dt.datetime) and value.tzinfo is not None:
            if self._zone is None:
                self._zone = _user_zone()
            return value.astimezone(self._zone).isoformat()
        if isinstance(value, (dt.datetime, dt.date, dt.time)):
            return value.isoformat()
        if isinstance(value, dt.timedelta):
            return str(value)
        if isinstance(value, (uuid.UUID, *_IP_TYPES)):
            return str(value)
        if isinstance(value, (bytes, bytearray, memoryview)):
            return f"<{len(value):,} bytes>"
        if depth > 20:
            return str(value)
        if asyncpg is not None and isinstance(value, asyncpg.Record):
            return self.mapping(dict(value.items()), depth)
        if isinstance(value, dict):
            return self.mapping(value, depth)
        if isinstance(value, (list, tuple, set, frozenset)):
            return [self.value(item, depth + 1) for item in value]
        if asyncpg is not None and isinstance(value, asyncpg.Range):
            if value.isempty:
                return "empty"
            lower = "" if value.lower is None else self.value(value.lower, depth + 1)
            upper = "" if value.upper is None else self.value(value.upper, depth + 1)
            return f"{'[' if value.lower_inc else '('}{lower},{upper}{']' if value.upper_inc else ')'}"
        if asyncpg is not None and isinstance(value, asyncpg.BitString):
            return value.as_string()
        return str(value)

    def mapping(self, data: dict[Any, Any], depth: int) -> dict[str, Any]:
        shaped: dict[str, Any] = {}
        for key, item in data.items():
            name = str(key)
            if is_secret_name(name):
                self.hide(name)
                shaped[name] = HIDDEN
            else:
                shaped[name] = self.value(item, depth + 1)
        return shaped


def _column_names(attributes: Any) -> list[str]:
    """The result's column names, a repeated one as name_2, name_3."""
    names: list[str] = []
    seen: dict[str, int] = {}
    for attribute in attributes:
        name = str(attribute.name) or "?column?"
        count = seen.get(name, 0) + 1
        seen[name] = count
        names.append(name if count == 1 else f"{name}_{count}")
    return names


def _json_kind(attribute: Any) -> str:
    """"json" for a json/jsonb column, "array" for an array of them, else ""."""
    name = getattr(getattr(attribute, "type", None), "name", "") or ""
    if name in ("json", "jsonb"):
        return "json"
    if name in ("_json", "_jsonb"):
        return "array"
    return ""


def _decode_json(value: Any, kind: str) -> Any:
    if value is None or not kind:
        return value
    try:
        if kind == "json" and isinstance(value, str):
            return json.loads(value)
        if kind == "array" and isinstance(value, list):
            return [json.loads(item) if isinstance(item, str) else item for item in value]
    except json.JSONDecodeError:
        return value
    return value


def _shape(attributes: Any, records: list[Any], cell_chars: int = MAX_CELL_CHARS) -> tuple[list[str], list[dict[str, Any]], _Shaper]:
    columns = _column_names(attributes)
    kinds = [_json_kind(attribute) for attribute in attributes]
    shaper = _Shaper(cell_chars)
    secret = [is_secret_name(name) for name in columns]
    for name, hide in zip(columns, secret):
        if hide:
            shaper.hide(name)
    rows: list[dict[str, Any]] = []
    for record in records:
        row: dict[str, Any] = {}
        for index, name in enumerate(columns):
            if secret[index]:
                row[name] = HIDDEN
            else:
                row[name] = shaper.value(_decode_json(record[index], kinds[index]))
        rows.append(row)
    return columns, rows, shaper


def _shown(text: str, limit: int = 160) -> str:
    """`text` cut short and safe for a console that cannot print Thai."""
    text = " ".join(text.split())
    text = text if len(text) <= limit else text[: limit - 1] + "…"
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return text.encode(encoding, errors="backslashreplace").decode(encoding)


# =====================================================================
# The agent
# =====================================================================


class _ConnectFailed(Exception):
    """Could not open the connection; the message is ready for the manager."""


class DatabaseAgent:
    """
    Runs one read-only SELECT per call on the PostgreSQL database in JARVIS_DATABASE_URL or
    DATABASE_URL and answers with JSON records. No model; one database connection per call.
    """

    name = "database"
    title = "Head of Database"
    tool_name = "delegate_to_database"

    def __init__(
        self,
        *,
        url: Optional[str] = None,
        max_rows: int = MAX_ROWS,
        statement_timeout: float = STATEMENT_TIMEOUT_SECONDS,
    ) -> None:
        self._url = url  # None: read the environment on every call
        self.max_rows = max_rows
        self.statement_timeout = statement_timeout
        self._schema_lock = threading.Lock()
        self._schema: dict[str, tuple[float, str]] = {}  # dsn -> (when, the listing)

    # -- setup -------------------------------------------------------------

    def _settings(self) -> tuple[Optional[ConnectionSettings], str]:
        """(settings, "") or (None, why there are none)."""
        if self._url is not None:
            url, source = self._url.strip(), "the database URL"
        else:
            url, source = database_url()
        if not url:
            return None, "DATABASE_URL is not set in .env"
        try:
            return connection_settings(url), ""
        except ValueError as exc:
            return None, f"{source} is not usable: {exc}"

    @property
    def unavailable_reason(self) -> str:
        if asyncpg is None:
            return f"asyncpg is not installed ({_IMPORT_ERROR}); run pip install -r requirements.txt"
        return self._settings()[1]

    @property
    def available(self) -> bool:
        return not self.unavailable_reason

    def describe(self) -> str:
        if not self.available:
            return f"off ({self.unavailable_reason})"
        settings, _ = self._settings()
        assert settings is not None
        source = "the given URL" if self._url is not None else database_url()[1]
        tls = ", certificate checked" if settings.verified_tls else ""
        return (
            f"asyncpg {asyncpg.__version__} -> {settings.where()} ({source}{tls}); read-only transactions, "
            f"{self.statement_timeout:g} s per query, up to {self.max_rows} rows"
        )

    # -- the tool ----------------------------------------------------------

    def run_query(self, sql_query: Any = "") -> str:
        """The tool: a JSON array of records, or ERROR: and why. Blocking; raises only Cancelled."""
        return self.query(sql_query).to_text()

    def query(self, sql: Any, *, max_rows: Optional[int] = None, cell_chars: int = MAX_CELL_CHARS) -> QueryResult:
        """aquery for code that is not async: runs it to the end on this thread (or a helper)."""
        return _run_sync(self.aquery(sql, max_rows=max_rows, cell_chars=cell_chars))

    def export(self, sql: Any) -> QueryResult:
        """The rows for a file rather than for a model to read: up to EXPORT_MAX_ROWS, text up to EXPORT_CELL_CHARS."""
        return self.query(sql, max_rows=EXPORT_MAX_ROWS, cell_chars=EXPORT_CELL_CHARS)

    async def aquery(self, sql: Any, *, max_rows: Optional[int] = None, cell_chars: int = MAX_CELL_CHARS) -> QueryResult:
        """Checks and runs one SELECT. Never raises, except request_context.Cancelled."""
        if asyncpg is None:
            return QueryResult(False, error=f"the database is not set up ({self.unavailable_reason}). Tell the user; do not try again.")
        problem = check_sql(sql)
        if problem:
            print(f"[Agent:database] Refused: {_shown(problem, 200)}")
            return QueryResult(False, error=f"{problem}.")
        settings, why = self._settings()
        if settings is None:
            return QueryResult(False, error=f"the database is not set up ({why}). Tell the user; do not try again.")
        request_context.check_cancelled()
        print(f"[Agent:database] Query on {settings.host or 'localhost'}: {_shown(sql)}")
        started = time.monotonic()
        cancel = request_context.cancel_event()
        work = asyncio.ensure_future(self._run(settings, sql, max_rows or self.max_rows, cell_chars))
        try:
            while not work.done():
                if cancel is not None and cancel.is_set():
                    work.cancel()  # asyncpg asks the server to cancel the statement
                    try:
                        await asyncio.wait_for(asyncio.shield(work), timeout=5)
                    except BaseException:  # noqa: BLE001 - it was cancelled; how it ended does not matter
                        pass
                    print("[Agent:database] Stopped: the query was cancelled.")
                    raise request_context.Cancelled()
                await asyncio.wait({work}, timeout=0.2)
            result = work.result()
        except asyncio.CancelledError:
            work.cancel()
            raise
        result.seconds = time.monotonic() - started
        if result.ok:
            more = "+" if result.more else ""
            print(f"[Agent:database] {len(result.records)}{more} row(s), {len(result.columns)} column(s) in {result.seconds:.2f} s.")
        else:
            print(f"[Agent:database] Failed in {result.seconds:.2f} s: {_shown(result.error, 240)}")
        return result

    async def _run(self, settings: ConnectionSettings, sql: str, max_rows: int, cell_chars: int) -> QueryResult:
        try:
            connection = await self._connect(settings)
        except _ConnectFailed as failed:
            return QueryResult(False, error=_scrub(str(failed), settings))
        try:
            return await self._select(connection, settings, sql, max_rows, cell_chars)
        finally:
            try:
                await asyncio.wait_for(connection.close(), timeout=5)
            except BaseException:  # noqa: BLE001 - a stuck close must not hold the answer
                connection.terminate()

    async def _connect(self, settings: ConnectionSettings) -> Any:
        try:
            return await asyncpg.connect(
                settings.dsn,
                ssl=settings.ssl,
                timeout=settings.connect_timeout,
                command_timeout=self.statement_timeout + 5,
                statement_cache_size=0,  # PgBouncer (Neon's pooled URL) and one-off connections
                server_settings={"application_name": APPLICATION_NAME},
            )
        except (asyncio.TimeoutError, TimeoutError):
            raise _ConnectFailed(
                f"the database did not answer within {settings.connect_timeout:g} seconds (a Neon database that "
                "was asleep can take a few seconds to wake). You may try once more; if it fails again, tell the user."
            ) from None
        except Exception as exc:  # noqa: BLE001 - refused, DNS, TLS, wrong password, no such database...
            raise _ConnectFailed(
                f"could not connect to the database ({type(exc).__name__}: {_first_line(exc)}). "
                "This is not a problem with the SQL: tell the user, and do not retry."
            ) from None

    async def _select(
        self, connection: Any, settings: ConnectionSettings, sql: str, max_rows: int, cell_chars: int
    ) -> QueryResult:
        transaction = connection.transaction(readonly=True)
        await transaction.start()
        open_ = True
        try:
            await connection.execute(self._session_sql(settings))
            statement = await connection.prepare(sql)
            if statement.get_parameters():
                return QueryResult(
                    False,
                    error="the query uses $1-style placeholders, and nothing fills them: write the values into the SQL",
                )
            attributes = statement.get_attributes()
            cursor = await statement.cursor()
            records = await cursor.fetch(max_rows + 1)
            more = len(records) > max_rows
            columns, rows, shaper = _shape(attributes, records[:max_rows], cell_chars)
            return QueryResult(True, rows, columns, more, shaper.hidden, shaper.cut)
        except asyncpg.PostgresError as exc:
            message = _postgres_error(exc, sql)
            if getattr(exc, "sqlstate", "") in _SCHEMA_SQLSTATES:
                open_ = False
                await transaction.rollback()  # the failed transaction ends; the listing gets its own
                listing = await self._schema_listing(connection, settings)
                if listing:
                    message += f" {listing}"
            return QueryResult(False, error=_scrub(message, settings))
        except (asyncio.TimeoutError, TimeoutError):
            return QueryResult(
                False,
                error=f"the query took longer than {self.statement_timeout:g} seconds and was stopped: filter it, "
                "aggregate in SQL or add a LIMIT",
            )
        except asyncpg.InterfaceError as exc:
            return QueryResult(False, error=_scrub(f"{type(exc).__name__}: {_first_line(exc)}", settings))
        except Exception as exc:  # noqa: BLE001 - a lost connection, anything unexpected
            return QueryResult(False, error=_scrub(f"the query failed ({type(exc).__name__}: {_first_line(exc)})", settings))
        finally:
            if open_:
                try:
                    await transaction.rollback()  # never committed, even though nothing could be written
                except BaseException:  # noqa: BLE001 - the connection is closed right after anyway
                    pass

    def _session_sql(self, settings: ConnectionSettings) -> str:
        """SET LOCAL lines for this one transaction: limits, the lexer rule check_sql assumes, the schema."""
        lines = [
            f"SET LOCAL statement_timeout = '{int(self.statement_timeout * 1000)}ms'",
            f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT_SECONDS * 1000}ms'",
            f"SET LOCAL idle_in_transaction_session_timeout = '{int((self.statement_timeout + 15) * 1000)}ms'",
            "SET LOCAL standard_conforming_strings = on",
        ]
        if settings.search_path:
            lines.append(f"SET LOCAL search_path TO {settings.search_path}")
        return "; ".join(lines)

    async def _schema_listing(self, connection: Any, settings: ConnectionSettings) -> str:
        """"Tables you can query: "User"(id, name, ...); ..." after an undefined table or column, or ""."""
        now = time.monotonic()
        with self._schema_lock:
            cached = self._schema.get(settings.dsn)
        if cached and now - cached[0] < SCHEMA_CACHE_SECONDS:
            return cached[1]
        try:
            listing = connection.transaction(readonly=True)
            await listing.start()
            try:
                await connection.execute(self._session_sql(settings))
                rows = await connection.fetch(_SCHEMA_SQL)
            finally:
                await listing.rollback()
        except Exception as exc:  # noqa: BLE001 - the listing is a help, not the answer
            print(f"[Agent:database] Could not list the tables: {type(exc).__name__}: {_shown(str(exc))}")
            return ""
        tables: list[str] = []
        for schema, table, columns in rows:
            if is_secret_name(table):
                continue
            name = quote_ident(table) if schema == "public" else f"{quote_ident(schema)}.{quote_ident(table)}"
            visible = [quote_ident(column) for column in columns if not is_secret_name(column)]
            tables.append(f"{name}({', '.join(visible)})")
        if not tables:
            text = "There are no tables this role can read."
        else:
            text = "Tables you can query (names in double quotes must be written with them): "
            used, kept = len(text), []
            for entry in tables:
                if used + len(entry) + 2 > MAX_SCHEMA_CHARS:
                    break
                kept.append(entry)
                used += len(entry) + 2
            text += "; ".join(kept)
            if len(kept) < len(tables):
                text += f"; and {len(tables) - len(kept)} more tables"
        with self._schema_lock:
            self._schema[settings.dsn] = (now, text)
        return text

    # -- setup check (python -m agents.database_agent --check) ------------

    def check(self) -> list[str]:
        """Lines on the setup: the driver, the URL, and one SELECT 1 plus a table count. Reads no data."""
        lines = [f"Database: {self.describe()}"]
        if not self.available:
            return lines
        settings, _ = self._settings()
        assert settings is not None
        if settings.dropped:
            lines.append(f"  URL parameters handled here, not sent to the server: {', '.join(settings.dropped)}")
        result = self.query(
            "SELECT current_database() AS database, current_user AS role, "
            "(SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema NOT IN ('pg_catalog', 'information_schema')) AS tables"
        )
        if result.ok and result.records:
            row = result.records[0]
            lines.append(
                f"  connected in {result.seconds:.2f} s: database {row.get('database')}, role {row.get('role')}, "
                f"{row.get('tables')} tables"
            )
        else:
            lines.append(f"  FAILED: {result.error}")
        return lines


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__


def _postgres_error(exc: Any, sql: str) -> str:
    """PostgreSQL's own message, its SQLSTATE, where in the query, and its DETAIL and HINT."""
    message = (getattr(exc, "message", "") or str(exc)).strip()
    parts = [message]
    state = getattr(exc, "sqlstate", "")
    if state:
        parts[0] += f" (SQLSTATE {state})"
    position = getattr(exc, "position", None)
    if position and str(position).isdigit():
        index = int(position) - 1
        near = " ".join(sql[index:index + 40].split())
        if near:
            parts.append(f"At character {int(position)}, near: {near}")
    for label in ("detail", "hint"):
        value = (getattr(exc, label, "") or "").strip()
        if value:
            parts.append(f"{label.upper()}: {value}")
    if state == "25006":
        parts.append("This connection is read-only: only SELECT statements work.")
    elif state == "57014":
        parts.append("The query ran too long and was stopped: filter it, aggregate in SQL or add a LIMIT.")
    elif state == "42501":
        parts.append("The database role Jarvis uses may not read that.")
    return ". ".join(part.rstrip(".") for part in parts) + "."


def _scrub(text: str, settings: ConnectionSettings) -> str:
    """The text without the URL or the password in it, in case a driver message quotes them."""
    for secret in (settings.dsn, settings.password, urllib.parse.quote(settings.password, safe="")):
        if secret and len(secret) >= 4:
            text = text.replace(secret, "***")
    return text


def _run_sync(coroutine: Any) -> Any:
    """Runs a coroutine to the end from code that is not async. With a loop already on this
    thread, on a helper thread (with this thread's context: who is asking, the cancel event)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    context = contextvars.copy_context()
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            box["value"] = context.run(asyncio.run, coroutine)
        except BaseException as exc:  # noqa: BLE001 - handed back to the caller below
            box["error"] = exc

    helper = threading.Thread(target=work, name="database-agent", daemon=True)
    helper.start()
    helper.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


def build() -> DatabaseAgent:
    return DatabaseAgent()


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
    except ImportError:  # pragma: no cover
        pass
    agent = build()
    arguments = sys.argv[1:]
    if not arguments or arguments == ["--check"]:
        print("\n".join(agent.check()))
        raise SystemExit(0 if agent.available else 1)
    answer = agent.run_query(" ".join(arguments))
    print(_shown(answer, 4000))
    raise SystemExit(0 if not answer.startswith("ERROR") else 1)
