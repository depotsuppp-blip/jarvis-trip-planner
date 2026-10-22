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

Failures (no network, DuckDuckGo refusing a burst of requests, a timeout) come
back as a tool result with isError set, so the model can say so; they never
crash the server. Logging goes to stderr; stdout carries protocol messages and
nothing else.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import warnings
from typing import Any, Callable, Optional

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "jarvis-websearch", "version": "1.0.0"}

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


def text_result(text: str, *, is_error: bool = False) -> dict:
    result: dict = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


class SearchError(Exception):
    """A failure the model should hear about, worded for it."""


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
        raise SearchError(
            "Web search is not installed on this machine. Install it with: pip install ddgs"
        ) from None


def _fetch(query: str, count: int, region: str, timelimit: Optional[str]) -> list[dict[str, Any]]:
    DDGS = _load_ddgs()
    try:
        client = DDGS(timeout=REQUEST_TIMEOUT)
        return list(client.text(query, region=region, safesearch="moderate", timelimit=timelimit, max_results=count))
    except SearchError:
        raise
    except Exception as exc:  # noqa: BLE001 - the library raises its own and the network's errors
        name = type(exc).__name__
        if "ratelimit" in name.lower() or "rate limit" in str(exc).lower():
            raise SearchError("DuckDuckGo is limiting searches right now. Try again in a minute.") from None
        if "timeout" in name.lower() or "timed out" in str(exc).lower():
            raise SearchError("The search timed out. Try again.") from None
        log(f"search failed: {name}: {exc}")
        raise SearchError("The search failed (no connection, or DuckDuckGo did not answer). Try again.") from None


def _search_with_deadline(
    query: str,
    count: int,
    region: str,
    timelimit: Optional[str],
    fetch: Callable[[str, int, str, Optional[str]], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Runs `fetch` on its own thread so a hung connection cannot hold the server past the deadline."""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["results"] = fetch(query, count, region, timelimit)
        except BaseException as exc:  # noqa: BLE001 - handed to the caller below
            box["error"] = exc

    worker = threading.Thread(target=run, name="websearch", daemon=True)
    worker.start()
    worker.join(DEADLINE_SECONDS)
    if worker.is_alive():
        raise SearchError("The search timed out. Try again.")
    if "error" in box:
        raise box["error"]
    return box["results"]


def _clean(value: Any, limit: int) -> str:
    text = _CONTROL.sub(" ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def format_results(query: str, results: list[dict[str, Any]]) -> str:
    """The numbered list the model reads. Results without a usable http(s) link are dropped."""
    lines: list[str] = []
    kept = 0
    for item in results:
        if not isinstance(item, dict):
            continue
        url = _clean(item.get("href") or item.get("url"), MAX_URL_CHARS)
        if not re.match(r"^https?://", url, re.IGNORECASE):
            continue
        kept += 1
        title = _clean(item.get("title"), MAX_TITLE_CHARS) or url
        snippet = _clean(item.get("body") or item.get("snippet"), MAX_SNIPPET_CHARS)
        lines.append(f"{kept}. {title}\n   {url}" + (f"\n   {snippet}" if snippet else ""))

    if not kept:
        return f"No results for \"{_clean(query, 80)}\"."
    text = f"Web search results for \"{_clean(query, 80)}\" ({kept}):\n\n" + "\n\n".join(lines)
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

    log(f"search {json.dumps(query, ensure_ascii=False)} (n={count}, region={region}, range={time_range or 'any'})")
    try:
        results = _search_with_deadline(query, count, region, timelimit, fetch)
    except SearchError as exc:
        return text_result(str(exc), is_error=True)
    except Exception as exc:  # noqa: BLE001 - a tool answers; it does not raise
        log(f"search crashed: {type(exc).__name__}: {exc}")
        return text_result("The search failed unexpectedly. Try again.", is_error=True)
    return text_result(format_results(query, results))


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
            log(f"{name} crashed: {type(exc).__name__}: {exc}")
            respond(request_id, result=text_result("The search tool hit an internal error.", is_error=True))
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
