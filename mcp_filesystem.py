"""
A read-only file system MCP server for Jarvis (see mcp_client.py).

It speaks the stdio transport - one JSON-RPC message per line on stdin and
stdout - using nothing outside the standard library, and offers two tools:

    read_file        the text of one file
    list_directory   the files and folders inside one directory

Everything is confined to the directory the server is started in (the project
root, since mcp_client.py starts it there) and the folders beneath it:

    * paths must be relative - an absolute path, a drive letter, a UNC share
      or a '..' that climbs out is refused;
    * the path is resolved first (symlinks and Windows junctions included) and
      the real location must still be inside the root, so a link that points
      outside is refused too;
    * secrets that live in a project folder are refused even though they are
      inside it: .env files, private keys, the local MCP server list and the
      .git folder. They also do not show up in directory listings.

Failures (missing file, no permission, not text, ...) come back as a tool
result with isError set, so the model can read the reason and try again; they
never crash the server and never show the real location of the root.

Jarvis starts it for you when mcp_servers.json lists it (see
mcp_servers.example.json). To poke at it by hand instead:

    python mcp_filesystem.py

then paste these lines, one at a time, and read what comes back:

    {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"hand","version":"0"}}}
    {"jsonrpc":"2.0","method":"notifications/initialized"}
    {"jsonrpc":"2.0","id":2,"method":"tools/list"}
    {"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"list_directory","arguments":{"path":"."}}}
    {"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"read_file","arguments":{"path":"PRODUCT.md"}}}

Logging goes to stderr; stdout carries protocol messages and nothing else.
"""

from __future__ import annotations

import fnmatch
import json
import os
import sys

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "jarvis-filesystem", "version": "1.0.0"}

MAX_READ_BYTES = 1_000_000   # a bigger file is refused rather than loaded
MAX_REPLY_CHARS = 60_000     # the text of a file is cut here (the client cuts again, shorter)
MAX_LIST_ENTRIES = 500       # a folder with more entries is listed up to this
MAX_PATH_CHARS = 1024

# Matched against one path component, case-insensitively. The *.example style
# templates are meant to be read, so they are let through below.
_SECRET_NAMES = (
    ".env", ".env.*", "*.pem", "*.key", "*.pfx", "*.p12", "*.ppk",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
    "mcp_servers.json", ".netrc", ".npmrc", ".pypirc",
)
_TEMPLATE_SUFFIXES = (".example", ".sample", ".template")
_SECRET_DIRS = (".git",)


def log(message: str) -> None:
    print(f"[mcp-fs] {message}", file=sys.stderr, flush=True)


class FsError(Exception):
    """A problem the model should hear about, worded without the real root."""


# =====================================================================
# Confinement
# =====================================================================

ROOT = os.path.realpath(os.getcwd())


def _norm(path: str) -> str:
    return os.path.normcase(path)


def _is_inside_root(real: str) -> bool:
    try:
        return os.path.commonpath([_norm(real), _norm(ROOT)]) == _norm(ROOT)
    except ValueError:  # another drive
        return False


def _is_secret_name(name: str, *, is_dir: bool) -> bool:
    lowered = name.lower()
    if is_dir:
        return lowered in _SECRET_DIRS
    if lowered.endswith(_TEMPLATE_SUFFIXES):
        return False
    return any(fnmatch.fnmatchcase(lowered, pattern) for pattern in _SECRET_NAMES)


def _is_secret_path(real: str, *, is_dir: bool) -> bool:
    """True if the resolved path is, or sits inside, something kept private."""
    relative = os.path.relpath(real, ROOT)
    if relative == ".":
        return False
    parts = relative.replace("\\", "/").split("/")
    if any(_is_secret_name(p, is_dir=True) for p in parts[:-1]):
        return True
    return _is_secret_name(parts[-1], is_dir=is_dir)


