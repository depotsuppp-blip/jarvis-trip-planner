"""
A tiny MCP server for testing Jarvis's MCP client (mcp_client.py).

It speaks the stdio transport - one JSON-RPC message per line on stdin and
stdout - using nothing outside the standard library, and offers two
read-only tools:

    get_server_time   the current time, optionally at a UTC offset
    read_mock_file    the contents of one of a few built-in text files

Jarvis starts it for you when mcp_servers.json lists it (see
mcp_servers.example.json). To poke at it by hand instead:

    python mock_mcp_server.py

then paste these lines, one at a time, and read what comes back:

    {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"hand","version":"0"}}}
    {"jsonrpc":"2.0","method":"notifications/initialized"}
    {"jsonrpc":"2.0","id":2,"method":"tools/list"}
    {"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"get_server_time","arguments":{"utc_offset":"+07:00"}}}

Logging goes to stderr; stdout carries protocol messages and nothing else.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "mock-mcp-server", "version": "1.0.0"}

# Fake files. Nothing here touches the disk, so the "read a file" tool cannot
# be turned into a way to read the real one.
MOCK_FILES = {
    "notes.txt": "Remember: the Jarvis demo is on Friday.\nBring the portable speaker.\n",
    "todo.md": "# To do\n- [x] wire up the MCP client\n- [ ] try a real MCP server\n- [ ] write the docs\n",
    "config.json": json.dumps({"service": "mock", "debug": False, "retries": 3}, indent=2),
}

TOOLS = [
    {
        "name": "get_server_time",
        "title": "Server time",
        "description": (
            "Returns the current date and time on the tool server. Optionally "
            "give a UTC offset such as '+07:00' to see the time somewhere else."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "utc_offset": {
                    "type": "string",
                    "description": "Offset from UTC like '+07:00' or '-05:30'. Omit for UTC.",
                }
            },
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "read_mock_file",
        "title": "Read a mock file",
        "description": (
            "Reads one of the mock server's built-in text files by name. "
            "Available: " + ", ".join(MOCK_FILES) + "."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "File name, e.g. 'notes.txt'."}},
            "required": ["name"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
]


def log(message: str) -> None:
    print(f"[mock-mcp] {message}", file=sys.stderr, flush=True)


def text_result(text: str, *, is_error: bool = False) -> dict:
    result: dict = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


def get_server_time(arguments: dict) -> dict:
    offset = arguments.get("utc_offset")
    zone = timezone.utc
    if offset not in (None, ""):
        match = re.fullmatch(r"([+-])(\d{1,2}):?(\d{2})?", str(offset).strip())
        if not match:
            return text_result(f"'{offset}' is not a UTC offset. Use a form like '+07:00'.", is_error=True)
        hours, minutes = int(match.group(2)), int(match.group(3) or 0)
        if hours > 14 or minutes > 59:
            return text_result(f"'{offset}' is outside the range of real UTC offsets.", is_error=True)
        sign = -1 if match.group(1) == "-" else 1
        zone = timezone(sign * timedelta(hours=hours, minutes=minutes))
    now = datetime.now(zone)
    return text_result(now.strftime("%A %d %B %Y, %H:%M:%S ") + f"(UTC{now.strftime('%z')[:3]}:{now.strftime('%z')[3:]})")


def read_mock_file(arguments: dict) -> dict:
    name = str(arguments.get("name", "")).strip()
    if name in MOCK_FILES:
        return text_result(MOCK_FILES[name])
    # A tool failure, not a protocol error: the model gets to read it and retry.
    return text_result(f"There is no mock file called '{name}'. Available: {', '.join(MOCK_FILES)}.", is_error=True)


HANDLERS = {"get_server_time": get_server_time, "read_mock_file": read_mock_file}


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
        log(f"tools/call {name} {json.dumps(arguments, ensure_ascii=False)}")
        respond(request_id, result=handler(arguments))
    else:
        respond(request_id, error={"code": -32601, "message": f"Method not found: {method}"})


def main() -> None:
    # Windows would otherwise write the console's code page, and Thai or any
    # other non-ASCII text in a reply would arrive as mojibake.
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
