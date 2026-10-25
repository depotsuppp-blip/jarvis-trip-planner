"""
A local data analysis MCP server for Jarvis (see mcp_client.py): a private
SQLite workspace per person, so the model can calculate instead of estimate.
No API key, no cloud, nothing but the standard library.

    list_tables      the tables in the workspace, with columns and row counts
    describe_table   a profile of one table: per column its empty and distinct
                     values, min / max / mean / median / standard deviation /
                     total for numbers, the most common values for text
    load_file        a CSV, TSV or JSON file from the project folder -> a new
                     table (column names cleaned, number columns detected)
    query            ONE read-only SELECT; the rows come back as a table
    run_sql          changes: CREATE / INSERT / UPDATE / DELETE / DROP / ALTER,
                     several statements at once, all or nothing

Besides SQLite's own functions, queries can use median(x), stdev(x),
variance(x) and percentile(x, p) (p from 0 to 100).

WHERE THE DATA LIVES. <data dir>/workspaces/<person>.sqlite3 - JARVIS_DATA_DIR,
or .data next to this file (gitignored; a named volume in docker-compose.yml).
One workspace per caller scope ("owner", or one per person signed in to the web
app; see mcp_stdio.py), so nobody sees anyone else's tables.

WHAT IT MAY READ. load_file takes the same paths as the local_fs server and
goes through the same checks (mcp_filesystem.resolve): relative to the project
folder, no way out of it, and no .env files, keys or other private files.

WHAT A STATEMENT MAY DO. An SQLite authorizer allows each statement only the
actions of its kind. A query can read and nothing else (the connection is also
set to query_only). Neither a query nor run_sql can ATTACH another database
file, VACUUM INTO a file (a way to write anywhere on disk), change a PRAGMA,
load an extension, or take over the transaction. A statement is stopped after
a time limit, and a workspace cannot grow past 500 MB.

run_sql is the one tool marked destructive, so Jarvis asks the person before
running it (the web chat, which cannot ask yet, declines it); the other tools
only read or add.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import json
import math
import os
import re
import sqlite3
import statistics
import time
import unicodedata
from typing import Any, Callable, Iterable, Iterator, Optional

import mcp_filesystem as fs
from mcp_stdio import Server, ToolError, data_dir, scope_of

MAX_FILE_BYTES = 50_000_000
MAX_JSON_BYTES = 20_000_000
MAX_ROWS_LOADED = 1_000_000
MAX_COLUMNS = 200
MAX_DB_BYTES = 500_000_000
QUERY_SECONDS = 15.0
LOAD_SECONDS = 25.0          # mcp_client.py gives a tool call 30 seconds
DEFAULT_ROWS = 50
MAX_ROWS = 200
MAX_CELL_CHARS = 80
MAX_REPLY_CHARS = 7000       # mcp_client.py keeps 8000 characters of a result
MAX_SQL_CHARS = 100_000
MAX_STATEMENTS = 50
MAX_DESCRIBED_COLUMNS = 40
MAX_TABLES_LISTED = 50

_LOADABLE = {".csv": "csv", ".tsv": "csv", ".tab": "csv", ".txt": "csv", ".json": "json", ".jsonl": "jsonl", ".ndjson": "jsonl"}
_MISSING = {"", "na", "n/a", "null", "none", "nan", "-", "--", "—", "#n/a"}
_CURRENCY = "$€£¥฿₩₹"
# SQL words that would need quoting as column names; such a column gets a trailing "_".
_RESERVED = {
    "select", "from", "where", "group", "order", "by", "limit", "offset", "table", "index", "join", "on",
    "and", "or", "not", "null", "as", "case", "when", "then", "else", "end", "in", "is", "like", "between",
    "values", "insert", "update", "delete", "create", "drop", "alter", "union", "all", "distinct", "having",
    "default", "check", "primary", "key", "references", "transaction", "to", "into", "set", "with", "exists",
}


# =====================================================================
# The workspace
# =====================================================================


def workspace_path(scope: str) -> str:
    folder = os.path.join(data_dir(), "workspaces")
    os.makedirs(folder, exist_ok=True)
    if scope == "owner":
        name = "owner"
    else:
        readable = re.sub(r"[^a-z0-9]+", "-", scope.lower()).strip("-")[:40]
        name = f"{readable}-{hashlib.sha256(scope.encode('utf-8')).hexdigest()[:10]}"
    return os.path.join(folder, f"{name}.sqlite3")


class _Median:
    def __init__(self) -> None:
        self.values: list[float] = []

    def step(self, value: Any) -> None:
        number = _as_float(value)
        if number is not None:
            self.values.append(number)

    def finalize(self) -> Optional[float]:
        return statistics.median(self.values) if self.values else None


class _Stdev(_Median):
    def finalize(self) -> Optional[float]:
        return statistics.stdev(self.values) if len(self.values) > 1 else None


class _Variance(_Median):
    def finalize(self) -> Optional[float]:
        return statistics.variance(self.values) if len(self.values) > 1 else None


class _Percentile:
    def __init__(self) -> None:
        self.values: list[float] = []
        self.p: Optional[float] = None

    def step(self, value: Any, p: Any) -> None:
        number = _as_float(value)
        if number is not None:
            self.values.append(number)
        if self.p is None:
            self.p = _as_float(p)

    def finalize(self) -> Optional[float]:
        if not self.values or self.p is None or not 0 <= self.p <= 100:
            return None
        ordered = sorted(self.values)
        rank = (len(ordered) - 1) * self.p / 100
        low, high = math.floor(rank), math.ceil(rank)
        return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def _as_float(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
        return number if math.isfinite(number) else None
    return None


def _open(scope: str, *, create: bool) -> Optional[sqlite3.Connection]:
    path = workspace_path(scope)
    if not create and not os.path.exists(path):
        return None
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.execute("PRAGMA busy_timeout = 10000")
    page_size = conn.execute("PRAGMA page_size").fetchone()[0] or 4096
    conn.execute(f"PRAGMA max_page_count = {MAX_DB_BYTES // page_size}")
    conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 20_000_000)
    conn.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
    conn.create_aggregate("median", 1, _Median)
    conn.create_aggregate("stdev", 1, _Stdev)
    conn.create_aggregate("stddev", 1, _Stdev)
    conn.create_aggregate("variance", 1, _Variance)
    conn.create_aggregate("percentile", 2, _Percentile)
    try:
        conn.execute("SELECT sqrt(4), ln(1), power(2, 2), floor(1.5)")
    except sqlite3.OperationalError:  # an SQLite built without its math functions
        for name, arity, function in (
            ("sqrt", 1, math.sqrt), ("ln", 1, math.log), ("log10", 1, math.log10), ("exp", 1, math.exp),
            ("power", 2, math.pow), ("pow", 2, math.pow), ("floor", 1, math.floor), ("ceil", 1, math.ceil),
        ):
            conn.create_function(name, arity, _null_safe(function), deterministic=True)
    return conn


def _null_safe(function: Callable[..., float]) -> Callable[..., Optional[float]]:
    def wrapped(*args: Any) -> Optional[float]:
        try:
            return function(*args)
        except (TypeError, ValueError, OverflowError):
            return None

    return wrapped


# ---- what a statement may do -------------------------------------------

_READ_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
_READ_PRAGMAS = {"table_info", "table_xinfo", "index_list", "index_info", "foreign_key_list"}
_WRITE_ACTIONS = _READ_ACTIONS | {
    sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_TEMP_INDEX,
    sqlite3.SQLITE_CREATE_TEMP_TABLE, sqlite3.SQLITE_CREATE_TEMP_TRIGGER, sqlite3.SQLITE_CREATE_TEMP_VIEW,
    sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_CREATE_VIEW, sqlite3.SQLITE_DELETE, sqlite3.SQLITE_DROP_INDEX,
    sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_DROP_TEMP_INDEX, sqlite3.SQLITE_DROP_TEMP_TABLE,
    sqlite3.SQLITE_DROP_TEMP_TRIGGER, sqlite3.SQLITE_DROP_TEMP_VIEW, sqlite3.SQLITE_DROP_TRIGGER,
    sqlite3.SQLITE_DROP_VIEW, sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_ALTER_TABLE,
    sqlite3.SQLITE_REINDEX, sqlite3.SQLITE_ANALYZE,
}
# Never: SQLITE_ATTACH (also how VACUUM INTO 'file' shows up), DETACH, PRAGMA
# (except the read-only ones above), TRANSACTION, SAVEPOINT, virtual tables.
_FORBIDDEN_FUNCTIONS = {"load_extension", "fts3_tokenizer", "readfile", "writefile", "edit"}


def _authorizer(allowed: set[int]) -> Callable[..., int]:
    def check(action: int, arg1: Any, arg2: Any, database: Any, source: Any) -> int:
        if action == sqlite3.SQLITE_FUNCTION and str(arg2 or "").lower() in _FORBIDDEN_FUNCTIONS:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_PRAGMA:
            name = str(arg1 or "").lower()
            return sqlite3.SQLITE_OK if name in _READ_PRAGMAS and arg2 is not None else sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY

    return check


def _deadline(conn: sqlite3.Connection, seconds: float) -> None:
    stop = time.monotonic() + seconds
    conn.set_progress_handler(lambda: 1 if time.monotonic() > stop else 0, 20_000)


def _read_only(conn: sqlite3.Connection) -> None:
    """From here on this connection can only read: query_only, the read authorizer, a time limit."""
    conn.execute("PRAGMA query_only = ON")  # before the authorizer, which would refuse the PRAGMA
    conn.set_authorizer(_authorizer(_READ_ACTIONS))
    _deadline(conn, QUERY_SECONDS)


def _sql_error(exc: sqlite3.Error, seconds: float) -> ToolError:
    message = str(exc)
    if "interrupted" in message:
        return ToolError(f"The statement ran longer than {seconds:.0f} seconds and was stopped. Narrow it down.")
    if "not authorized" in message:
        return ToolError(
            "That statement is not allowed here: no ATTACH, VACUUM, PRAGMA settings, transactions or extensions."
        )
    if "database or disk is full" in message:
        return ToolError(f"The workspace is full ({MAX_DB_BYTES // 1_000_000} MB). Drop a table it no longer needs.")
    return ToolError(f"SQLite error: {message}")


# =====================================================================
# Formatting
# =====================================================================


def _cell(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, float):
        text = format(value, ".10g")
    elif isinstance(value, bytes):
        text = f"<{len(value)} bytes>"
    else:
        text = str(value)
    text = re.sub(r"\s+", " ", text).replace("|", "/")
    return text if len(text) <= MAX_CELL_CHARS else text[: MAX_CELL_CHARS - 1] + "…"


def _table_text(columns: list[str], rows: Iterable[tuple], *, budget: int = MAX_REPLY_CHARS) -> tuple[str, int]:
    """A pipe table of the rows that fit in `budget` characters, and how many rows that was."""
    lines = ["| " + " | ".join(_cell(c) for c in columns) + " |", "|" + " --- |" * len(columns)]
    size = sum(len(line) + 1 for line in lines)
    shown = 0
    for row in rows:
        line = "| " + " | ".join(_cell(v) for v in row) + " |"
        if size + len(line) + 1 > budget:
            break
        lines.append(line)
        size += len(line) + 1
        shown += 1
    return "\n".join(lines), shown


def _number(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        if value.is_integer() and abs(value) < 1e15:
            return f"{int(value):,}"
        return f"{value:,.4g}" if abs(value) >= 1e6 else f"{value:,.6g}"
    if isinstance(value, int):
        return f"{value:,}"
    return _cell(value)


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


# =====================================================================
# Tools
# =====================================================================


def _user_tables(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return [
        (row[0], row[1])
        for row in conn.execute(
            "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY type, name"
        )
    ]


def _columns(conn: sqlite3.Connection, table: str) -> list[tuple[str, str]]:
    return [(row[1], row[2] or "") for row in conn.execute(f"PRAGMA table_info({_quote(table)})")]


_EMPTY = (
    "The data workspace is empty. Load a CSV, TSV or JSON file from the project with load_file, "
    "or create a table with run_sql."
)


def list_tables(arguments: dict) -> str:
    conn = _open(scope_of(arguments), create=False)
    if conn is None:
        return _EMPTY
    with contextlib.closing(conn):
        _read_only(conn)
        tables = _user_tables(conn)
        if not tables:
            return _EMPTY
        blocks = []
        for name, kind in tables[:MAX_TABLES_LISTED]:
            columns = ", ".join(f"{c} {t}".strip() for c, t in _columns(conn, name))
            if kind == "view":
                blocks.append(f"- {name} (view): {columns}")
                continue
            try:
                count = conn.execute(f"SELECT count(*) FROM {_quote(name)}").fetchone()[0]
                blocks.append(f"- {name} ({count:,} rows): {columns}")
            except sqlite3.OperationalError:
                blocks.append(f"- {name}: {columns}")
        more = f"\n[... {len(tables) - MAX_TABLES_LISTED} more]" if len(tables) > MAX_TABLES_LISTED else ""
        text = f"{len(tables)} table{'s' if len(tables) != 1 else ''} in the data workspace:\n" + "\n".join(blocks) + more
    return text[:MAX_REPLY_CHARS]


def _existing_table(conn: sqlite3.Connection, raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ToolError("Give the 'table' to work on (list_tables shows them).")
    wanted = raw.strip().strip('"[]`')
    for name, _ in _user_tables(conn):
        if name.lower() == wanted.lower():
            return name
    names = ", ".join(n for n, _ in _user_tables(conn)[:20]) or "none"
    raise ToolError(f"There is no table named '{wanted}'. Tables: {names}.")


def describe_table(arguments: dict) -> str:
    conn = _open(scope_of(arguments), create=False)
    if conn is None:
        return _EMPTY
    with contextlib.closing(conn):
        _read_only(conn)
        table = _existing_table(conn, arguments.get("table"))
        quoted = _quote(table)
        try:
            rows = conn.execute(f"SELECT count(*) FROM {quoted}").fetchone()[0]
            columns = _columns(conn, table)
            blocks = [f"Table {table}: {rows:,} rows, {len(columns)} columns."]
            for name, declared in columns[:MAX_DESCRIBED_COLUMNS]:
                blocks.append(_describe_column(conn, quoted, name, declared, rows))
        except sqlite3.Error as exc:
            raise _sql_error(exc, QUERY_SECONDS) from None
        if len(columns) > MAX_DESCRIBED_COLUMNS:
            blocks.append(f"[... {len(columns) - MAX_DESCRIBED_COLUMNS} more columns not described]")
    text = "\n".join(blocks)
    return text if len(text) <= MAX_REPLY_CHARS else text[: MAX_REPLY_CHARS - 1] + "…"


_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?")


def _describe_column(conn: sqlite3.Connection, table: str, name: str, declared: str, rows: int) -> str:
    column = _quote(name)
    empty, distinct, numeric = conn.execute(
        f"SELECT count(*) - count({column}), count(DISTINCT {column}), "
        f"coalesce(sum(typeof({column}) IN ('integer', 'real')), 0) FROM {table}"
    ).fetchone()
    filled = rows - empty
    label = f"{name} ({declared or 'any type'})"
    base = f"{empty:,} empty, {distinct:,} distinct"
    if filled and numeric >= 0.5 * filled:
        low, high, mean, total, middle, spread = conn.execute(
            f"SELECT min({column}), max({column}), avg({column}), sum({column}), median({column}), stdev({column}) "
            f"FROM {table} WHERE typeof({column}) IN ('integer', 'real')"
        ).fetchone()
        stats = (
            f"min {_number(low)}, max {_number(high)}, mean {_number(mean)}, median {_number(middle)}, "
            f"stdev {_number(spread)}, total {_number(total)}"
        )
        odd = f"; {filled - numeric:,} non-numeric values" if numeric < filled else ""
        return f"- {label}: {base}; {stats}{odd}"
    if not filled:
        return f"- {label}: all {rows:,} empty"
    sample = [r[0] for r in conn.execute(f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL LIMIT 50")]
    if sample and all(isinstance(v, str) and _ISO_DATE.match(v) for v in sample):
        first, last = conn.execute(f"SELECT min({column}), max({column}) FROM {table}").fetchone()
        return f"- {label}: {base}; dates from {_cell(first)} to {_cell(last)}"
    top = conn.execute(
        f"SELECT {column}, count(*) AS n FROM {table} WHERE {column} IS NOT NULL "
        f"GROUP BY {column} ORDER BY n DESC LIMIT 5"
    ).fetchall()
    common = ", ".join(f"{_cell(value)} ({count:,})" for value, count in top)
    return f"- {label}: {base}; most common: {common}"


# ---- load_file -----------------------------------------------------------


def _identifier(raw: str, fallback: str) -> str:
    """A column or table name that needs no quoting: lower case, letters, digits and _ (Thai kept)."""
    chars = []
    for char in unicodedata.normalize("NFC", str(raw)).strip().lower():
        if char.isalnum() or char == "_" or unicodedata.category(char).startswith("M"):
            chars.append(char)
        else:
            chars.append("_")
    name = re.sub(r"_+", "_", "".join(chars)).strip("_")[:60]
    if not name:
        name = fallback
    if name[0].isdigit():
        name = f"c_{name}"
    if name in _RESERVED:
        name += "_"
    return name


def _unique(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    result = []
    for name in names:
        candidate = name
        while candidate in seen:
            seen[name] += 1
            candidate = f"{name}_{seen[name]}"
        seen.setdefault(name, 1)
        seen.setdefault(candidate, 1)
        result.append(candidate)
    return result


_INT = re.compile(r"^[+-]?\d+$")
_THOUSANDS = re.compile(r"^[+-]?\d{1,3}(,\d{3})+(\.\d+)?$")


def parse_number(text: str) -> Optional[int | float]:
    """'1,234.50' / '฿1,200' / '-3' / '2.5e3' -> a number; anything else -> None."""
    value = text.strip()
    if value[:1] in _CURRENCY:
        value = value[1:].strip()
    elif value[:1] in "+-" and value[1:2] in _CURRENCY:
        value = value[0] + value[2:].strip()
    if _THOUSANDS.match(value):
        value = value.replace(",", "")
    if _INT.match(value):
        digits = value.lstrip("+-")
        if len(digits) > 1 and digits.startswith("0"):
            return None  # a code, a phone number or a postcode: keep its zeros as text
        if len(digits) > 18:
            return None
        return int(value)
    if not re.match(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$", value):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


class _ColumnKind:
    """What a column turned out to hold, from every value in it."""

    def __init__(self) -> None:
        self.ints = self.floats = self.texts = 0

    def see(self, value: Any) -> None:
        if value is None:
            return
        if isinstance(value, bool):  # JSON true/false: SQLite's own TRUE/FALSE are 1/0
            self.ints += 1
        elif isinstance(value, int):
            self.ints += 1
        elif isinstance(value, float):
            self.floats += 1
        elif isinstance(value, str):
            if value.strip().lower() in _MISSING:
                return
            number = parse_number(value)
            if number is None:
                self.texts += 1
            elif isinstance(number, int):
                self.ints += 1
            else:
                self.floats += 1
        else:
            self.texts += 1

    @property
    def sql_type(self) -> str:
        if self.texts or not (self.ints or self.floats):
            return "TEXT"
        return "REAL" if self.floats else "INTEGER"


def _convert(value: Any, sql_type: str) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return int(value) if sql_type != "TEXT" else ("true" if value else "false")
    if sql_type == "TEXT":
        text = str(value)
        return None if text == "" else text
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    text = str(value)
    if text.strip().lower() in _MISSING:
        return None
    number = parse_number(text)
    if number is None:
        return text
    return float(number) if sql_type == "REAL" else number


def _detect_encoding(sample: bytes) -> str:
    try:
        # A sample can end inside a multi-byte character; that is not a reason to doubt UTF-8.
        sample.decode("utf-8-sig") if len(sample) < 65536 else sample[:-4].decode("utf-8-sig")
        return "utf-8-sig"
    except UnicodeDecodeError:
        pass
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(sample).best()
        if best is not None and best.encoding:
            return best.encoding
    except Exception:  # noqa: BLE001
        pass
    return "cp874"  # Thai Windows, what Excel writes on a Thai PC


def _delimiter(sample: str, requested: Any, extension: str) -> str:
    if isinstance(requested, str) and requested:
        value = {"tab": "\t", "\\t": "\t", "comma": ",", "semicolon": ";", "pipe": "|"}.get(requested.lower(), requested)
        if len(value) != 1:
            raise ToolError("'delimiter' must be one character, or 'tab'.")
        return value
    if extension in (".tsv", ".tab"):
        return "\t"
    try:
        return csv.Sniffer().sniff(sample[:65536], delimiters=",;\t|").delimiter
    except csv.Error:
        return ","


def _json_records(real: str, kind: str) -> list[dict]:
    if os.path.getsize(real) > MAX_JSON_BYTES:
        raise ToolError(f"JSON files over {MAX_JSON_BYTES // 1_000_000} MB are not loaded; save it as CSV instead.")
    with open(real, "rb") as handle:
        raw = handle.read()
    text = raw.decode(_detect_encoding(raw[:200_000]), errors="replace")
    try:
        if kind == "jsonl":
            data: Any = [json.loads(line) for line in text.splitlines() if line.strip()]
        else:
            data = json.loads(text)
    except ValueError as exc:
        raise ToolError(f"The file is not valid JSON ({exc}).") from None
    if isinstance(data, dict):  # {"data": [...]} or {"rows": [...]}: the first list of records in it
        data = next((v for v in data.values() if isinstance(v, list)), [data])
    if not isinstance(data, list) or not data:
        raise ToolError("The JSON holds no list of records to load.")
    records = [item if isinstance(item, dict) else {"value": item} for item in data]
    if len(records) > MAX_ROWS_LOADED:
        raise ToolError(f"The file has {len(records):,} records; at most {MAX_ROWS_LOADED:,} are loaded.")
    return records


def load_file(arguments: dict) -> str:
    scope = scope_of(arguments)
    try:
        real, label = fs.resolve(arguments.get("path"))
    except fs.FsError as exc:
        raise ToolError(str(exc)) from None
    if fs._is_secret_path(real, is_dir=False):  # same rule as local_fs: .env, keys, ...
        raise ToolError(f"'{label}' is private and cannot be loaded.")
    if not os.path.isfile(real):
        raise ToolError(f"'{label}' does not exist or is not a file. list_directory shows what is there.")
    extension = os.path.splitext(real)[1].lower()
    kind = _LOADABLE.get(extension)
    if kind is None:
        hint = " Save it as CSV first." if extension in (".xlsx", ".xls", ".ods", ".numbers") else ""
        raise ToolError(f"'{label}' is not a file this tool loads (CSV, TSV, TXT, JSON, JSONL).{hint}")
    size = os.path.getsize(real)
    if size > MAX_FILE_BYTES:
        raise ToolError(f"'{label}' is {size / 1e6:.0f} MB; files over {MAX_FILE_BYTES // 1_000_000} MB are not loaded.")

    started = time.monotonic()
    notes: list[str] = []
    if kind == "csv":
        with open(real, "rb") as handle:
            sample_bytes = handle.read(65536)
        encoding = _detect_encoding(sample_bytes)
        sample = sample_bytes.decode(encoding, errors="replace")
        delimiter = _delimiter(sample, arguments.get("delimiter"), extension)
        header_flag = arguments.get("header")
        has_header = not (header_flag is False or str(header_flag).strip().lower() == "false")
        first = next(_filled_rows(real, encoding, delimiter, skip_first=False), None)
        if first is None:
            raise ToolError(f"'{label}' is empty.")

        def records() -> Iterator[list[str]]:
            return _filled_rows(real, encoding, delimiter, skip_first=has_header)

        width = len(first)
        kinds: list[_ColumnKind] = []
        count = ragged = 0
        # Pass 1: how many columns, and what each one holds. Pass 2 (the insert) reads the file again.
        for row in records():
            count += 1
            if count > MAX_ROWS_LOADED:
                raise ToolError(f"'{label}' has more than {MAX_ROWS_LOADED:,} rows, which is more than this tool loads.")
            if len(row) != width:
                ragged += 1
                width = max(width, len(row))
            while len(kinds) < min(width, MAX_COLUMNS):
                kinds.append(_ColumnKind())
            for index, value in enumerate(row[:MAX_COLUMNS]):
                kinds[index].see(value)
            if time.monotonic() - started > LOAD_SECONDS / 2:
                raise ToolError(f"'{label}' is too big to load within the time limit. Load a smaller file.")
        width = min(width, MAX_COLUMNS)
        while len(kinds) < width:
            kinds.append(_ColumnKind())
        raw_names = (first if has_header else []) + [""] * width
        source_names = [str(n).strip() for n in raw_names[:width]]
        names = _unique([_identifier(n, f"column_{i + 1}") for i, n in enumerate(source_names)])
        types = [k.sql_type for k in kinds[:width]]
        data_rows: Callable[[], Iterator[list[Any]]] = lambda: (
            [_convert(v, types[i]) for i, v in enumerate((row + [None] * width)[:width])] for row in records()
        )
        notes.append(f"encoding {encoding.replace('-sig', '')}, delimiter {repr(delimiter)}")
        if ragged:
            notes.append(f"{ragged:,} rows had a different number of fields from the first row")
    else:
        records_list = _json_records(real, kind)
        keys: list[str] = []
        for record in records_list:
            for key in record:
                if key not in keys:
                    keys.append(key)
        if len(keys) > MAX_COLUMNS:
            notes.append(f"only the first {MAX_COLUMNS} of {len(keys)} fields were loaded")
            keys = keys[:MAX_COLUMNS]
        kinds = [_ColumnKind() for _ in keys]
        for record in records_list:
            for index, key in enumerate(keys):
                value = record.get(key)
                kinds[index].see(json.dumps(value) if isinstance(value, (dict, list)) else value)
        source_names = [str(k) for k in keys]
        names = _unique([_identifier(k, f"column_{i + 1}") for i, k in enumerate(keys)])
        types = [k.sql_type for k in kinds]
        width = len(keys)
        count = len(records_list)
        data_rows = lambda: ([_convert(record.get(key), types[i]) for i, key in enumerate(keys)] for record in records_list)

    if not width or not count:
        raise ToolError(f"'{label}' has no data rows to load.")
    requested = arguments.get("table")
    stem = os.path.splitext(os.path.basename(real))[0]
    table = _identifier(requested if isinstance(requested, str) and requested.strip() else stem, "data")
    if table.startswith("sqlite_"):
        table = "t_" + table

    conn = _open(scope, create=True)
    assert conn is not None
    with contextlib.closing(conn):
        existing = {n.lower(): n for n, _ in _user_tables(conn)}
        if table.lower() in existing:
            raise ToolError(
                f"A table named '{existing[table.lower()]}' already exists, and load_file never overwrites. "
                f"Load it under another name (table: '{table}_2'), or drop the old one with run_sql."
            )
        _deadline(conn, max(1.0, LOAD_SECONDS - (time.monotonic() - started)))
        columns_sql = ", ".join(f"{_quote(n)} {t}" for n, t in zip(names, types))
        placeholders = ", ".join("?" for _ in names)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(f"CREATE TABLE {_quote(table)} ({columns_sql})")
            batch: list[list[Any]] = []
            for row in data_rows():
                batch.append(row)
                if len(batch) >= 2000:
                    conn.executemany(f"INSERT INTO {_quote(table)} VALUES ({placeholders})", batch)
                    batch = []
            if batch:
                conn.executemany(f"INSERT INTO {_quote(table)} VALUES ({placeholders})", batch)
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise _sql_error(exc, LOAD_SECONDS) from None

    described = []
    for name, sql_type, source in zip(names, types, source_names):
        renamed = f" (from '{source}')" if source and source != name else ""
        described.append(f"{name} {sql_type}{renamed}")
    text = (
        f"Loaded {count:,} rows from {label} into table {table}"
        + (f" ({'; '.join(notes)})" if notes else "")
        + f".\nColumns: {', '.join(described)}.\n"
        + f"Next: describe_table for an overview, or query, e.g. SELECT count(*) FROM {table}."
    )
    return text[:MAX_REPLY_CHARS]


def _filled_rows(real: str, encoding: str, delimiter: str, *, skip_first: bool) -> Iterator[list[str]]:
    """The file's rows minus blank ones; minus the first (the header) when asked. Closes the file when done."""
    with open(real, "r", encoding=encoding, errors="replace", newline="") as handle:
        skipped = not skip_first
        for row in csv.reader(handle, delimiter=delimiter):
            if not any(cell.strip() for cell in row):
                continue
            if not skipped:
                skipped = True
                continue
            yield row