def resolve(raw: object, *, default: str | None = None) -> tuple[str, str]:
    """
    A path from the model -> (real path inside the root, tidy relative label).
    Raises FsError for anything that is not a plain relative path under the root.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if default is None:
            raise FsError("'path' is required: give a path relative to the project root.")
        raw = default
    if not isinstance(raw, str):
        raise FsError("'path' must be a string.")
    text = raw.strip()
    if len(text) > MAX_PATH_CHARS:
        raise FsError("That path is too long.")
    if "\x00" in text:
        raise FsError("That path contains an invalid character.")
    # ':' covers drive letters (C:\x, C:x) and NTFS alternate streams (file:stream).
    if ":" in text or text.startswith(("/", "\\")) or os.path.isabs(text):
        raise FsError("Only paths relative to the project root are allowed (no absolute paths or drive letters).")

    candidate = os.path.join(ROOT, text)
    real = os.path.realpath(candidate)
    if not _is_inside_root(real):
        raise FsError("That path is outside the project root, which is not allowed.")
    label = os.path.relpath(real, ROOT).replace("\\", "/")
    return real, ("." if label == "." else label)


# =====================================================================
# Tools
# =====================================================================


def _human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _os_error_message(exc: OSError, label: str) -> str:
    if isinstance(exc, FileNotFoundError):
        return f"'{label}' does not exist."
    if isinstance(exc, PermissionError):
        return f"Permission denied reading '{label}'."
    if isinstance(exc, NotADirectoryError):
        return f"'{label}' is not a directory."
    if isinstance(exc, IsADirectoryError):
        return f"'{label}' is a directory, not a file."
    return f"Could not read '{label}' ({type(exc).__name__})."


def read_file(arguments: dict) -> str:
    real, label = resolve(arguments.get("path"))
    # Checked before existence, so a secret gets the same answer whether or not it is there.
    if _is_secret_path(real, is_dir=False):
        raise FsError(f"'{label}' is private and cannot be read through this tool.")
    if os.path.isdir(real):
        raise FsError(f"'{label}' is a directory, not a file. Use list_directory for folders.")
    if not os.path.exists(real):
        raise FsError(f"'{label}' does not exist.")
    if not os.path.isfile(real):
        raise FsError(f"'{label}' is not a regular file.")
    try:
        size = os.path.getsize(real)
        if size > MAX_READ_BYTES:
            raise FsError(f"'{label}' is {_human_size(size)}; files over {_human_size(MAX_READ_BYTES)} are not read.")
        with open(real, "rb") as handle:
            data = handle.read(MAX_READ_BYTES + 1)
    except OSError as exc:
        raise FsError(_os_error_message(exc, label)) from exc

    if b"\x00" in data[:8192]:
        raise FsError(f"'{label}' looks like a binary file, not text.")
    text = data.decode("utf-8-sig", errors="replace")
    if len(text) > MAX_REPLY_CHARS:
        text = text[:MAX_REPLY_CHARS] + f"\n[... cut: {len(text) - MAX_REPLY_CHARS} more characters]"
    return text if text else "(the file is empty)"


def list_directory(arguments: dict) -> str:
    real, label = resolve(arguments.get("path"), default=".")
    if _is_secret_path(real, is_dir=True):
        raise FsError(f"'{label}' is private and cannot be listed through this tool.")
    if os.path.isfile(real):
        raise FsError(f"'{label}' is a file, not a directory. Use read_file to read it.")
    if not os.path.exists(real):
        raise FsError(f"'{label}' does not exist.")
    try:
        entries = list(os.scandir(real))
    except OSError as exc:
        raise FsError(_os_error_message(exc, label)) from exc

    folders: list[str] = []
    files: list[str] = []
    for entry in entries:
        try:
            target = os.path.realpath(entry.path)
            if not _is_inside_root(target):  # a link that leads out of the root
                continue
            is_dir = os.path.isdir(target)
            if _is_secret_path(target, is_dir=is_dir):
                continue
            if is_dir:
                folders.append(entry.name + "/")
            else:
                try:
                    files.append(f"{entry.name}  ({_human_size(os.path.getsize(target))})")
                except OSError:
                    files.append(entry.name)
        except OSError:
            continue  # vanished or unreadable between scandir and now

    folders.sort(key=str.lower)
    files.sort(key=str.lower)
    lines = folders + files
    total = len(lines)
    if total > MAX_LIST_ENTRIES:
        lines = lines[:MAX_LIST_ENTRIES] + [f"[... {total - MAX_LIST_ENTRIES} more entries not shown]"]
    header = f"{label}/ - {len(folders)} folder(s), {len(files)} file(s)"
    return header + ("\n" + "\n".join(lines) if lines else "\n(empty)")


TOOLS = [
    {
        "name": "read_file",
        "title": "Read a file",
        "description": (
            "Reads a text file from the project folder and returns its contents. "
            "The path must be relative to the project root, e.g. 'PRODUCT.md' or 'plugins/example.py'. "
            "Files outside the project, binary files and secrets such as .env cannot be read."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to the project root, e.g. 'DESIGN.md'."}
            },
            "required": ["path"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "list_directory",
        "title": "List a folder",
        "description": (
            "Lists the files and folders inside a folder of the project. "
            "The path is relative to the project root; omit it or use '.' for the root itself."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Folder path relative to the project root. Default '.'."}
            },
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
    },
]

HANDLERS = {"read_file": read_file, "list_directory": list_directory}


# =====================================================================
# JSON-RPC over stdio
# =====================================================================


def text_result(text: str, *, is_error: bool = False) -> dict:
    result: dict = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


def run_tool(handler, arguments: dict) -> dict:
    try:
        return text_result(handler(arguments))
    except FsError as exc:
        return text_result(str(exc), is_error=True)
    except Exception as exc:  # a bug here must not take the server down
        log(f"unexpected {type(exc).__name__}: {exc}")
        return text_result("The file server hit an unexpected error.", is_error=True)


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

    if not isinstance(method, str):
        if request_id is not None:  # a stray response or junk - only requests get an answer
            respond(request_id, error={"code": -32600, "message": "Invalid Request"})
        return
    if request_id is None:  # a notification: nothing to answer
        if method not in ("notifications/initialized", "notifications/cancelled"):
            log(f"ignored notification {method}")
        return

    if method == "initialize":
        asked = params.get("protocolVersion")
        respond(
            request_id,
            result={
                "protocolVersion": asked if asked in SUPPORTED_VERSIONS else PROTOCOL_VERSION,
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
        handler = HANDLERS.get(name) if isinstance(name, str) else None
        if handler is None:
            respond(request_id, error={"code": -32602, "message": f"Unknown tool: {name}"})
            return
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        log(f"tools/call {name} {json.dumps(arguments, ensure_ascii=False)[:200]}")
        respond(request_id, result=run_tool(handler, arguments))
    else:
        respond(request_id, error={"code": -32601, "message": f"Method not found: {method}"})


def main() -> None:
    # Windows would otherwise use the console's code page, and a Thai file name
    # or text would arrive as mojibake.
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    log(f"ready (stdio), read-only, root = {os.path.basename(ROOT) or ROOT}")
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
            try:
                handle(message)
            except Exception as exc:
                log(f"unexpected {type(exc).__name__}: {exc}")
                if message.get("id") is not None:
                    respond(message["id"], error={"code": -32603, "message": "Internal error"})
        else:
            respond(None, error={"code": -32600, "message": "Invalid Request"})
    log("stdin closed, exiting")


if __name__ == "__main__":
    main()
