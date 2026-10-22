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

The same module decides what the hub may READ ALOUD (see `speech_for`): a reply
whose plain text is at most VOICE_MAX_CHARS (200) characters. Making the audio
is reply_audio.py's job (edge-tts); this module only says which words, so it
stays free of that dependency.

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
    parsed = _envelope(content)
    if parsed is None:
        return content

    names = [str(item.get("tool")) for item in _widgets_in(parsed) if isinstance(item.get("tool"), str)]
    if not names:
        return content

    text = parsed.get("text") if isinstance(parsed.get("text"), str) else ""
    note = "[Showed the user a " + ", ".join(name.replace("_", " ") for name in names) + " widget.]"
    return f"{text.strip()} {note}".strip()


def _envelope(content: str) -> Optional[dict[str, Any]]:
    """`content` as the JSON object it is, or None when it is plain prose (or JSON that is not an object)."""
    stripped = content.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return None
    try:
        parsed = json.loads(stripped)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _widgets_in(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    """The widgets an envelope carries, in any of the three shapes the page reads."""
    shown: list[dict[str, Any]] = []
    if envelope.get("type") == "widget":
        shown.append(envelope)
    if isinstance(envelope.get("widget"), dict):
        shown.append(envelope["widget"])
    if isinstance(envelope.get("widgets"), list):
        shown.extend(item for item in envelope["widgets"] if isinstance(item, dict))
    return shown


# =====================================================================
# What gets read aloud
# =====================================================================

# The hub plays a reply out loud only when its plain text is this long or shorter:
# the "conversational" size - a sentence or two, not a plan or a list of results.
# The page enforces the same number on its side (lib/jarvis/replyAudio.ts), so a
# brain that ignored it still could not make the page read an essay aloud.
VOICE_MAX_CHARS = 200

_MD_LINK = re.compile(r"\[([^\]\n]+)\]\([^)\n]*\)")
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_MD_BLOCK = re.compile(r"^\s{0,3}(?:#{1,6}|>|[-+*]|\d+[.)])\s+", re.MULTILINE)
_MD_MARKS = re.compile(r"[*`]+")


def spoken_text(reply: str) -> str:
    """
    The words of a reply, without its widgets: the sentence itself, or the `text` of a
    widget envelope ('' when the reply is only a widget). This is "the plain text
    response" the voice limit is measured on.
    """
    envelope = _envelope(reply)
    if envelope is not None and _widgets_in(envelope):
        text = envelope.get("text")
        return text.strip() if isinstance(text, str) else ""
    return reply.strip()


def speech_for(reply: str, limit: int = VOICE_MAX_CHARS) -> Optional[str]:
    """
    What to read aloud for `reply`, or None when it should not be spoken: there is
    no text, or the text is longer than `limit` characters (counted on the text as
    the person reads it, before anything is stripped for the voice). A link, or
    markdown a voice would pronounce as symbols, is taken out of what is spoken.
    """
    text = spoken_text(reply)
    if not text or len(text) > limit:
        return None

    cleaned = _MD_LINK.sub(r"\1", text)
    cleaned = _URL.sub(" ", cleaned)
    cleaned = _MD_BLOCK.sub("", cleaned)
    cleaned = _MD_MARKS.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # Nothing left that a voice could say (a bare link, only punctuation).
    return cleaned if re.search(r"\w", cleaned) else None
