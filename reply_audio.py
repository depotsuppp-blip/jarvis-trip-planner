"""
Spoken replies for the web hub: Edge-TTS turns a short reply into MP3 bytes.

The voice assistant (voice_engine.py) already speaks through Microsoft Edge's
neural voices. This is the same voice for the web chat, reached differently: the
brain cannot play sound in the person's browser, so the audio travels back in the
chat response (api_server.py: `audio: {mime, base64}`) and the page plays it.

WHAT IS SPOKEN: ENGLISH, ALWAYS
-------------------------------
Only when the page asked for it (`speak: true` - the person has voice replies on).
The chat may show a reply in Thai, but the voice never reads it: the brain makes an
English spoken version of every reply (llm_brain: `spoken` in the final event - the
reply itself when it is already short English, else translated and summarized to at
most rich_reply.VOICE_MAX_CHARS (200) characters), and api_server.py hands only that
to for_speech. The voice is always the English one (EDGE_TTS_VOICE); a text with
letters of another script is refused rather than read out in an English voice.

HOW IT FAILS
------------
Never loudly. No network, a timeout, a Microsoft error or a missing package all
mean "no audio": `for_speech` returns None and the person simply gets the text,
as they always did. Synthesis runs after the brain has answered and outside its
lock, so a slow voice never holds up another person's message.

WHAT LEAVES THE MACHINE
-----------------------
The reply's text goes to Microsoft's Edge-TTS service to be voiced - the one place
the web chat's replies leave this machine. JARVIS_WEB_TTS=0 turns it off for the
web chat, and JARVIS_TTS_OFFLINE_ONLY=1 (the voice assistant's "nothing leaves the
machine" switch) turns it off here too.

SETTINGS (.env, all optional)
-----------------------------
    JARVIS_WEB_TTS=0            no spoken replies in the web chat
    JARVIS_WEB_TTS_TIMEOUT      seconds to wait for the voice (default 10)
    EDGE_TTS_VOICE              the voice, English (default en-GB-RyanNeural, as the assistant)
    EDGE_TTS_RATE / _VOLUME / _PITCH   prosody, shared with the assistant

edge-tts must be 7.3.1 or newer: 7.2.8 and older are refused by Microsoft's service
with HTTP 403 (see requirements.txt).
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import os
from typing import Optional

import rich_reply

try:
    import edge_tts

    _EDGE_ERROR = ""
except Exception as exc:  # noqa: BLE001 - no edge-tts just means no spoken replies
    edge_tts = None  # type: ignore[assignment]
    _EDGE_ERROR = f"{type(exc).__name__}: {exc}"

MIME = "audio/mpeg"
DEFAULT_VOICE = "en-GB-RyanNeural"
DEFAULT_TIMEOUT = 10.0
# 200 characters is about 15 seconds of speech, well under 200 KB; this only stops a
# misbehaving service from filling a JSON response.
MAX_AUDIO_BYTES = 1_500_000



def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _timeout() -> float:
    try:
        return max(1.0, float(_env("JARVIS_WEB_TTS_TIMEOUT", str(DEFAULT_TIMEOUT))))
    except ValueError:
        return DEFAULT_TIMEOUT


def unavailable_reason() -> str:
    """Why replies are not spoken, or '' when they can be."""
    if _env("JARVIS_WEB_TTS", "1").lower() in ("0", "false", "no", "off"):
        return "turned off (JARVIS_WEB_TTS=0)"
    if _flag("JARVIS_TTS_OFFLINE_ONLY"):
        return "turned off (JARVIS_TTS_OFFLINE_ONLY is set)"
    if edge_tts is None:
        return f"edge-tts is not installed ({_EDGE_ERROR})"
    return ""


def enabled() -> bool:
    return not unavailable_reason()


def pick_voice(text: str = "") -> str:
    """The English voice - for every text: what is spoken is English (see the top)."""
    return _env("EDGE_TTS_VOICE", DEFAULT_VOICE)


def is_english(text: str) -> bool:
    """True when every letter is a Latin one (the same test as llm_brain.is_english)."""
    return not any(ch.isalpha() and ord(ch) >= 0x250 for ch in text)


async def _collect(text: str, voice: str) -> bytes:
    communicate = edge_tts.Communicate(
        text,
        voice=voice,
        rate=_env("EDGE_TTS_RATE", "+0%"),
        volume=_env("EDGE_TTS_VOLUME", "+0%"),
        pitch=_env("EDGE_TTS_PITCH", "+0Hz"),
    )
    buffer = bytearray()
    # edge-tts delivers the audio as it is synthesised; the chunks are gathered here
    # because the response carries one finished clip.
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            buffer.extend(chunk["data"])
            if len(buffer) > MAX_AUDIO_BYTES:
                raise ValueError("the clip is larger than expected")
    return bytes(buffer)


def _run(text: str, voice: str) -> bytes:
    """Runs the synthesis to completion on a fresh event loop, whatever thread this is."""

    def work() -> bytes:
        return asyncio.run(asyncio.wait_for(_collect(text, voice), timeout=_timeout()))

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return work()  # the normal case: a worker thread with no loop of its own
    # Called from inside a running loop (async code): asyncio.run is not allowed there,
    # so use a helper thread. This blocks that loop for the round trip - see synthesize().
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(work).result()


def synthesize(text: str, voice: Optional[str] = None) -> Optional[bytes]:
    """
    MP3 bytes for `text`, or None. Never raises. Blocks for the length of the
    network round trip (about a second and a half), so call it from a worker
    thread, as FastAPI does for a plain `def` endpoint.
    """
    if not text.strip() or not enabled():
        return None
    voice = voice or pick_voice(text)
    try:
        audio = _run(text, voice)
    except (asyncio.TimeoutError, TimeoutError):
        print(f"[TTS] Edge-TTS timed out after {_timeout():.0f}s - sending the reply without audio.")
        return None
    except Exception as exc:  # noqa: BLE001 - whatever went wrong, the text still goes out
        print(f"[TTS] Edge-TTS failed ({type(exc).__name__}: {exc}) - sending the reply without audio.")
        return None
    return audio or None


def for_speech(spoken: str) -> Optional[dict[str, str]]:
    """
    The `audio` field for a chat response - {"mime": "audio/mpeg", "base64": "..."} - for the
    brain's English spoken text (never the chat reply itself), or None: nothing to say, longer
    than rich_reply.VOICE_MAX_CHARS, not English, voice unavailable or failed.
    """
    if not enabled():
        return None
    speech = " ".join((spoken or "").split())
    if not speech or len(speech) > rich_reply.VOICE_MAX_CHARS:
        return None
    if not is_english(speech):
        print("[TTS] Refused a spoken text that is not English - the voice speaks English only.")
        return None
    audio = synthesize(speech)
    if audio is None:
        return None
    return {"mime": MIME, "base64": base64.b64encode(audio).decode("ascii")}
