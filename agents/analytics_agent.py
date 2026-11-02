"""
The Head of Analytics: Excel spreadsheets (.xlsx) built from tabular data.

    manager (llm_brain) --delegate_to_analytics(tabular_data)--> AnalyticsAgent
        -> the JSON read and checked against the shapes below (parse_table)
        -> a pandas DataFrame, written by the openpyxl engine: one sheet, a bold header row
           that stays in view while scrolling, a filter on it, columns as wide as their text
        -> saved as artifacts/spreadsheet_YYYYMMDD_HHMMSS.xlsx, opened again and checked
        <- "SUCCESS: ...\nPath: <absolute path>\nURL: /artifacts/<file name>"
           or "ERROR: <what was wrong and what to change>"

In the web chat the brain swaps the Path and URL lines for a Markdown download link,
[Download Spreadsheet](/artifacts/<file name>) (markdown_link): api_server.py serves the
folder at that path, and the web app hands the file to the person whose chat shows the
link (TripPlanner_Web/app/artifacts/[name]).

NOT A LANGUAGE-MODEL LOOP, like the Heads of Creative and Publishing. The manager works
out the data - with the Head of Research when it needs facts - and writes the JSON; this
lays it out. No API key, no network, nothing billed.

THE DATA. One JSON value, as a string or as the object itself, in one of two shapes:

    columns  {"Month": ["Jan", "Feb"], "Revenue (THB)": [1200, 1350]}
             column name -> its values, top to bottom; every list the same length
    rows     [{"Month": "Jan", "Revenue (THB)": 1200}, {"Month": "Feb", "Revenue (THB)": 1350}]
             one object per row; the columns in the order they first appear, and a
             column a row leaves out is empty in that row

Either may be wrapped to name the sheet: {"sheet_name": "Q1 sales", "data": <either shape>}.
A header list with rows of values, {"columns": ["Month", "Revenue"], "rows": [["Jan", 1200]]},
is read too: models often write it, and it cannot be mistaken for a table of columns.
A cell is text, a number, true/false or null (empty). Anything else - a list or an object
in a cell, a column name that is empty or used twice, columns of different lengths - is
an ERROR naming the place and what to change, so the manager can fix the data and call
again. A ```json fence around the string is ignored.

TEXT STAYS TEXT. A cell that starts with "=" is saved as text, not as a formula: the data
is written by a model that may have read web pages, and a formula in a file someone opens
in Excel can fetch web addresses or start programs (formula injection). The manager works
out totals itself (ANALYTICS_RULE in llm_brain.py), and the SUCCESS answer says how many
cells were kept as text. Numbers written as text ("1,200", "฿500") stay text too: nothing
here guesses.

VERIFIED. The saved file is opened again with openpyxl: its sheet must have exactly the
header and the number of rows asked for, or the file is deleted and the answer is an
ERROR. A file is never overwritten (_2, _3... when two land in the same second), and the
same table asked for again within SAME_REQUEST_SECONDS returns the file already made.

NEVER RAISES. Every failure comes back as one sentence starting "ERROR:".

    python -m agents.analytics_agent --check        a test table in memory, the folder
    python -m agents.analytics_agent table.json     builds one spreadsheet from a JSON file
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from .creative_agent import ARTIFACTS_DIR, web_url

try:
    import openpyxl
    import pandas as pd
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    _IMPORT_ERROR = ""
except ImportError as exc:  # pragma: no cover - requirements.txt has both; an old environment may not
    pd = None  # type: ignore[assignment]
    openpyxl = None  # type: ignore[assignment]
    _IMPORT_ERROR = str(exc)

# What an .xlsx is. api_server.py registers it for the files it serves: Python's own table
# has no entry for it on Linux (the Docker image), where it would go out as
# application/octet-stream - and the web app refuses a file whose type is not its name's.
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
FILE_PREFIX = "spreadsheet"
DOWNLOAD_LABEL = "Download Spreadsheet"

# Room for MAX_ROWS rows of a few columns (a JSON file from the command line); a model's tool
# call is far smaller, held to the brain's output limit.
MAX_INPUT_CHARS = 5_000_000
MAX_ROWS = 10_000
MAX_COLUMNS = 100
MAX_HEADER_CHARS = 255
MAX_CELL_CHARS = 32_767  # Excel's own limit for one cell
SAME_REQUEST_SECONDS = 120
DEFAULT_SHEET = "Sheet1"

ENVELOPE_FIELDS = ("sheet_name", "data")
SHAPES = (
    'tabular_data must be either an object of columns, {"Column": [value, ...], ...}, or a list of rows, '
    '[{"Column": value, ...}, ...] - optionally wrapped as {"sheet_name": "...", "data": <either>}'
)

# Column widths, in characters: room for the longest value, within reason.
MIN_WIDTH = 8
MAX_WIDTH = 60

# Characters Excel's file format cannot hold (openpyxl refuses them): control characters
# other than tab, line feed and carriage return.
_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")
# Characters a sheet name may not contain, and its limit.
_SHEET_FORBIDDEN = re.compile(r"[\[\]:*?/\\]")
MAX_SHEET_CHARS = 31
_FENCE = re.compile(r"^```[a-zA-Z]*\s*(.*?)\s*```$", re.DOTALL)
# CJK characters (and others that are drawn twice as wide) need two characters of column width.
_WIDE = re.compile("[ᄀ-ᅟ⺀-꓏가-힣豈-﫿︰-﹏＀-｠￠-￦]")


class TableError(Exception):
    """The data cannot become a spreadsheet. The message is one sentence for the manager: what is wrong, what to change."""


@dataclass(frozen=True)
class Table:
    sheet: str
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps([self.sheet, self.columns, self.rows], ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest()


# ---------------------------------------------------------------------------------------
# Reading the data
# ---------------------------------------------------------------------------------------


def parse_table(data: Any) -> Table:
    """`data` (a JSON string, or the object itself) as a Table. Raises TableError."""
    value = _load(data)
    sheet = DEFAULT_SHEET
    if _is_envelope(value):
        sheet = _sheet_name(value.get("sheet_name"))
        value = value["data"]
    if _is_header_and_rows(value):
        columns, rows = _from_header_and_rows(value)
    elif isinstance(value, dict):
        columns, rows = _from_columns(value)
    elif isinstance(value, list):
        columns, rows = _from_rows(value)
    else:
        raise TableError(f"{SHAPES}, not {_kind(value)}.")
    if len(columns) > MAX_COLUMNS:
        raise TableError(f"the table has {len(columns)} columns; at most {MAX_COLUMNS} fit one sheet here - leave some out.")
    if not rows:
        raise TableError("the table has no rows - send at least one row of values.")
    if len(rows) > MAX_ROWS:
        raise TableError(f"the table has {len(rows):,} rows; at most {MAX_ROWS:,} are made into one spreadsheet - send fewer.")
    return Table(sheet=sheet, columns=tuple(columns), rows=tuple(tuple(row) for row in rows))


def _load(data: Any) -> Any:
    if isinstance(data, (dict, list)):
        return data
    if not isinstance(data, str):
        raise TableError(f"{SHAPES}, sent as a JSON string; got {_kind(data)}.")
    text = data.strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    if not text:
        raise TableError(f"tabular_data is empty - {SHAPES}.")
    if len(text) > MAX_INPUT_CHARS:
        raise TableError(f"tabular_data is {len(text):,} characters long; send at most {MAX_INPUT_CHARS:,}.")
    try:
        # strict=False: a line break typed straight into a text value (models do) is read as one.
        return json.loads(text, strict=False, parse_constant=_no_constant)
    except TableError:
        raise
    except json.JSONDecodeError as exc:
        raise TableError(
            f"tabular_data is not valid JSON ({exc.msg} at line {exc.lineno}, column {exc.colno}) - send one JSON object or list."
        ) from exc


def _no_constant(name: str) -> Any:
    raise TableError(f"{name} is not a value a cell can hold - use a number, or null for an empty cell.")


def _is_envelope(value: Any) -> bool:
    """{"sheet_name": "...", "data": <a table>}: told apart from a table that has a column called "data"."""
    if not isinstance(value, dict) or "data" not in value or not set(value) <= set(ENVELOPE_FIELDS):
        return False
    inner = value["data"]
    return isinstance(inner, dict) or (isinstance(inner, list) and all(isinstance(item, dict) for item in inner))


def _sheet_name(value: Any) -> str:
    if value is None:
        return DEFAULT_SHEET
    if not isinstance(value, str):
        raise TableError(f"sheet_name must be text; got {_kind(value)}.")
    # Excel refuses these characters in a sheet's name, and names longer than 31 characters.
    name = _SHEET_FORBIDDEN.sub(" ", _ILLEGAL.sub("", value))
    name = " ".join(name.split()).strip("'")[:MAX_SHEET_CHARS].strip()
    return name or DEFAULT_SHEET


def _header(name: Any, where: str) -> str:
    if not isinstance(name, str):
        raise TableError(f"{where}: a column name must be text; got {_kind(name)}.")
    clean = " ".join(_ILLEGAL.sub("", name).split())
    if not clean:
        raise TableError(f"{where}: a column name is empty - give every column a name.")
    if len(clean) > MAX_HEADER_CHARS:
        raise TableError(f"{where}: the column name {clean[:40]!r}... is longer than {MAX_HEADER_CHARS} characters - shorten it.")
    return clean


def _from_columns(table: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    if not table:
        raise TableError(f"the object has no columns - {SHAPES}.")
    columns: list[str] = []
    values: list[list[Any]] = []
    for raw_name, column in table.items():
        name = _header(raw_name, f"column {raw_name!r}")
        if name in columns:
            raise TableError(f"two columns are named {name!r} - give each column its own name.")
        if not isinstance(column, list):
            raise TableError(
                f"column {name!r} holds {_kind(column)}, not a list of values - in the columns shape every column is "
                'a list, {"Column": [value, ...]}; for one object per row, send a list of rows instead.'
            )
        columns.append(name)
        values.append([_cell(cell, f"column {name!r}, value {number}") for number, cell in enumerate(column, start=1)])
    lengths = {len(column) for column in values}
    if len(lengths) > 1:
        longest = max(range(len(values)), key=lambda index: len(values[index]))
        shortest = min(range(len(values)), key=lambda index: len(values[index]))
        raise TableError(
            f"column {columns[shortest]!r} has {len(values[shortest])} values but {columns[longest]!r} has "
            f"{len(values[longest])} - every column needs one value per row (null for an empty cell)."
        )
    return columns, [list(row) for row in zip(*values)]


def _is_header_and_rows(value: Any) -> bool:
    """{"columns": ["A", "B"], "rows": [[1, 2], ...]}: the layout models often write; it cannot be a table of columns, whose cells are never lists."""
    return (
        isinstance(value, dict)
        and set(value) == {"columns", "rows"}
        and isinstance(value["columns"], list)
        and isinstance(value["rows"], list)
        and all(isinstance(row, list) for row in value["rows"])
    )


def _from_header_and_rows(table: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    columns: list[str] = []
    for number, raw_name in enumerate(table["columns"], start=1):
        name = _header(raw_name, f"column {number}")
        if name in columns:
            raise TableError(f"two columns are named {name!r} - give each column its own name.")
        columns.append(name)
    if not columns:
        raise TableError("columns is empty - name every column.")
    rows: list[list[Any]] = []
    for number, row in enumerate(table["rows"], start=1):
        if len(row) != len(columns):
            raise TableError(
                f"row {number} has {len(row)} values but there are {len(columns)} columns - every row needs one value "
                "per column (null for an empty cell)."
            )
        rows.append([_cell(cell, f"row {number}, column {name!r}") for cell, name in zip(row, columns)])
    return columns, rows


def _from_rows(table: list[Any]) -> tuple[list[str], list[list[Any]]]:
    if not table:
        raise TableError("the list of rows is empty - send at least one row.")
    columns: list[str] = []
    named: list[dict[str, Any]] = []
    for number, row in enumerate(table, start=1):
        if not isinstance(row, dict):
            raise TableError(
                f"row {number} is {_kind(row)}, not an object - each row is {{\"Column\": value, ...}}."
            )
        cells: dict[str, Any] = {}
        for raw_name, cell in row.items():
            name = _header(raw_name, f"row {number}")
            if name in cells:
                raise TableError(f"row {number} names the column {name!r} twice - give each column its own name.")
            cells[name] = _cell(cell, f"row {number}, column {name!r}")
            if name not in columns:
                columns.append(name)
        named.append(cells)
    if not columns:
        raise TableError("the rows have no columns - each row is {\"Column\": value, ...}.")
    return columns, [[cells.get(name) for name in columns] for cells in named]


def _cell(value: Any, where: str) -> Any:
    """A value a cell can hold: text (cleaned), a whole number, a finite number, true/false, or None."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TableError(f"{where} is not a finite number - use a number, or null for an empty cell.")
        return value
    if isinstance(value, str):
        text = _ILLEGAL.sub("", value)
        if len(text) > MAX_CELL_CHARS:
            raise TableError(f"{where} holds {len(text):,} characters; one cell holds at most {MAX_CELL_CHARS:,} - shorten it.")
        return text
    raise TableError(
        f"{where} holds {_kind(value)}; a cell holds text, a number, true/false or null - "
        "join a list into one text, or give each part its own column."
    )


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true/false"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, str):
        return "text"
    if isinstance(value, list):
        return "a list"
    if isinstance(value, dict):
        return "an object"
    return type(value).__name__