# ---- query / run_sql -------------------------------------------------------


def _statements(sql: str) -> list[str]:
    """Splits a script into complete statements (quotes, comments and trigger bodies respected)."""
    statements: list[str] = []
    start = 0
    position = sql.find(";")
    while position != -1:
        candidate = sql[start: position + 1]
        if sqlite3.complete_statement(candidate):
            if candidate.strip(" \t\r\n;"):
                statements.append(candidate.strip())
            start = position + 1
        position = sql.find(";", position + 1)
    tail = sql[start:].strip()
    if tail and re.sub(r"--[^\n]*|/\*.*?\*/", "", tail, flags=re.S).strip():
        statements.append(tail)
    return statements


def _first_word(statement: str) -> str:
    stripped = re.sub(r"^(\s|--[^\n]*\n|/\*.*?\*/)+", "", statement, flags=re.S)
    match = re.match(r"[A-Za-z]+", stripped)
    return match.group(0).lower() if match else ""


def _sql_argument(arguments: dict) -> str:
    sql = arguments.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        raise ToolError("Give the 'sql' to run.")
    if len(sql) > MAX_SQL_CHARS:
        raise ToolError(f"The SQL is too long ({len(sql):,} characters, at most {MAX_SQL_CHARS:,}).")
    return sql


def query(arguments: dict) -> str:
    sql = _sql_argument(arguments)
    statements = _statements(sql)
    if len(statements) != 1:
        raise ToolError("query runs exactly one SELECT statement. Send them one at a time.")
    statement = statements[0].rstrip(";").strip()
    if _first_word(statement) not in ("select", "with", "values", "explain", "pragma"):
        raise ToolError("query only reads (SELECT / WITH ... SELECT). To change data, use run_sql.")
    try:
        limit = int(arguments.get("max_rows") or DEFAULT_ROWS)
    except (TypeError, ValueError):
        limit = DEFAULT_ROWS
    limit = max(1, min(MAX_ROWS, limit))

    conn = _open(scope_of(arguments), create=False)
    if conn is None:
        return _EMPTY
    with contextlib.closing(conn):
        _read_only(conn)
        try:
            cursor = conn.execute(statement)
            columns = [d[0] for d in cursor.description or []]
            rows = cursor.fetchmany(limit + 1)
        except sqlite3.Error as exc:
            raise _sql_error(exc, QUERY_SECONDS) from None
    if not columns:
        return "The statement returned nothing."
    more = len(rows) > limit
    rows = rows[:limit]
    if not rows:
        return f"No rows. (Columns: {', '.join(columns)}.)"
    table, shown = _table_text(columns, rows, budget=MAX_REPLY_CHARS - 200)
    header = f"{shown} row{'s' if shown != 1 else ''}"
    if shown < len(rows) or more:
        header += " shown; more exist - add LIMIT/WHERE, or aggregate, to see what matters"
    return f"{header}:\n{table}"


