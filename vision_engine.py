"""
Module 11: On-demand vision (one screenshot, at the moment you ask)

Gives Jarvis eyes on the screen, and only when somebody asks:

    on "what's on my screen?":  grab -> downscale -> JPEG -> vision model

Nothing runs in the background. There is no capture thread, no timer,
and no buffered frame sitting in memory: the screen is read at the
instant the analyze_screen tool is called, used once, and dropped when
the answer comes back.

WHY THERE IS NO ROLLING BUFFER ANY MORE
---------------------------------------
This module used to keep a daemon grabbing a frame every few seconds so
that an answer was always ready. It bought a fraction of a second and
cost a screenshot of the user's desktop taken twenty times a minute,
whether or not anyone would ever look at it - a continuous read of
whatever happens to be on screen, including windows that have nothing
to do with the assistant.

Capturing on demand is both the private option and the correct one: the
question is always about *now*, and a frame grabbed at the moment of
asking is fresher than any buffer could be. The capture costs a few tens
of milliseconds, which is invisible next to the vision model round trip
that follows it.

THREADING
---------
None of its own. capture_now() runs on whichever thread called it -
normally the tool-execution path inside llm_brain - and the mss instance
is created and closed inside that call, because its Windows backend
keeps a per-thread device context and is not safe to share across
threads. A capture failure (locked workstation, display asleep, a GPU
driver hiccup) is counted and reported, never raised.

DEPENDENCIES
------------
    pip install mss pillow

mss for capture (fast, no X/Win32 boilerplate), Pillow for the resize
and the JPEG encode. Both are optional at import time: without them
this module reports itself unavailable and the analyze_screen tool says
so out loud instead of crashing.
"""

from __future__ import annotations

import base64
import io
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

try:
    from mss import mss as _MSS_CLASS

    _MSS_AVAILABLE = True
    _MSS_IMPORT_ERROR = ""
except Exception as exc:  # noqa: BLE001 - optional; the tool says so out loud
    _MSS_CLASS = None  # type: ignore[assignment]
    _MSS_AVAILABLE = False
    _MSS_IMPORT_ERROR = str(exc)

try:
    from PIL import Image

    _PIL_AVAILABLE = True
    _PIL_IMPORT_ERROR = ""
except Exception as exc:  # noqa: BLE001 - optional, as above
    Image = None  # type: ignore[assignment]
    _PIL_AVAILABLE = False
    _PIL_IMPORT_ERROR = str(exc)


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

# Longest edge, in pixels, sent to the vision model. A 4K screenshot
# costs a fortune in tokens and reads no better than a 1280px one.
MAX_DIMENSION = 1280

# JPEG quality for the encode. 80 keeps text on screen legible.
JPEG_QUALITY = 80

# Monitor index, mss-style: 0 = all monitors stitched together,
# 1 = primary, 2 = second, ... Overridable with JARVIS_VISION_MONITOR.
DEFAULT_MONITOR = 1


def _enabled() -> bool:
    return os.getenv("JARVIS_VISION", "1").strip().lower() not in ("0", "false", "no", "off")


def _monitor_index() -> int:
    raw = os.getenv("JARVIS_VISION_MONITOR", "").strip()
    if not raw:
        return DEFAULT_MONITOR
    try:
        return int(raw)
    except ValueError:
        print(f"[Vision] Ignoring invalid JARVIS_VISION_MONITOR={raw!r}.")
        return DEFAULT_MONITOR


def is_available() -> tuple[bool, str]:
    """(usable, reason) - reason is empty when usable."""
    if not _MSS_AVAILABLE:
        return False, f"mss is not installed ({_MSS_IMPORT_ERROR})"
    if not _PIL_AVAILABLE:
        return False, f"Pillow is not installed ({_PIL_IMPORT_ERROR})"
    if not _enabled():
        return False, "vision is disabled (JARVIS_VISION=0)"
    return True, ""


@dataclass
class Frame:
    """One captured screen, already JPEG-encoded and ready to send."""

    jpeg: bytes
    captured_at: float          # time.time(), for "how stale is this?"
    width: int
    height: int

    @property
    def age(self) -> float:
        return max(0.0, time.time() - self.captured_at)

    @property
    def size_kb(self) -> float:
        return len(self.jpeg) / 1024

    def to_base64(self) -> str:
        return base64.b64encode(self.jpeg).decode("ascii")