# ---------------------------------------------------------------------------------------
# Writing the workbook
# ---------------------------------------------------------------------------------------


def build_workbook(table: Table) -> tuple[bytes, int]:
    """The .xlsx file's bytes, and how many cells that looked like formulas were kept as text."""
    frame = pd.DataFrame(list(table.rows), columns=list(table.columns), dtype=object)
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        frame.to_excel(writer, sheet_name=table.sheet, index=False)
        sheet = writer.sheets[table.sheet]
        kept_as_text = _finish(sheet, table)
    return buffer.getvalue(), kept_as_text


def _finish(sheet: Any, table: Table) -> int:
    """Text stays text, the header stands out and stays in view, a filter, readable widths. Returns cells kept as text."""
    kept_as_text = 0
    for row in sheet.iter_rows():
        for cell in row:
            # openpyxl makes any text that starts with "=" a formula: keep it the text it was.
            if cell.data_type == "f":
                cell.data_type = "s"
                kept_as_text += 1
    # pandas writes an empty cell as empty text; Excel should see it as blank.
    for row_number, values in enumerate(table.rows, start=2):
        for column_number, value in enumerate(values, start=1):
            if value is None:
                sheet.cell(row=row_number, column=column_number).value = None
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    sheet.freeze_panes = "A2"
    last = get_column_letter(len(table.columns))
    sheet.auto_filter.ref = f"A1:{last}{len(table.rows) + 1}"
    for index, name in enumerate(table.columns):
        longest = max([_width(name)] + [_width(row[index]) for row in table.rows])
        sheet.column_dimensions[get_column_letter(index + 1)].width = min(MAX_WIDTH, max(MIN_WIDTH, longest + 2))
    return kept_as_text