def run_sql(arguments: dict) -> str:
    sql = _sql_argument(arguments)
    statements = _statements(sql)
    if not statements:
        raise ToolError("There is no SQL statement in 'sql'.")
    if len(statements) > MAX_STATEMENTS:
        raise ToolError(f"{len(statements)} statements; at most {MAX_STATEMENTS} per call.")
    for index, statement in enumerate(statements, start=1):
        word = _first_word(statement)
        if word in ("select", "values", "explain"):
            raise ToolError(f"Statement {index} only reads; use query for that. Nothing was run.")
        if word in ("attach", "detach", "pragma", "vacuum", "begin", "commit", "end", "rollback", "savepoint", "release"):
            raise ToolError(f"Statement {index} ({word.upper()}) is not allowed here. Nothing was run.")

    conn = _open(scope_of(arguments), create=True)
    assert conn is not None
    results: list[str] = []
    with contextlib.closing(conn):
        _deadline(conn, QUERY_SECONDS)
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error:
            raise ToolError("The data workspace is busy with another change. Try again in a moment.") from None
        try:
            conn.set_authorizer(_authorizer(_WRITE_ACTIONS))
            for index, statement in enumerate(statements, start=1):
                try:
                    cursor = conn.execute(statement)
                except sqlite3.Error as exc:
                    error = _sql_error(exc, QUERY_SECONDS)
                    raise ToolError(f"Statement {index} failed - {error}. Nothing was changed.") from None
                word = _first_word(statement).upper()
                if cursor.rowcount is not None and cursor.rowcount >= 0 and word in ("INSERT", "UPDATE", "DELETE", "REPLACE", "WITH"):
                    results.append(f"{index}. {word}: {cursor.rowcount:,} row{'s' if cursor.rowcount != 1 else ''}")
                else:
                    results.append(f"{index}. {word}: done")
            conn.set_authorizer(None)
            conn.execute("COMMIT")
        except BaseException:
            conn.set_authorizer(None)
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    return "Done.\n" + "\n".join(results)