# ---------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------

# Counters for the capabilities tool. A lock only because a plugin could
# call capture_now() from a dispatcher thread while a tool call is doing
# the same on the main one; nothing here blocks on anything slow.
_STATS_LOCK = threading.Lock()
_CAPTURES = 0
_FAILURES = 0
_LAST_ERROR = ""
_LAST_CAPTURE_AT = 0.0


def _encode(shot) -> Frame:
    """Downscales and JPEG-encodes one raw mss grab."""
    image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    if max(image.size) > MAX_DIMENSION:
        scale = MAX_DIMENSION / max(image.size)
        image = image.resize(
            (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
            Image.LANCZOS,
        )

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return Frame(
        jpeg=buffer.getvalue(),
        captured_at=time.time(),
        width=image.width,
        height=image.height,
    )


def capture_now() -> Optional[Frame]:
    """
    Grabs the screen right now, on the calling thread.

    This is the only way a frame is ever produced. Returns None - having
    said why on the console - when vision is unavailable or the grab
    failed, so the caller can tell the user it could not look rather
    than describing a screen it never saw.
    """
    global _CAPTURES, _FAILURES, _LAST_ERROR, _LAST_CAPTURE_AT

    usable, reason = is_available()
    if not usable:
        print(f"[Vision] Cannot capture: {reason}.")
        return None

    started = time.monotonic()
    try:
        # Opened and closed inside the call: mss is not thread-safe, and
        # holding one open between questions would be a resource kept
        # alive for a feature that is idle almost all of the time.
        with _MSS_CLASS() as sct:
            monitors = sct.monitors
            index = _monitor_index()
            if index >= len(monitors):
                print(
                    f"[Vision] Monitor {index} does not exist "
                    f"({len(monitors) - 1} attached); using the primary one."
                )
                index = 1 if len(monitors) > 1 else 0
            frame = _encode(sct.grab(monitors[index]))
    except Exception as exc:  # noqa: BLE001 - locked screen, sleeping display, ...
        with _STATS_LOCK:
            _FAILURES += 1
            _LAST_ERROR = f"{type(exc).__name__}: {exc}"
        print(f"[Vision] Screen capture failed: {_LAST_ERROR}")
        return None

    with _STATS_LOCK:
        _CAPTURES += 1
        _FAILURES = 0
        _LAST_ERROR = ""
        _LAST_CAPTURE_AT = frame.captured_at

    print(
        f"[Vision] Captured the screen on demand: {frame.width}x{frame.height}, "
        f"{frame.size_kb:.0f} KB, in {(time.monotonic() - started) * 1000:.0f}ms."
    )
    return frame


def status() -> str:
    """One spoken line about the state of vision, for the status tool."""
    usable, reason = is_available()
    if not usable:
        return f"Screen vision is off: {reason}."

    with _STATS_LOCK:
        captures, failures, last_error, last_at = (
            _CAPTURES, _FAILURES, _LAST_ERROR, _LAST_CAPTURE_AT
        )

    line = (
        "Screen vision is on demand: nothing is captured in the background, "
        "and a screenshot is only taken when you ask about the screen."
    )
    if failures and last_error:
        return f"{line} The last capture failed: {last_error}."
    if not captures:
        return f"{line} Nothing has been captured this session."
    ago = max(0.0, time.time() - last_at)
    return f"{line} {captures} taken this session, the last one {ago:.0f} seconds ago."


if __name__ == "__main__":
    usable, why = is_available()
    print(f"Vision available: {usable}" + ("" if usable else f" ({why})"))
    if not usable:
        raise SystemExit(1)

    with _MSS_CLASS() as s:
        print(f"Monitors: {len(s.monitors) - 1} attached, capturing #{_monitor_index()}")

    print("\nCapturing once, on demand...")
    frame = capture_now()
    if frame is not None:
        out = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".models", "vision_test.jpg")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "wb") as handle:
            handle.write(frame.jpeg)
        print(f"\nWrote the frame to {out} ({frame.size_kb:.0f} KB).")
        print(f"base64 length: {len(frame.to_base64())} chars")
    print(f"\n{status()}")