def _width(value: Any) -> int:
    if value is None:
        return 0
    text = str(value).split("\n")
    line = max(text, key=len) if text else ""
    return len(line) + len(_WIDE.findall(line))


def _verify(path: Path, table: Table) -> str:
    """"" when the saved file opens with exactly this sheet, header and number of rows; else what is wrong."""
    try:
        workbook = openpyxl.load_workbook(path, read_only=True)
    except Exception as exc:  # noqa: BLE001 - a broken zip, bad XML
        return f"it does not open as a spreadsheet ({type(exc).__name__})"
    try:
        if workbook.sheetnames != [table.sheet]:
            return f"its sheets are {workbook.sheetnames} instead of [{table.sheet!r}]"
        rows = list(workbook[table.sheet].iter_rows(values_only=True))
    finally:
        workbook.close()
    if not rows or tuple("" if value is None else str(value) for value in rows[0]) != table.columns:
        return "its header row is not the columns asked for"
    if len(rows) - 1 != len(table.rows):
        return f"it has {len(rows) - 1} rows instead of {len(table.rows)}"
    return ""


# ---------------------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------------------


class AnalyticsAgent:
    """
    Builds one Excel spreadsheet per call from tabular data and saves it in artifacts/.
    Makes no network call; needs pandas and openpyxl.
    """

    name = "analytics"
    title = "Head of Analytics"
    tool_name = "delegate_to_analytics"

    def __init__(self, *, artifacts_dir: Optional[Path | str] = None, now: Optional[Callable[[], datetime]] = None) -> None:
        self.artifacts_dir = Path(artifacts_dir) if artifacts_dir else ARTIFACTS_DIR
        self._now = now or datetime.now
        self._lock = threading.Lock()  # the list of recent files, and saving
        self._recent: dict[str, tuple[float, Path]] = {}  # the table's fingerprint -> (when, file)

    @property
    def unavailable_reason(self) -> str:
        if pd is None or openpyxl is None:
            return f"pandas and openpyxl are not installed ({_IMPORT_ERROR})"
        return ""

    @property
    def available(self) -> bool:
        return not self.unavailable_reason

    def describe(self) -> str:
        if not self.available:
            return f"off ({self.unavailable_reason})"
        return f"pandas {pd.__version__} + openpyxl {openpyxl.__version__}; into {self.artifacts_dir}"

    def build_spreadsheet(self, tabular_data: Any) -> str:
        """Builds a spreadsheet from `tabular_data` and saves it. Returns SUCCESS + its path and URL, or ERROR + why."""
        if not self.available:
            return f"ERROR: spreadsheets are not set up ({self.unavailable_reason}). Tell the user; do not try again."
        try:
            table = parse_table(tabular_data)
        except TableError as err:
            print(f"[Agent:analytics] Not built: {_shown(str(err), 240)}")
            return f"ERROR: {err}"

        with self._lock:
            again = self._already_made(table.fingerprint)
            if again:
                return again
            started = time.monotonic()
            summary = _summary(table)
            print(f"[Agent:analytics] Building {summary}: {_shown(', '.join(table.columns), 120)}")
            try:
                content, kept_as_text = build_workbook(table)
            except Exception as exc:  # noqa: BLE001 - pandas/openpyxl refusing something unexpected
                print(f"[Agent:analytics] The spreadsheet failed: {type(exc).__name__}: {exc}")
                return f"ERROR: the spreadsheet could not be built ({type(exc).__name__}: {_shown(str(exc), 160)})."
            try:
                path = self._save(content)
            except OSError as exc:
                return f"ERROR: the spreadsheet was built but could not be saved in {self.artifacts_dir} ({exc})."

            problem = _verify(path, table)
            if problem:
                print(f"[Agent:analytics] {path.name} failed its check ({problem}); deleted.")
                path.unlink(missing_ok=True)
                return f"ERROR: the spreadsheet was saved but did not check out ({problem}), so it was deleted."

            self._recent[table.fingerprint] = (time.monotonic(), path)
            size_kb = max(1, round(path.stat().st_size / 1024))
            print(f"[Agent:analytics] Saved {path} ({size_kb} KB, {time.monotonic() - started:.2f} s).")
            return self._answer(path, table, summary, kept_as_text)

    @staticmethod
    def _answer(path: Path, table: Table, summary: str, kept_as_text: int) -> str:
        shown = list(table.columns[:12]) + (["..."] if len(table.columns) > 12 else [])
        lines = [
            f"SUCCESS: the spreadsheet was created and saved as an Excel file ({summary}).",
            f"Columns: {', '.join(shown)}",
            f"Path: {path}",
            f"URL: {web_url(path)}",
        ]
        if kept_as_text:
            lines.append(
                f"Note: {kept_as_text} cell{'s' if kept_as_text != 1 else ''} starting with = "
                f"{'were' if kept_as_text != 1 else 'was'} saved as text, not as formulas."
            )
        return "\n".join(lines)

    def _already_made(self, fingerprint: str) -> str:
        """The answer for the same table made moments ago (the file still there), or ""."""
        now = time.monotonic()
        for key, (when, _path) in list(self._recent.items()):
            if now - when > SAME_REQUEST_SECONDS:
                del self._recent[key]
        hit = self._recent.get(fingerprint)
        if hit is None or not hit[1].exists():
            return ""
        print(f"[Agent:analytics] Same table {now - hit[0]:.0f} s ago; not made again.")
        return (
            f"SUCCESS: this exact spreadsheet was already created {now - hit[0]:.0f} seconds ago, so it was not "
            f"made again.\nPath: {hit[1]}\nURL: {web_url(hit[1])}"
        )

    def _save(self, content: bytes) -> Path:
        """Writes a new artifacts/spreadsheet_YYYYMMDD_HHMMSS.xlsx (_2, _3... if taken) and returns its absolute path."""
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        stamp = self._now().strftime("%Y%m%d_%H%M%S")
        for number in range(1, 100):
            path = self.artifacts_dir / (f"{FILE_PREFIX}_{stamp}.xlsx" if number == 1 else f"{FILE_PREFIX}_{stamp}_{number}.xlsx")
            try:
                handle = open(path, "xb")  # claims the name; never overwrites
            except FileExistsError:
                continue
            try:
                with handle:
                    handle.write(content)
            except BaseException:
                path.unlink(missing_ok=True)
                raise
            return path.resolve()
        raise OSError(f"no free file name for {FILE_PREFIX}_{stamp}")

    # -- setup check (python -m agents.analytics_agent --check) ----------------

    def check(self) -> list[str]:
        """Lines on the setup: pandas/openpyxl and a test table built in memory, the folder. Saves nothing."""
        lines = [f"Spreadsheets: {self.describe()}"]
        if self.available:
            try:
                sample = parse_table({"Item": ["Flights", "Hotel", "ทดสอบภาษาไทย"], "Cost (THB)": [12000, 8400.5, None]})
                content, _ = build_workbook(sample)
                lines.append(f"  test spreadsheet: built in memory ({max(1, len(content) // 1024)} KB)")
            except Exception as exc:  # noqa: BLE001
                lines.append(f"  test spreadsheet: FAILED ({type(exc).__name__}: {exc})")
        try:
            self.artifacts_dir.mkdir(parents=True, exist_ok=True)
            probe = self.artifacts_dir / ".write-check"
            probe.write_bytes(b"")
            probe.unlink()
            lines.append(f"  folder: {self.artifacts_dir} is writable")
        except OSError as exc:
            lines.append(f"  folder: {self.artifacts_dir} is NOT writable ({exc})")
        return lines


