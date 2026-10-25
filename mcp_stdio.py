"""
The stdio side of the MCP servers that ship with Jarvis: mcp_fetch.py,
mcp_memory.py and mcp_data.py import this instead of each carrying a copy of
the JSON-RPC loop. (mcp_filesystem.py and mcp_websearch.py predate it and keep
their own.)

A server built on it is a list of tool definitions plus one function per tool.
The function takes the call's arguments and returns the text the model reads;
raising ToolError turns its message into a tool result with isError set, so
the model can read why and try again. Anything else a tool raises is logged
and answered with a generic error - a bug in one tool never stops the server.

Protocol: one JSON-RPC 2.0 message per line on stdin and stdout (the MCP stdio
framing). stdout carries protocol messages and nothing else; logging goes to
stderr, which mcp_client.py drains.

CALLER SCOPE
------------
The memory and data servers keep one store per person. They cannot tell who
is asking - the model writes the arguments, and the model must not be trusted
to name the person - so mcp_client.py adds the caller to every call it
forwards to them, as a hidden "_scope" argument the model never sees (it is
not in the tool's schema, and a value the model sends is overwritten):

    "owner"         the voice assistant, or the owner signed in on the web
    "user:<id>"     another person signed in to the web app

scope_of() reads it. A call without one - the server run by hand, or by
another MCP client - is the owner's.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Callable

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

OWNER_SCOPE = "owner"
SCOPE_ARG = "_scope"
_SCOPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,159}$")


class ToolError(Exception):
    """A failure the model should hear about, worded for it."""


Handler = Callable[[dict], str]


def text_result(text: str, *, is_error: bool = False) -> dict:
    result: dict = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


def scope_of(arguments: dict) -> str:
    """The caller's scope (see the module docstring), removed from `arguments`."""
    raw = arguments.pop(SCOPE_ARG, None)
    if raw is None:
        return OWNER_SCOPE
    if not isinstance(raw, str) or not _SCOPE.match(raw):
        raise ToolError("This call came without a valid caller, so nothing was read or saved.")
    return raw


def data_dir() -> str:
    """
    Where the servers keep their files: JARVIS_DATA_DIR, or .data next to this
    file - the folder llm_budget.py already uses, which is gitignored and is a
    named volume in docker-compose.yml, so it survives a rebuild.
    """
    configured = os.environ.get("JARVIS_DATA_DIR", "").strip()
    path = configured or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".data")
    os.makedirs(path, exist_ok=True)
    return path


def env_on(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


class Server:
    def __init__(
        self,
        *,
        name: str,
        version: str,
        instructions: str,
        tools: list[dict],
        handlers: dict[str, Handler],
        log_prefix: str,
    ) -> None:
        missing = {t["name"] for t in tools} ^ set(handlers)
        if missing:
            raise ValueError(f"tools and handlers do not match: {sorted(missing)}")
        self.info = {"name": name, "version": version}
        self.instructions = instructions
        self.tools = tools
        self.handlers = handlers
        self.prefix = log_prefix

    def log(self, message: str) -> None:
        print(f"[{self.prefix}] {message}", file=sys.stderr, flush=True)

    def call(self, name: str, arguments: dict) -> dict:
        """One tools/call -> its result. Never raises."""
        try:
            return text_result(self.handlers[name](arguments))
        except ToolError as exc:
            return text_result(str(exc), is_error=True)
        except Exception as exc:  # noqa: BLE001 - a bug in a tool must not take the server down
            self.log(f"{name} crashed: {type(exc).__name__}: {exc}")
            return text_result(f"The {name} tool hit an internal error.", is_error=True)

    # -- JSON-RPC ------------------------------------------------------

    @staticmethod
    def respond(request_id: Any, *, result: Any = None, error: Any = None) -> None:
        body: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
        if error is not None:
            body["error"] = error
        else:
            body["result"] = result
        sys.stdout.write(json.dumps(body, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()

    def handle(self, message: dict) -> None:
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}

        if not isinstance(method, str):
            if request_id is not None:  # a stray response or junk - only requests get an answer
                self.respond(request_id, error={"code": -32600, "message": "Invalid Request"})
            return
        if request_id is None:  # a notification: nothing to answer
            if method not in ("notifications/initialized", "notifications/cancelled"):
                self.log(f"ignored notification {method}")
            return

        if method == "initialize":
            asked = params.get("protocolVersion")
            self.respond(
                request_id,
                result={
                    "protocolVersion": asked if asked in SUPPORTED_VERSIONS else PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": self.info,
                    "instructions": self.instructions,
                },
            )
        elif method == "ping":
            self.respond(request_id, result={})
        elif method == "tools/list":
            self.respond(request_id, result={"tools": self.tools})
        elif method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str) or name not in self.handlers:
                self.respond(request_id, error={"code": -32602, "message": f"Unknown tool: {name}"})
                return
            arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            shown = {k: v for k, v in arguments.items() if k != SCOPE_ARG}
            self.log(f"tools/call {name} {json.dumps(shown, ensure_ascii=False)[:200]}")
            self.respond(request_id, result=self.call(name, arguments))
        else:
            self.respond(request_id, error={"code": -32601, "message": f"Method not found: {method}"})

    def serve(self) -> None:
        # Windows would otherwise use the console's code page, and Thai text in
        # a request or a result would arrive as mojibake.
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
        self.log("ready (stdio)")
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                self.respond(None, error={"code": -32700, "message": "Parse error"})
                continue
            if not isinstance(message, dict):
                self.respond(None, error={"code": -32600, "message": "Invalid Request"})
                continue
            try:
                self.handle(message)
            except Exception as exc:  # noqa: BLE001
                self.log(f"unexpected {type(exc).__name__}: {exc}")
                if message.get("id") is not None:
                    self.respond(message["id"], error={"code": -32603, "message": "Internal error"})
        self.log("stdin closed, exiting")
