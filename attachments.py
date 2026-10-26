"""
Files the person attached in the web hub, as the brain is told about them.

The web app (TripPlanner_Web, lib/uploads.ts) saves an attached file under this
project's uploads/ folder and sends its path - "uploads/3f9a...-report.pdf",
relative to this folder - with the message (api_server.py's `attachments`). The
brain does not get the file's contents. It gets one line after the person's
words that says where the file is and which tool reads it:

    [System Context: The user attached a file named "report.pdf" located at
    'uploads/3f9a...-report.pdf'. Use the mcp__local_fs__read_file tool to read
    its contents if needed.]

and the model reads the file itself with the filesystem tool server
(mcp_filesystem.py), which runs in this same folder, reads PDF, Word and text
files, and pages long ones. The same line is added to the earlier turns that
carried a file, so a follow-up ("and page 3?") still knows where it is.

A data file - .csv, .tsv, .json, .jsonl - gets a different line when the data
tool server (mcp_data.py) is connected:

    [System Context: The user attached a data file named "sales.csv" located
    at 'uploads/3f9a...-sales.csv'. Use the mcp__data__load_file tool to load,
    query, and analyze this structured data.]

Loaded into a table, its numbers are totalled and averaged with SQL instead of
being read as text and added up in the model's head. On an earlier turn the line
also says the file may be loaded already, so a follow-up queries that table
instead of loading a second copy. Without the data server, a data file is read
like any other text file.

When the model could not read the file anyway - the path is not a file in
uploads/ on this machine (the web app and the brain on different machines), or
no read_file tool is connected (JARVIS_MCP=0) - the line says so instead and
tells the model to say it could not open the file, rather than answer as if it
had read it.

With the agent team on (agents/), the manager holds neither tool: both are the
Head of Data & Documents', reached through delegate_to_data, and the line says to
call that with the request and the file's exact path in the task:

    [System Context: The user attached a file named "report.pdf" located at
    'uploads/3f9a...-report.pdf'. To read it, call the delegate_to_data tool
    with the user's request and this exact path in the task.]

The line also keeps the turn away from the brain's front door (the cheap
router with no tools, llm_brain.LLMBrain._front_door): "file" is a word from
the read_file tool's own name, and a message that uses one goes straight to the
model that has the tools.

No dependencies beyond the standard library; nothing here raises.
"""

from __future__ import annotations

import os
import re
from typing import Iterable, Optional, Protocol, Sequence

ROOT = os.path.dirname(os.path.abspath(__file__))
UPLOADS_FOLDER = "uploads"
UPLOADS_DIR = os.path.join(ROOT, UPLOADS_FOLDER)

# The bundled filesystem server is "local_fs" (mcp_client.bundled_servers); a config
# may run it under another name, so any MCP server's read_file is the fallback.
READ_TOOL = "mcp__local_fs__read_file"
# The bundled data server is "data"; likewise, any MCP server's load_file is the fallback.
DATA_TOOL = "mcp__data__load_file"
# The manager's tool for both when the agent team is on (agents/data_agent.py): the
# Head of Data & Documents holds read_file and load_file, and gets the path in its task.
DELEGATE_TOOL = "delegate_to_data"
# What mcp_data.load_file reads that the web app also accepts (lib/jarvis/attachments.ts).
DATA_EXTENSIONS = frozenset({".csv", ".tsv", ".json", ".jsonl"})

# uploads/<one file name>: no folders below it, no drive letters, streams or control characters,
# and nothing that could close the quotes or the brackets of the line it is quoted in.
_STORED_PATH = re.compile(r"^uploads/[^/\\:\x00-\x1f'\"\[\]]{1,200}$")
_UNSAFE_NAME = re.compile(r'[\x00-\x1f\x7f"\[\]]+')
MAX_NAME_CHARS = 120


class AttachmentLike(Protocol):
    path: str
    name: str


def read_tool(tool_names: Iterable[str]) -> Optional[str]:
    """
    The exposed name of the tool that reads files, or None when none is connected.
    With the agent team on (agents/) the manager has no reader of its own: then it
    is DELEGATE_TOOL, and the Head of Data & Documents reads the file.
    """
    names = list(tool_names)
    if READ_TOOL in names:
        return READ_TOOL
    found = next((n for n in names if n.startswith("mcp__") and n.endswith("__read_file")), None)
    return found or (DELEGATE_TOOL if DELEGATE_TOOL in names else None)


