"""
A web search MCP server for Jarvis (see mcp_client.py), backed by DuckDuckGo.

It speaks the stdio transport - one JSON-RPC message per line on stdin and
stdout - and offers one read-only tool:

    web_search   searches the web and returns each result's title, URL and a
                 short snippet. 'query' is required; 'max_results' (1-10,
                 default 5), 'region' ("wt-wt" = anywhere, "th-th", "us-en", ...)
                 and 'time_range' ("day", "week", "month", "year") are optional.

Jarvis starts it by itself - it is one of the servers that ship with it, so
mcp_servers.json does not need to mention it (add `"web_search": {"disabled":
true}` there to turn it off). To poke at it by hand instead:

    python mcp_websearch.py

then paste these lines, one at a time, and read what comes back:

    {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"hand","version":"0"}}}
    {"jsonrpc":"2.0","method":"notifications/initialized"}
    {"jsonrpc":"2.0","id":2,"method":"tools/list"}
    {"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"web_search","arguments":{"query":"python 3.13 release date","max_results":3}}}

THE LIBRARY. The search itself is the `ddgs` package, which is `duckduckgo-search`
under its current name: the author renamed it, the old name was frozen at 8.1.1
and now prints a rename warning and returns no results. This server imports
`ddgs` and falls back to `duckduckgo_search` only for a machine that still has
that one installed. Neither is needed to START the server: without one, the tool
answers that the package is missing (pip install ddgs) instead of crashing.

WHAT THE MODEL GETS. Snippets are short by nature: they are a pointer to a page,
not the page. Results are cut to a fixed size so a long one cannot swamp the
conversation, and mcp_client.py labels everything this returns as external data
(a web page can contain text that tries to give the assistant orders).

WHEN IT FINDS NOTHING OR FAILS. Every such answer tells the model in so many words
not to search again and what to tell the person - "Do not retry the search tool".
An answer that said "try again" was taken literally: the model searched again and
again until the brain's step limit ended the turn with "I went back and forth on
that too many times". The cases:

    no results      a normal tool result ("returned no results ... do not retry"),
                    given only after the server itself has tried once more (ddgs
                    is empty now and then for a query that has results, _search).
                    ddgs raises instead of returning an empty list, and the error
                    it raises is whichever engine failed last - with the region
                    "wt-wt" its Wikipedia lookup always fails (wt.wikipedia.org
                    does not exist) - so "found nothing" and "nothing answered"
                    look alike there; both are reported as no results for this
                    query, and the same words are not searched again for
                    EMPTY_REPEAT_SECONDS.
    rate limited,   isError, "the search is currently unavailable". The search then
    timed out,      stays off for a cooldown (COOLDOWN_SECONDS, longer after a rate
    no connection   limit): a retry within it is answered at once, without asking
                    DuckDuckGo - more requests during a rate limit only make it
                    last longer. The first search that works ends it.
    not installed   isError, with the pip command.

None of them crash the server. Logging goes to stderr; stdout carries protocol
messages and nothing else. Lines that say ERROR or WARNING - every failure and
every empty search - are also printed on the Jarvis console by mcp_client.py
(it keeps the rest of a server's stderr to itself).
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
import warnings
from typing import Any, Callable, Optional

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "jarvis-websearch", "version": "1.1.0"}

DEFAULT_RESULTS = 5
MAX_RESULTS = 10
MAX_QUERY_CHARS = 400
MAX_TITLE_CHARS = 140
MAX_SNIPPET_CHARS = 300
MAX_URL_CHARS = 300
# One reply must fit what mcp_client.py keeps of a tool result (its MAX_RESULT_CHARS, 8000).
MAX_REPLY_CHARS = 7000
# DuckDuckGo's own per-request limit, and the wall-clock limit for the whole call (the
# client gives a tool call 30 seconds). A search takes two to five seconds.
REQUEST_TIMEOUT = 10
DEADLINE_SECONDS = 22.0
DEFAULT_REGION = "wt-wt"
# After a failure the search stays off this long (see WHEN IT FINDS NOTHING above).
COOLDOWN_SECONDS = 45.0
RATELIMIT_COOLDOWN_SECONDS = 120.0
# The same words that just found nothing are not searched again within this window.
EMPTY_REPEAT_SECONDS = 300.0
MAX_REMEMBERED_EMPTY = 100
# An empty answer is tried once more (see _search) only if this much of the deadline is left.
RETRY_MIN_SECONDS = 8.0

# Live run 2026-10-26: told only "do not retry", the model still searched again with
# other words, so the sentence says that too.
DO_NOT_RETRY = "Do not retry the search tool for this question, not even with different words."
# Why a search failed, as the model is told it (and the person, in its words).
_FAILURE_REASONS = {
    "ratelimit": "DuckDuckGo is refusing searches for now (rate limit)",
    "timeout": "the search took too long to answer",
    "unavailable": "no connection, or the search service did not answer",
    "missing": "web search is not installed on this machine (pip install ddgs)",
}

_TIME_RANGES = {"day": "d", "week": "w", "month": "m", "year": "y"}
_REGION = re.compile(r"^[a-z]{2}-[a-z]{2}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

TOOLS = [
    {
        "name": "web_search",
        "title": "Web search",
        "description": (
            "Searches the web (DuckDuckGo) and returns each result's title, URL and a short "
            "snippet. Use it for current events, recent facts, prices, documentation, or "
            "anything you are not sure of or that may have changed since your training. "
            "Snippets are only a pointer to a page: answer from them when they settle the "
            "question, say so when they do not, and give the URL when the person may want to "
            "read more. Search with a short, specific query rather than a whole sentence."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for."},
                "max_results": {
                    "type": "integer",
                    "description": f"How many results, 1 to {MAX_RESULTS}. Default {DEFAULT_RESULTS}.",
                },
                "region": {
                    "type": "string",
                    "description": "Where to favour results from, like 'th-th' or 'us-en'. Default: anywhere.",
                },
                "time_range": {
                    "type": "string",
                    "enum": list(_TIME_RANGES),
                    "description": "Only results from the last day, week, month or year. Omit for any time.",
                },
            },
            "required": ["query"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True},
    }
]


def log(message: str) -> None:
    print(f"[mcp-websearch] {message}", file=sys.stderr, flush=True)


def log_problem(level: str, message: str) -> None:
    """An ERROR or WARNING line: mcp_client.py shows these on the Jarvis console."""
    log(f"{level}: {message}")


def text_result(text: str, *, is_error: bool = False) -> dict:
    result: dict = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


class SearchError(Exception):
    """
    A search that produced no answer. `kind` decides what the model is told: "empty"
    (nothing found), "nothing" (no engine gave a result and one failed - see the module
    docstring), "ratelimit", "timeout", "unavailable" or "missing". `detail` is the
    underlying error, for the log only.
    """

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(detail or kind)
        self.kind = kind
        self.detail = detail


def _classify(exc: BaseException) -> SearchError:
    """What a library or network exception means for the person asking."""
    if isinstance(exc, SearchError):
        return exc
    name = type(exc).__name__.lower()
    text = str(exc)
    lowered = text.lower()
    detail = f"{type(exc).__name__}: {text}"[:500]
    if "ratelimit" in name or "ratelimit" in lowered or "rate limit" in lowered:
        return SearchError("ratelimit", detail)
    if "timeout" in name or "timed out" in lowered:
        return SearchError("timeout", detail)
    if "no results found" in lowered:
        return SearchError("empty", detail)
    # ddgs's own exception, raised when no engine returned anything and at least one
    # failed: it carries the last engine's error, which says nothing about the others.
    if name == "ddgsexception":
        return SearchError("nothing", detail)
    return SearchError("unavailable", detail)


# =====================================================================
# The search
# =====================================================================


def _load_ddgs() -> Any:
    """The DDGS class from `ddgs`, or from the frozen `duckduckgo_search` as a fallback."""
    try:
        from ddgs import DDGS  # type: ignore[import-not-found]

        return DDGS
    except ImportError:
        pass
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # it warns that it was renamed to ddgs
            from duckduckgo_search import DDGS  # type: ignore[import-not-found]

        return DDGS
    except ImportError:
        raise SearchError("missing", "neither ddgs nor duckduckgo_search is installed") from None


def _fetch(query: str, count: int, region: str, timelimit: Optional[str]) -> list[dict[str, Any]]:
    DDGS = _load_ddgs()
    try:
        client = DDGS(timeout=REQUEST_TIMEOUT)
        return list(client.text(query, region=region, safesearch="moderate", timelimit=timelimit, max_results=count))
    except Exception as exc:  # noqa: BLE001 - the library raises its own and the network's errors
        raise _classify(exc) from None


def _search_with_deadline(
    query: str,
    count: int,
    region: str,
    timelimit: Optional[str],
    fetch: Callable[[str, int, str, Optional[str]], list[dict[str, Any]]],
    seconds: Optional[float] = None,
) -> list[dict[str, Any]]:
    """Runs `fetch` on its own thread so a hung connection cannot hold the server past the deadline."""
    seconds = DEADLINE_SECONDS if seconds is None else seconds
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["results"] = fetch(query, count, region, timelimit)
        except BaseException as exc:  # noqa: BLE001 - handed to the caller below
            box["error"] = exc

    worker = threading.Thread(target=run, name="websearch", daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        raise SearchError("timeout", f"no answer within {seconds:.0f} seconds")
    if "error" in box:
        raise _classify(box["error"])
    return box["results"]


def _search(
    query: str,
    count: int,
    region: str,
    timelimit: Optional[str],
    fetch: Callable[[str, int, str, Optional[str]], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """
    One search as the model sees it: up to two tries, within one DEADLINE_SECONDS.

    ddgs comes back empty now and then for a query that has plenty of results - it asks
    a shuffled set of engines, and the ones it reached returned nothing (measured
    2026-10-26: "latest news thailand" empty in about one run in three, whatever the
    region). So an empty answer is tried once more here, where it costs a few seconds,
    instead of being handed to the model, which would either say there is nothing or
    search again itself. A failure (rate limit, timeout, no connection) is not retried.
    """
    started = time.perf_counter()
    for attempt in (1, 2):
        remaining = DEADLINE_SECONDS - (time.perf_counter() - started)
        try:
            results = _search_with_deadline(query, count, region, timelimit, fetch, remaining)
        except SearchError as exc:
            if exc.kind not in ("empty", "nothing"):
                raise
            results, why = [], exc.detail or exc.kind
        else:
            why = f"{len(results)} result(s), none with a usable link"
        if _entries(results):
            return results
        left = DEADLINE_SECONDS - (time.perf_counter() - started)
        if attempt == 2 or left < RETRY_MIN_SECONDS:
            raise SearchError("nothing" if results == [] else "empty", why)
        log(f"nothing on the first try ({why[:160]}); trying once more")
    raise SearchError("empty")  # not reached


# =====================================================================
# Not asking again: the cooldown after a failure, and searches that found nothing
# =====================================================================

# Only the main thread (the stdio loop) reads and writes these.
_cooldown: dict[str, Any] = {}  # {"until": monotonic time, "kind": ..., "at": monotonic time}
_recent_empty: dict[tuple, float] = {}


def _cooling_down(now: float) -> Optional[dict[str, Any]]:
    return _cooldown if _cooldown and now < _cooldown["until"] else None


def _start_cooldown(kind: str, now: float) -> None:
    seconds = RATELIMIT_COOLDOWN_SECONDS if kind == "ratelimit" else COOLDOWN_SECONDS
    _cooldown.update({"until": now + seconds, "kind": kind, "at": now})


def _remember_empty(key: tuple, now: float) -> None:
    for old in [k for k, at in _recent_empty.items() if now - at >= EMPTY_REPEAT_SECONDS]:
        del _recent_empty[old]
    while len(_recent_empty) >= MAX_REMEMBERED_EMPTY:
        del _recent_empty[next(iter(_recent_empty))]
    _recent_empty[key] = now


def _no_results(query: str, why: str = "") -> dict:
    """Nothing found: a plain result, so the model reports it rather than treating it as a fault to work round."""
    return text_result(
        f"Web search returned no results for \"{_clean(query, 80)}\"{why}. {DO_NOT_RETRY} "
        "Tell the user the search found nothing for it; if you can answer from what you "
        "already know, do, and say that it is not from a live search."
    )


def _unavailable(kind: str, *, earlier: Optional[float] = None) -> dict:
    reason = _FAILURE_REASONS.get(kind, _FAILURE_REASONS["unavailable"])
    later = " and to try again in a few minutes" if kind == "ratelimit" else ""
    if earlier is None:
        opening = f"Web search failed: {reason}."
    else:
        opening = f"Web search is unavailable right now: it failed {earlier:.0f} seconds ago ({reason})."
    return text_result(
        f"{opening} {DO_NOT_RETRY} Tell the user the search is currently unavailable{later}.",
        is_error=True,
    )


def _clean(value: Any, limit: int) -> str:
    text = _CONTROL.sub(" ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _entries(results: list[dict[str, Any]]) -> list[str]:
    """One numbered entry per result. Results without a usable http(s) link are dropped."""
    lines: list[str] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        url = _clean(item.get("href") or item.get("url"), MAX_URL_CHARS)
        if not re.match(r"^https?://", url, re.IGNORECASE):
            continue
        title = _clean(item.get("title"), MAX_TITLE_CHARS) or url
        snippet = _clean(item.get("body") or item.get("snippet"), MAX_SNIPPET_CHARS)
        lines.append(f"{len(lines) + 1}. {title}\n   {url}" + (f"\n   {snippet}" if snippet else ""))
    return lines


def format_results(query: str, results: list[dict[str, Any]]) -> str:
    """The numbered list the model reads (web_search answers an empty one with _no_results instead)."""
    lines = _entries(results)
    if not lines:
        return f"No results for \"{_clean(query, 80)}\"."
    text = f"Web search results for \"{_clean(query, 80)}\" ({len(lines)}):\n\n" + "\n\n".join(lines)
    return text if len(text) <= MAX_REPLY_CHARS else text[: MAX_REPLY_CHARS - 1].rstrip() + "…"


def web_search(arguments: dict, fetch: Callable[..., list[dict[str, Any]]] = _fetch) -> dict:
    query = _clean(arguments.get("query"), MAX_QUERY_CHARS * 2)
    if not query:
        return text_result("Give a 'query' to search for.", is_error=True)
    if len(query) > MAX_QUERY_CHARS:
        return text_result(f"The query is too long ({len(query)} characters, at most {MAX_QUERY_CHARS}). Shorten it.", is_error=True)

    count = arguments.get("max_results", DEFAULT_RESULTS)
    try:
        count = DEFAULT_RESULTS if isinstance(count, bool) else int(count)
    except (TypeError, ValueError, OverflowError):  # a string, null, NaN, infinity
        count = DEFAULT_RESULTS
    count = max(1, min(MAX_RESULTS, count))

    region = str(arguments.get("region") or DEFAULT_REGION).strip().lower()
    if region != DEFAULT_REGION and not _REGION.match(region):
        return text_result(f"'{region}' is not a region. Use a form like 'th-th' or 'us-en', or leave it out.", is_error=True)

    time_range = arguments.get("time_range")
    timelimit = None
    if time_range not in (None, ""):
        timelimit = _TIME_RANGES.get(str(time_range).strip().lower())
        if timelimit is None:
            return text_result(f"'{time_range}' is not a time range. Use day, week, month or year.", is_error=True)

    shown = json.dumps(query, ensure_ascii=False)
    now = time.monotonic()

    # A retry while the search is off, or the same words that just found nothing, are
    # answered from memory: no request goes out (see WHEN IT FINDS NOTHING at the top).
    blocked = _cooling_down(now)
    if blocked:
        log_problem("WARNING", f"search {shown} not sent: still cooling down after a {blocked['kind']} failure")
        return _unavailable(blocked["kind"], earlier=now - blocked["at"])
    key = (query.casefold(), region, timelimit)
    searched = _recent_empty.get(key)
    if searched is not None and now - searched < EMPTY_REPEAT_SECONDS:
        log_problem("WARNING", f"search {shown} not sent: it found nothing {now - searched:.0f}s ago")
        return _no_results(query, f" (searched {now - searched:.0f} seconds ago)")

    log(f"search {shown} (n={count}, region={region}, range={time_range or 'any'})")
    try:
        results = _search(query, count, region, timelimit, fetch)
    except SearchError as exc:
        return _failed(exc, query, shown, key)
    except Exception as exc:  # noqa: BLE001 - a tool answers; it does not raise
        return _failed(SearchError("unavailable", f"{type(exc).__name__}: {exc}"[:500]), query, shown, key)

    _cooldown.clear()  # it works again
    return text_result(format_results(query, results))


def _failed(exc: SearchError, query: str, shown: str, key: tuple) -> dict:
    """Logs a search that gave no answer and words it for the model (see the top of the file)."""
    now = time.monotonic()
    if exc.kind in ("empty", "nothing"):
        log_problem("WARNING", f"no results for {shown} ({exc.detail or exc.kind})")
        _remember_empty(key, now)
        return _no_results(query)
    log_problem("ERROR", f"search {shown} failed ({exc.kind}): {exc.detail or exc.kind}")
    if exc.kind != "missing":  # a missing package does not come back by waiting
        _start_cooldown(exc.kind, now)
    return _unavailable(exc.kind)


HANDLERS = {"web_search": web_search}


# =====================================================================
# The stdio JSON-RPC loop (same shape as mock_mcp_server.py)
# =====================================================================


def respond(request_id, *, result=None, error=None) -> None:
    body = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        body["error"] = error
    else:
        body["result"] = result
    sys.stdout.write(json.dumps(body, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def handle(message: dict) -> None:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") if isinstance(message.get("params"), dict) else {}

    if request_id is None:  # a notification: nothing to answer
        if method not in ("notifications/initialized", "notifications/cancelled"):
            log(f"ignored notification {method}")
        return

    if method == "initialize":
        respond(
            request_id,
            result={
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            },
        )
    elif method == "ping":
        respond(request_id, result={})
    elif method == "tools/list":
        respond(request_id, result={"tools": TOOLS})
    elif method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        handler = HANDLERS.get(name)
        if handler is None:
            respond(request_id, error={"code": -32602, "message": f"Unknown tool: {name}"})
            return
        try:
            respond(request_id, result=handler(arguments))
        except Exception as exc:  # noqa: BLE001 - a bug in a tool must not take the server down
            log_problem("ERROR", f"{name} crashed: {type(exc).__name__}: {exc}")
            respond(
                request_id,
                result=text_result(
                    f"The search tool hit an internal error. {DO_NOT_RETRY} "
                    "Tell the user the search is currently unavailable.",
                    is_error=True,
                ),
            )
    else:
        respond(request_id, error={"code": -32601, "message": f"Method not found: {method}"})


def main() -> None:
    # Windows would otherwise write the console's code page, and Thai or any
    # other non-ASCII text in a result would arrive as mojibake.
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    log("ready (stdio)")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            respond(None, error={"code": -32700, "message": "Parse error"})
            continue
        if isinstance(message, dict):
            handle(message)
        else:
            respond(None, error={"code": -32600, "message": "Invalid Request"})
    log("stdin closed, exiting")


if __name__ == "__main__":
    main()
