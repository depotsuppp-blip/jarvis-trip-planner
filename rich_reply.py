"""
The wire format between the brain and the Trip Planner hub's rich message renderer.

The hub (TripPlanner_Web, `/jarvis`) draws a reply as text, or - when a tool
answered with more than words - as text plus widgets. It reads that from the
SAME string the brain always returned (`{ "reply": "<string>" }`): a plain
sentence stays a plain sentence, and a reply with widgets is a JSON object in
that string. The contract lives on the Next side, in
`TripPlanner_Web/lib/jarvis/messageContent.ts`; the shapes written here are the
ones it reads:

    plain reply          "Good evening."                                      (unchanged)
    text + one widget    {"text": "...", "widget": {"tool": "...", "data": {...}}}
    text + more widgets  {"text": "...", "widgets": [{"tool": "...", "data": {...}}, ...]}

A widget is `{ "tool": <snake_case name>, "data": <anything JSON>, "sample"?: true,
"title"?: <str> }`. `sample: true` tells the page the data is a demonstration and
it draws a "Sample" badge. The page validates `data` per tool; a tool it has no
view for is drawn as a placeholder, so sending an unknown tool is safe.

Only the web chat gets widgets. The voice assistant speaks, so a tool that
shows a widget on the web returns its numbers as a sentence there (see
system_status.py and request_context.add_widget).

Dependency-free, like request_context.py and link_guard.py, so the brain, a
tool and the server can all import it without a cycle.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

# The page draws at most this many widgets in one reply (lib/jarvis/messageContent.ts).
MAX_WIDGETS = 6
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_TITLE_CHARS = 80


def widget(tool: str, data: Any, *, title: Optional[str] = None, sample: bool = False) -> dict[str, Any]:
    """
    One widget, in the shape the page reads. Raises ValueError for a tool name
    the page would refuse or for data that is not JSON - a programming error
    that should show up in a test, not be shipped as an unreadable reply.
    """
    if not isinstance(tool, str) or not _TOOL_NAME.match(tool):
        raise ValueError(f"widget tool must be lower-case snake_case, got {tool!r}")
    json.dumps(data)  # TypeError if it is not serialisable

    built: dict[str, Any] = {"tool": tool, "data": data}
    if title and title.strip():
        built["title"] = title.strip()[:_MAX_TITLE_CHARS]
    if sample:
        built["sample"] = True
    return built


def wrap_reply(text: str, widgets: list[dict[str, Any]]) -> str:
    """
    The reply to return for `text` plus `widgets`. With no widgets it is `text`
    itself, untouched - so ordinary conversation is exactly what it was before
    widgets existed.
    """
    if not widgets:
        return text

    envelope: dict[str, Any] = {}
    if text and text.strip():
        envelope["text"] = text.strip()
    kept = widgets[:MAX_WIDGETS]
    if len(kept) == 1:
        envelope["widget"] = kept[0]
    else:
        envelope["widgets"] = kept
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))


def history_text(content: str) -> str:
    """
    A stored reply as the MODEL should remember it. The hub stores a widget reply
    as its JSON envelope and sends it back as history; the model should read the
    sentence it said and a note that a widget was shown, not a blob of JSON it
    might then imitate. Anything that is not a widget envelope is returned as is.
    """
    stripped = content.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return content
    try:
        parsed = json.loads(stripped)
    except ValueError:
        return content
    if not isinstance(parsed, dict):
        return content

    shown: list[Any] = []
    if parsed.get("type") == "widget":
        shown.append(parsed)
    if isinstance(parsed.get("widget"), dict):
        shown.append(parsed["widget"])
    if isinstance(parsed.get("widgets"), list):
        shown.extend(item for item in parsed["widgets"] if isinstance(item, dict))
    names = [str(item.get("tool")) for item in shown if isinstance(item.get("tool"), str)]
    if not names:
        return content

    text = parsed.get("text") if isinstance(parsed.get("text"), str) else ""
    note = "[Showed the user a " + ", ".join(name.replace("_", " ") for name in names) + " widget.]"
    return f"{text.strip()} {note}".strip()