# =====================================================================
# The server
# =====================================================================

INSTRUCTIONS = (
    "A private local SQLite workspace for crunching numbers: load CSV, TSV or JSON data files from the project, "
    "then answer with SQL (totals, averages, medians, group-bys) instead of estimating."
)

TOOLS = [
    {
        "name": "list_tables",
        "title": "List data tables",
        "description": (
            "Lists the tables in the person's private data workspace (a local SQLite database), with their "
            "columns and row counts. Use it first when a question involves data loaded earlier, or to find a "
            "table's exact name before a query."
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "describe_table",
        "title": "Profile a data table",
        "description": (
            "Profiles one table in the data workspace: for every column its empty and distinct values, and min, "
            "max, mean, median, standard deviation and total for numbers, the date range for dates, or the most "
            "common values for text. Use it for a quick overview of a dataset, when asked to analyse or summarise "
            "a table, or before writing a query."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"table": {"type": "string", "description": "The table's name, as list_tables shows it."}},
            "required": ["table"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "load_file",
        "title": "Load a data file",
        "description": (
            "Loads a CSV, TSV or JSON data file from the project folder into a new table in the person's data "
            "workspace, cleaning the column names and detecting number columns. Use it when they want numbers in "
            "a file counted, totalled, averaged, compared or ranked - then answer with query, not mental arithmetic. "
            "The path is relative to the project, as list_directory shows it (an attached file is under uploads/). "
            "It never overwrites: if the table name is taken, choose another. Excel files must be saved as CSV first."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The file, relative to the project folder, e.g. 'data/sales.csv'."},
                "table": {"type": "string", "description": "Name for the new table. Default: from the file name."},
                "delimiter": {"type": "string", "description": "For CSV/TXT only: ',', ';', '|' or 'tab'. Default: detected."},
                "header": {"type": "boolean", "description": "For CSV/TXT only: false if the first row is data, not column names. Default true."},
            },
            "required": ["path"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    },
    {
        "name": "query",
        "title": "Query data",
        "description": (
            "Runs ONE read-only SQL SELECT (SQLite dialect) on the person's data workspace and returns the rows as "
            "a table. Use it to calculate rather than estimate: totals, averages, counts, group-bys, top-N, joins, "
            "date ranges. Besides SQLite's functions it has median(x), stdev(x), variance(x) and percentile(x, p) "
            "(p from 0 to 100); text dates work with date(), strftime() and julianday(). Aggregate in SQL instead of "
            "pulling many rows. It cannot change anything."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "One SELECT (or WITH ... SELECT) statement."},
                "max_rows": {"type": "integer", "description": f"Rows to return, 1 to {MAX_ROWS}. Default {DEFAULT_ROWS}."},
            },
            "required": ["sql"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "run_sql",
        "title": "Change data",
        "description": (
            "Changes the person's data workspace with SQL (SQLite dialect): CREATE TABLE/VIEW/INDEX, INSERT, "
            "UPDATE, DELETE, DROP, ALTER. Several statements separated by semicolons run together, all or nothing. "
            "Use it to build a small table from numbers the person gives you, to fix values, or to remove a table "
            "they no longer want. The person is asked before it runs. For reading, use query."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"sql": {"type": "string", "description": "One or more statements separated by semicolons."}},
            "required": ["sql"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
    },
]

SERVER = Server(
    name="jarvis-data",
    version="1.0.0",
    instructions=INSTRUCTIONS,
    tools=TOOLS,
    handlers={
        "list_tables": list_tables,
        "describe_table": describe_table,
        "load_file": load_file,
        "query": query,
        "run_sql": run_sql,
    },
    log_prefix="mcp-data",
)


if __name__ == "__main__":
    SERVER.serve()