def data_tool(tool_names: Iterable[str]) -> Optional[str]:
    """The exposed name of the tool that loads data files into tables (or DELEGATE_TOOL, as above), or None."""
    names = list(tool_names)
    if DATA_TOOL in names:
        return DATA_TOOL
    found = next((n for n in names if n.startswith("mcp__") and n.endswith("__load_file")), None)
    return found or (DELEGATE_TOOL if DELEGATE_TOOL in names else None)


def is_data_file(path: str) -> bool:
    """True for a file the data tool should load (by the stored file's extension)."""
    return isinstance(path, str) and os.path.splitext(path)[1].lower() in DATA_EXTENSIONS


def resolve(path: str) -> Optional[str]:
    """The real location of an uploaded file, or None unless `path` names a file inside uploads/."""
    if not isinstance(path, str) or not _STORED_PATH.match(path) or ".." in path:
        return None
    real = os.path.realpath(os.path.join(ROOT, path))
    uploads = os.path.realpath(UPLOADS_DIR)
    try:
        inside = os.path.commonpath([os.path.normcase(real), os.path.normcase(uploads)]) == os.path.normcase(uploads)
    except ValueError:  # another drive
        return None
    return real if inside and real != uploads and os.path.isfile(real) else None


def _quoted_name(name: str, path: str) -> str:
    """The person's name for the file, safe to put in quotes: no quotes, brackets or control characters."""
    clean = _UNSAFE_NAME.sub(" ", name or "").strip()
    clean = " ".join(clean.split())[:MAX_NAME_CHARS]
    return clean or path.rsplit("/", 1)[-1]


def describe(
    attachment: AttachmentLike, tool: Optional[str], data: Optional[str] = None, *, earlier: bool = False
) -> str:
    """
    The one line the brain reads about one attached file. `data` is the data
    tool (data_tool()), which a .csv/.tsv/.json/.jsonl file is pointed at when it
    is connected; `earlier` marks a file from a previous turn.
    """
    path = attachment.path
    if resolve(path) is None:
        # Not quoted back: a path that is not one of ours says nothing useful, and may be made up.
        name = _quoted_name(attachment.name, "the file")
        return (
            f'[System Context: The user attached a file named "{name}", but it is not on this machine, '
            "so it cannot be read. Tell the user you couldn't open it and ask them to attach it again.]"
        )
    name = _quoted_name(attachment.name, path)
    if DELEGATE_TOOL in (tool, data):
        # The team is on: the manager hands the file to the Head of Data & Documents.
        kind, work = ("data file", "load it as a table and compute with SQL") if is_data_file(path) else ("file", "read it")
        return (
            f"[System Context: The user attached a {kind} named \"{name}\" located at '{path}'. "
            f"To {work}, call the {DELEGATE_TOOL} tool with the user's request and this exact path in the task.]"
        )
    if data is not None and is_data_file(path):
        again = (
            " It may have been loaded in an earlier turn already: check the existing tables before loading it again."
            if earlier
            else ""
        )
        return (
            f"[System Context: The user attached a data file named \"{name}\" located at '{path}'. "
            f"Use the {data} tool to load, query, and analyze this structured data.{again}]"
        )
    if tool is None:
        return (
            f"[System Context: The user attached a file named \"{name}\" located at '{path}', but no "
            "file-reading tool is connected right now. Tell the user you can't open files at the moment.]"
        )
    return (
        f"[System Context: The user attached a file named \"{name}\" located at '{path}'. "
        f"Use the {tool} tool to read its contents if needed.]"
    )


def with_context(
    text: str,
    attachments: Sequence[AttachmentLike],
    tool: Optional[str],
    *,
    current: bool = False,
    data: Optional[str] = None,
) -> str:
    """
    `text` (the person's words) followed by one line per attached file. `current` is
    the message being answered now: a file sent with no words then gets a nudge to
    say what it is, since there is no question to answer. `data` is the data tool
    for .csv/.tsv/.json/.jsonl files (see describe).
    """
    words = (text or "").strip()
    if not attachments:
        return words
    lines = [describe(item, tool, data, earlier=not current) for item in attachments]
    readable = any(
        resolve(item.path) and (tool is not None or (data is not None and is_data_file(item.path)))
        for item in attachments
    )
    if current and not words and readable:
        lines.append("[System Context: The user sent the file without a message. Read it and briefly say what it contains.]")
    return "\n\n".join([words, *lines]) if words else "\n\n".join(lines)
