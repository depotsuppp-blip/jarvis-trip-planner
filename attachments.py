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

When the model could not read the file anyway - the path is not a file in
uploads/ on this machine (the web app and the brain on different machines), or
no read_file tool is connected (JARVIS_MCP=0) - the line says so instead and
tells the model to say it could not open the file, rather than answer as if it
had read it.

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

# uploads/<one file name>: no folders below it, no drive letters, streams or control characters,
# and nothing that could close the quotes or the brackets of the line it is quoted in.
_STORED_PATH = re.compile(r"^uploads/[^/\\:\x00-\x1f'\"\[\]]{1,200}$")
_UNSAFE_NAME = re.compile(r'[\x00-\x1f\x7f"\[\]]+')
MAX_NAME_CHARS = 120


class AttachmentLike(Protocol):
    path: str
    name: str


def read_tool(tool_names: Iterable[str]) -> Optional[str]:
    """The exposed name of the tool that reads files, or None when none is connected."""
    names = list(tool_names)
    if READ_TOOL in names:
        return READ_TOOL
    return next((n for n in names if n.startswith("mcp__") and n.endswith("__read_file")), None)


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


def describe(attachment: AttachmentLike, tool: Optional[str]) -> str:
    """The one line the brain reads about one attached file."""
    path = attachment.path
    if resolve(path) is None:
        # Not quoted back: a path that is not one of ours says nothing useful, and may be made up.
        name = _quoted_name(attachment.name, "the file")
        return (
            f'[System Context: The user attached a file named "{name}", but it is not on this machine, '
            "so it cannot be read. Tell the user you couldn't open it and ask them to attach it again.]"
        )
    name = _quoted_name(attachment.name, path)
    if tool is None:
        return (
            f"[System Context: The user attached a file named \"{name}\" located at '{path}', but no "
            "file-reading tool is connected right now. Tell the user you can't open files at the moment.]"
        )
    return (
        f"[System Context: The user attached a file named \"{name}\" located at '{path}'. "
        f"Use the {tool} tool to read its contents if needed.]"
    )


def with_context(text: str, attachments: Sequence[AttachmentLike], tool: Optional[str], *, current: bool = False) -> str:
    """
    `text` (the person's words) followed by one line per attached file. `current` is
    the message being answered now: a file sent with no words then gets a nudge to
    say what it is, since there is no question to answer.
    """
    words = (text or "").strip()
    if not attachments:
        return words
    lines = [describe(item, tool) for item in attachments]
    readable = tool is not None and any(resolve(item.path) for item in attachments)
    if current and not words and readable:
        lines.append("[System Context: The user sent the file without a message. Read it and briefly say what it contains.]")
    return "\n\n".join([words, *lines]) if words else "\n\n".join(lines)