def _summary(table: Table) -> str:
    """"12 rows, 3 columns, sheet 'Q1 sales'"."""
    rows = f"{len(table.rows):,} row{'s' if len(table.rows) != 1 else ''}"
    columns = f"{len(table.columns)} column{'s' if len(table.columns) != 1 else ''}"
    sheet = f", sheet '{table.sheet}'" if table.sheet != DEFAULT_SHEET else ""
    return f"{rows}, {columns}{sheet}"


def markdown_link(path: Path | str, label: str = DOWNLOAD_LABEL) -> str:
    """The Markdown line the web chat shows as a download: [Download Spreadsheet](/artifacts/<file name>)."""
    return f"[{label}]({web_url(path)})"


def _shown(text: str, limit: int = 120) -> str:
    """`text` cut short and safe for a console that cannot print Thai."""
    text = text if len(text) <= limit else text[: limit - 1] + "…"
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return text.encode(encoding, errors="backslashreplace").decode(encoding)


def build() -> AnalyticsAgent:
    return AnalyticsAgent()


if __name__ == "__main__":
    agent = build()
    arguments = sys.argv[1:]
    if not arguments or arguments == ["--check"]:
        print("\n".join(agent.check()))
        raise SystemExit(0 if agent.available else 1)
    result = agent.build_spreadsheet(Path(arguments[0]).read_text(encoding="utf-8"))
    print(_shown(result, 2000))
    raise SystemExit(0 if result.startswith("SUCCESS") else 1)
