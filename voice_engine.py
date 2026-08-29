"""
Module 3: Voice I/O - Hybrid TTS (cloud + offline) and offline STT

Gives Jarvis a mouth and ears. The ears stay 100% on-device; the mouth
is now a three-tier hybrid that trades cost for quality automatically:

    speak(text) -> ElevenLabs   (premium British male, short replies)
                -> Edge-TTS     (free neural en-GB, long replies)
                -> pyttsx3      (offline SAPI5, no network at all)
                -> print()      (no audio stack whatsoever)

    listen()    -> Vosk         (local Kaldi recogniser)

This module is the STANDBY half of the hybrid speech-to-text stack.
Vosk spots the wake phrase cheaply; whisper_manager.py takes over for
ACTIVE-mode commands, where Thai and Thai-English code-switching matter.
stream_utterances(detailed=True) is the seam between them: it hands back
the raw PCM and Vosk word timings that let session_manager slice the
audio after a wake phrase and pass it to Whisper unheard-by-Vosk.

-------------------------------------------------------------------
WHY THE TTS ROUTES BY LENGTH
-------------------------------------------------------------------
ElevenLabs bills per character and is the best-sounding option, so it
is worth spending on the short conversational replies the user hears
most often. Long passages - a read-back of a database result, an error
explanation - would burn quota fast for a voice nobody listens to
closely, so anything at or over TTS_PREMIUM_MAX_CHARS goes to Edge-TTS
instead. Edge-TTS is free, neural, and also British.

The routing is a fallback *chain*, not a switch: any tier that is
unconfigured, uninstalled, or fails at runtime hands off to the next
one down. Every hand-off prints a "[Voice] Route:" line so the routing
is observable from the console.

-------------------------------------------------------------------
ASYNC MODEL
-------------------------------------------------------------------
Both cloud backends are network calls, and edge-tts is natively
asyncio. The real implementation is therefore `speak_async()`.

Because the rest of Jarvis is synchronous, this module owns a private
event loop running on a daemon thread, and exposes three entry points:

    await speak_async(text)   the async implementation
    speak(text)               blocks until the audio finishes
    speak_nowait(text)        returns immediately, audio plays behind you

Nothing here runs synthesis on the calling thread, and an asyncio.Lock
serialises playback so two utterances never talk over each other.

-------------------------------------------------------------------
WHY SpeechRecognition IS GONE
-------------------------------------------------------------------
This module previously used the SpeechRecognition package, whose
default backend uploads recorded audio to Google's web endpoint. That
contradicted the project's offline guarantee for *listening*.

The fix is structural rather than a policy choice: SpeechRecognition is
no longer imported at all, so there is no code path from this module to
any cloud recogniser. Audio goes microphone -> PyAudio -> Vosk -> text,
entirely in this process. Nothing leaves the machine.

Note the asymmetry, because it is deliberate: speech *out* may now use
the network (that is the point of the hybrid engine), but speech *in*
never does. Set JARVIS_TTS_OFFLINE_ONLY=1 to force the mouth offline
too, which restores the original no-network-at-all behaviour.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Iterator, Optional, Union

try:  # Windows only; elsewhere cached fillers play through MCI/ffplay.
    import winsound
except ImportError:  # pragma: no cover - non-Windows
    winsound = None  # type: ignore[assignment]


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

# The routing threshold. Text shorter than this goes to ElevenLabs;
# anything at or over it goes to Edge-TTS.
TTS_PREMIUM_MAX_CHARS = 200

# ElevenLabs voice, by display name. Resolved to a voice ID against the
# account's library at first use; the IDs below are the fallback for
# when that lookup is unavailable.
ELEVENLABS_VOICE_NAME = "George"

# Known IDs for ElevenLabs' stock British male voices. Used only when
# the /v1/voices lookup fails, so a network hiccup during resolution
# doesn't cost us the premium tier.
ELEVENLABS_KNOWN_VOICES = {
    "george": "JBFqnCBsd6RMkjVDRZzb",   # warm, mature British male
    "callum": "N2lVS1w4EtoT3dr4eOWO",   # younger, gravelly British male
}

# Turbo is the latency-optimised model - the right trade for an
# assistant the user is waiting on. Swap to "eleven_flash_v2_5" for
# even lower latency, or "eleven_multilingual_v2" for top quality.
ELEVENLABS_MODEL = "eleven_turbo_v2_5"

# 128 kbps MP3: indistinguishable over speakers, small enough to keep
# the download inside the latency budget.
ELEVENLABS_OUTPUT_FORMAT = "mp3_44100_128"

# Seconds to wait on the ElevenLabs synthesis call before giving up and
# dropping to Edge-TTS. Kept tight: a slow premium voice is worse than
# a fast free one when someone is standing there waiting.
ELEVENLABS_TIMEOUT = 12.0

# Free neural British male. Alternatives: en-GB-ThomasNeural (male),
# en-GB-SoniaNeural / en-GB-LibbyNeural (female).
EDGE_TTS_VOICE = "en-GB-RyanNeural"

# Spoken the instant a command has been captured - before Whisper has
# even started decoding it. Synthesising these through Edge-TTS at the
# moment they are needed would add the 1-2 s network round trip to the
# gap they exist to hide, so they are synthesised once at startup and
# cached on disk; play_filler() then only has to hand a local file to
# the OS. Short by design: this plays while the brain is still working,
# and the real answer queues behind it.
#
# Deliberately NEUTRAL. Nothing here has been transcribed yet, so the
# phrase has to fit a question as well as an instruction: "On it" and
# "Right away" answer a command that may turn out to have been "what
# time is it?", which sounds like Jarvis misheard.
FILLER_PHRASES: tuple[str, ...] = (
    "Just a moment.",
    "Let me check.",
    "One moment, sir.",
    "Thinking.",
    "Bear with me.",
)

# Speech shorter than this gets the beep but no spoken filler.
#
# The filler is a guess made before transcription, and the only signal
# available at that point is how long the person spoke for. Under a
# couple of seconds is almost always a pleasantry ("thank you"), a
# confirmation ("yes, go ahead"), or a one-line question - all of which
# are answered fast enough that a filler would collide with the answer,
# and all of which sound absurd prefixed with "Let me check". Longer
# than that is a real command, whose decode plus reply is where the
# dead air actually lives.
FILLER_MIN_SECONDS = 2.5

# The same gate for the Vosk path, which has a transcript instead of a
# duration. Roughly the word count of FILLER_MIN_SECONDS of speech.
FILLER_MIN_WORDS = 6

# Sample rate of the cached WAVs. Edge-TTS synthesises at 24 kHz, so
# anything else here is a needless resample.
FILLER_WAV_RATE = 24000

# The "mic is listening" cue: a short, quiet chime (not a filler phrase -
# the user needs "the mic is open now", not a sentence) played the
# instant ACTIVE opens the mic for a new command. See
# VoiceEngine.play_listening_cue and session_manager._active.
#
# Deliberately NOT hooked into stream_utterances()'s own "Listening..."
# print (voice_engine.py) or whisper_manager.py's - both of those fire
# for STANDBY's wake-word listening too, and beeping on every idle
# wake-word cycle would be constant noise, not a "mic is hot" cue.
# session_manager._active is the one place that unambiguously means
# ACTIVE, not STANDBY.
#
# Two ascending sine-wave notes (A4 -> C5), not a single square-wave
# tone: winsound.Beep is a raw square wave with instant on/off edges,
# which is exactly what reads as "harsh and robotic" - the buzzy
# harmonics of a square wave, and the click at each hard edge. A sine
# wave has neither, and a two-note rise reads as an affirming chime
# rather than an alarm. See _synthesize_listening_chime.
LISTENING_CUE_NOTES: tuple[tuple[float, float], ...] = ((440.0, 0.10), (523.25, 0.10))

# Fade in/out per note, in seconds. The click at a hard-edged tone's
# start/stop is most of what makes a beep sound harsh - a linear fade
# removes it almost entirely at a cost too short to blunt the cue.
LISTENING_CUE_FADE_SECONDS = 0.01

# Peak amplitude as a fraction of full scale. "Subtle" per the brief -
# a cue loud enough to startle defeats the point of a gentle one.
LISTENING_CUE_VOLUME = 0.25

# Sample rate for the synthesised chime. Matches FILLER_WAV_RATE below;
# a cue this short does not benefit from a higher rate.
LISTENING_CUE_SAMPLE_RATE = 24000

# winsound.Beep fallback, used only if the synthesised chime cannot be
# written or played at all (e.g. no disk access). Rougher than the
# chime, but still better than silence.
LISTENING_CUE_FREQUENCY_HZ = 800
LISTENING_CUE_DURATION_MS = 200

# Cached filler audio lives next to this file, not in the system temp
# directory: it must survive a reboot, or the first command after every
# restart pays the full network cost again.
FILLER_CACHE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".cache", "fillers"
)

# The synthesised chime is cached next to the fillers, but named and
# treated separately - see _synthesize_listening_chime. It is not
# spoken audio, and FillerCache's phrase-based lookup does not apply.
LISTENING_CUE_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".cache", "cues", "listening_chime.wav"
)

# Edge-TTS prosody, in its own "+N%" / "+NHz" notation.
EDGE_TTS_RATE = "+0%"
EDGE_TTS_VOLUME = "+0%"
EDGE_TTS_PITCH = "+0Hz"

# Seconds to wait on Edge-TTS before dropping to offline pyttsx3.
EDGE_TTS_TIMEOUT = 20.0

# Words per minute for the offline pyttsx3 voice (its default is ~200,
# which sounds rushed for an assistant).
TTS_RATE = 175

# Substring match against installed system voices, e.g. "Zira" or
# "David" on Windows. None = keep the OS default voice.
TTS_VOICE_HINT: Optional[str] = None

# Vosk expects 16 kHz mono 16-bit PCM - the same format the wake word
# engine already captures.
SAMPLE_RATE = 16000

# Frames per read. 4000 (0.25 s) is Vosk's recommended chunk: large
# enough to be efficient, small enough to stay responsive.
CHUNK_SIZE = 4000

# Give up if speech hasn't STARTED within this many seconds.
LISTEN_TIMEOUT = 8.0

# Hard cap on a single spoken command, once speech has started.
LISTEN_PHRASE_LIMIT = 15.0

# Download the Vosk model automatically on first use if it's absent.
# Set False to require an explicit `python vosk_manager.py` instead.
AUTO_DOWNLOAD_MODEL = True


@dataclass
class Utterance:
    """
    One recognised utterance, with the audio it was recognised from.

    Vosk's text is not the end of the story any more. In STANDBY the
    text is only used to spot the wake phrase; the audio that followed
    it is handed to Whisper, which is the recogniser that actually
    understands Thai. Keeping the PCM alongside the transcript is what
    makes that handoff possible.

    `words` is Vosk's per-word timing list ({"word", "start", "end"}),
    and both those times and `stream_start` are measured in seconds
    from the start of the capture stream, not from this utterance.
    """

    text: str
    audio: bytes = b""
    words: list = field(default_factory=list)
    stream_start: float = 0.0

    def __str__(self) -> str:
        return self.text

    @property
    def duration(self) -> float:
        return len(self.audio) / 2 / SAMPLE_RATE

    def audio_after(self, stream_time: float) -> bytes:
        """
        The PCM from `stream_time` (a Vosk word-end time) onwards.

        Returns b"" when that point is at or past the end of the
        utterance - i.e. the user said the wake phrase and nothing else.
        """
        offset_seconds = max(0.0, stream_time - self.stream_start)
        # x2: 16-bit samples are two bytes each.
        index = int(offset_seconds * SAMPLE_RATE) * 2
        return self.audio[index:] if index < len(self.audio) else b""


# ---------------------------------------------------------------------
# Optional dependency probing (never hard-fail at import time)
# ---------------------------------------------------------------------

try:
    import pyttsx3

    _TTS_AVAILABLE = True
    _TTS_IMPORT_ERROR = ""
except Exception as exc:  # noqa: BLE001 - any import-time failure disables TTS
    pyttsx3 = None  # type: ignore[assignment]
    _TTS_AVAILABLE = False
    _TTS_IMPORT_ERROR = str(exc)

try:
    import edge_tts

    _EDGE_AVAILABLE = True
    _EDGE_IMPORT_ERROR = ""
except Exception as exc:  # noqa: BLE001 - no edge-tts means one fewer tier
    edge_tts = None  # type: ignore[assignment]
    _EDGE_AVAILABLE = False
    _EDGE_IMPORT_ERROR = str(exc)

try:
    import httpx

    _HTTPX_AVAILABLE = True
    _HTTPX_IMPORT_ERROR = ""
except Exception as exc:  # noqa: BLE001 - no httpx means no ElevenLabs tier
    httpx = None  # type: ignore[assignment]
    _HTTPX_AVAILABLE = False
    _HTTPX_IMPORT_ERROR = str(exc)

try:
    from vosk import KaldiRecognizer, Model, SetLogLevel

    # Silence Kaldi's very chatty startup logging.
    SetLogLevel(-1)
    _VOSK_AVAILABLE = True
    _VOSK_IMPORT_ERROR = ""
except Exception as exc:  # noqa: BLE001 - any import-time failure disables STT
    KaldiRecognizer = Model = None  # type: ignore[assignment]
    _VOSK_AVAILABLE = False
    _VOSK_IMPORT_ERROR = str(exc)

try:
    import pyaudio

    _AUDIO_AVAILABLE = True
    _AUDIO_IMPORT_ERROR = ""
except Exception as exc:  # noqa: BLE001 - no mic capture without PyAudio
    pyaudio = None  # type: ignore[assignment]
    _AUDIO_AVAILABLE = False
    _AUDIO_IMPORT_ERROR = str(exc)

try:
    from dotenv import load_dotenv

    _DOTENV_AVAILABLE = True
except Exception:  # noqa: BLE001 - .env loading is a convenience, not a requirement
    load_dotenv = None  # type: ignore[assignment]
    _DOTENV_AVAILABLE = False


def _env_path() -> str:
    """Absolute path to the project's .env file."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


_ENV_LOADED = False


def _load_env() -> None:
    """
    Reads .env once per process. override=False so an already-exported
    environment variable beats the file, which is what you want in CI.
    """
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    if _DOTENV_AVAILABLE:
        load_dotenv(_env_path(), override=False)
    _ENV_LOADED = True


def _env(name: str, default: str = "") -> str:
    _load_env()
    return (os.environ.get(name) or "").strip() or default


def _env_flag(name: str) -> bool:
    return _env(name).lower() in ("1", "true", "yes", "on")


def _int_env(name: str, default: int) -> int:
    """Reads an integer setting, ignoring anything unparseable."""
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[Voice] {name}={raw!r} is not a number; using {default}.")
        return default


def elevenlabs_api_key() -> Optional[str]:
    """ELEVENLABS_API_KEY from the environment or .env, or None."""
    return _env("ELEVENLABS_API_KEY") or None


# ---------------------------------------------------------------------
# Background event loop
# ---------------------------------------------------------------------
#
# Synthesis is async, the assistant is not. Rather than spinning up a
# fresh loop per utterance (which would re-open a TCP connection every
# time), the module keeps one loop alive on a daemon thread and posts
# coroutines to it.


class _LoopThread:
    """A single asyncio loop running on a daemon thread, created lazily."""

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is not None and not self._loop.is_closed():
                return self._loop

            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._run,
                args=(self._loop,),
                name="jarvis-voice-loop",
                daemon=True,
            )
            self._thread.start()
            return self._loop

    @staticmethod
    def _run(loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    def submit(self, coro):
        """Schedules `coro` on the background loop, returning a Future."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop())

    def shutdown(self) -> None:
        """Stops the loop and joins the thread. Safe to call twice."""
        with self._lock:
            loop, thread = self._loop, self._thread
            self._loop = self._thread = None

        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5.0)
        try:
            loop.close()
        except Exception:  # noqa: BLE001 - teardown must stay quiet
            pass


_LOOP = _LoopThread()

# Serialises playback so overlapping speak() calls queue instead of
# overlapping. Created inside the background loop on first use, because
# an asyncio.Lock binds to whichever loop is running when it is awaited.
_SPEAK_LOCK: Optional[asyncio.Lock] = None


def _speak_lock() -> asyncio.Lock:
    global _SPEAK_LOCK
    if _SPEAK_LOCK is None:
        _SPEAK_LOCK = asyncio.Lock()
    return _SPEAK_LOCK


# ---------------------------------------------------------------------
# Audio playback
# ---------------------------------------------------------------------

_MCI_COUNTER = 0


def _play_with_mci(path: str) -> bool:
    """
    Plays an MP3 through Windows' built-in MCI interface.

    Chosen over pygame/playsound because it needs no extra dependency:
    winmm ships with Windows. Returns False (rather than raising) if
    MCI refuses the file, so the caller can try something else.
    """
    global _MCI_COUNTER
    import ctypes

    try:
        mci = ctypes.windll.winmm.mciSendStringW
    except Exception:  # noqa: BLE001 - not Windows, or no winmm
        return False

    _MCI_COUNTER += 1
    alias = f"jarvis_tts_{os.getpid()}_{_MCI_COUNTER}"

    # "mpegvideo" is the MCI driver that handles MP3 on Windows.
    if mci(f'open "{path}" type mpegvideo alias {alias}', None, 0, None) != 0:
        return False
    try:
        # "wait" blocks until playback finishes. This runs inside a
        # worker thread, so the event loop is never blocked.
        return mci(f"play {alias} wait", None, 0, None) == 0
    finally:
        mci(f"close {alias}", None, 0, None)


def _play_with_subprocess(path: str) -> bool:
    """Falls back to whichever command-line player exists on this box."""
    candidates = (
        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path],
        ["afplay", path],           # macOS
        ["mpg123", "-q", path],
        ["mpv", "--no-video", "--really-quiet", path],
    )
    for cmd in candidates:
        try:
            completed = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except FileNotFoundError:
            continue
        except Exception as exc:  # noqa: BLE001 - a broken player is not fatal
            print(f"[Voice] Player {cmd[0]} failed: {exc}")
            continue
        if completed.returncode == 0:
            return True
    return False


def _play_file_blocking(path: str) -> bool:
    """Plays an audio file to completion. Returns False if nothing could."""
    if sys.platform == "win32" and _play_with_mci(path):
        return True
    return _play_with_subprocess(path)


async def _play_bytes(audio: bytes, suffix: str = ".mp3") -> bool:
    """
    Writes `audio` to a temp file and plays it without blocking the loop.

    The file is deleted afterwards even if playback fails; on Windows
    the handle must be closed before MCI can open it, hence delete=False
    plus an explicit unlink rather than a context manager.
    """
    if not audio:
        return False

    handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        handle.write(audio)
        handle.close()
        return await asyncio.to_thread(_play_file_blocking, handle.name)
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass  # a player may still hold the handle; the OS reaps it later


# ---------------------------------------------------------------------
# Tier 1: ElevenLabs
# ---------------------------------------------------------------------

_VOICE_ID_CACHE: dict[str, str] = {}


async def _resolve_elevenlabs_voice(client, api_key: str, name: str) -> Optional[str]:
    """
    Maps an ElevenLabs voice display name to its ID.

    Looks the name up in the account's library so a renamed or cloned
    voice still resolves, and falls back to the known stock IDs when
    the lookup is unavailable. Cached for the life of the process.
    """
    key = name.strip().lower()
    if key in _VOICE_ID_CACHE:
        return _VOICE_ID_CACHE[key]

    try:
        response = await client.get(
            "https://api.elevenlabs.io/v1/voices",
            headers={"xi-api-key": api_key},
        )
        response.raise_for_status()
        for voice in response.json().get("voices", []):
            if str(voice.get("name", "")).strip().lower() == key:
                voice_id = str(voice.get("voice_id", "")).strip()
                if voice_id:
                    _VOICE_ID_CACHE[key] = voice_id
                    print(f"[Voice] ElevenLabs voice '{name}' resolved to {voice_id}.")
                    return voice_id
    except Exception as exc:  # noqa: BLE001 - lookup is best-effort
        print(f"[Voice] Could not list ElevenLabs voices ({exc}); using the built-in ID.")

    fallback = ELEVENLABS_KNOWN_VOICES.get(key)
    if fallback:
        _VOICE_ID_CACHE[key] = fallback
        return fallback

    print(
        f"[Voice] Unknown ElevenLabs voice '{name}'. "
        f"Known names: {', '.join(sorted(ELEVENLABS_KNOWN_VOICES))}."
    )
    return None


async def _synthesize_elevenlabs(text: str, voice_name: str) -> Optional[bytes]:
    """
    Returns MP3 bytes from ElevenLabs, or None on any failure.

    Never raises: the caller's whole purpose is to fall through to the
    next tier, and it can only do that if it gets a value back.
    """
    api_key = elevenlabs_api_key()
    if not api_key or not _HTTPX_AVAILABLE:
        return None

    try:
        async with httpx.AsyncClient(timeout=ELEVENLABS_TIMEOUT) as client:
            voice_id = await _resolve_elevenlabs_voice(client, api_key, voice_name)
            if voice_id is None:
                return None

            response = await client.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                params={"output_format": ELEVENLABS_OUTPUT_FORMAT},
                headers={"xi-api-key": api_key, "Content-Type": "application/json"},
                json={
                    "text": text,
                    "model_id": _env("ELEVENLABS_MODEL", ELEVENLABS_MODEL),
                    "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
                },
            )

            if response.status_code == 401:
                print("[Voice] ElevenLabs rejected the API key (401).")
                return None
            if response.status_code == 429:
                print("[Voice] ElevenLabs quota or rate limit hit (429).")
                return None
            response.raise_for_status()
            return response.content
    except Exception as exc:  # noqa: BLE001 - every failure means "next tier"
        print(f"[Voice] ElevenLabs synthesis failed: {type(exc).__name__}: {exc}")
        return None


# ---------------------------------------------------------------------
# Tier 2: Edge-TTS
# ---------------------------------------------------------------------


async def _synthesize_edge(text: str, voice: str) -> Optional[bytes]:
    """Returns MP3 bytes from Microsoft Edge's neural voices, or None."""
    if not _EDGE_AVAILABLE:
        return None

    try:
        communicate = edge_tts.Communicate(
            text,
            voice=voice,
            rate=_env("EDGE_TTS_RATE", EDGE_TTS_RATE),
            volume=_env("EDGE_TTS_VOLUME", EDGE_TTS_VOLUME),
            pitch=_env("EDGE_TTS_PITCH", EDGE_TTS_PITCH),
        )

        async def collect() -> bytes:
            buffer = bytearray()
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    buffer.extend(chunk["data"])
            return bytes(buffer)

        return await asyncio.wait_for(collect(), timeout=EDGE_TTS_TIMEOUT)
    except asyncio.TimeoutError:
        print(f"[Voice] Edge-TTS timed out after {EDGE_TTS_TIMEOUT:.0f}s.")
        return None
    except Exception as exc:  # noqa: BLE001 - every failure means "next tier"
        print(f"[Voice] Edge-TTS synthesis failed: {type(exc).__name__}: {exc}")
        return None


# ---------------------------------------------------------------------
# Zero-latency filler playback
# ---------------------------------------------------------------------

# MCI aliases opened without "wait" outlive the call that started them,
# so they are closed on the next play rather than immediately. One entry
# is enough: fillers are never overlapped.
_ASYNC_MCI_ALIASES: list[str] = []


def _close_async_mci() -> None:
    """Closes whatever the previous fire-and-forget MCI play left open."""
    global _ASYNC_MCI_ALIASES
    if not _ASYNC_MCI_ALIASES:
        return
    import ctypes

    try:
        mci = ctypes.windll.winmm.mciSendStringW
    except Exception:  # noqa: BLE001 - not Windows
        _ASYNC_MCI_ALIASES = []
        return
    for alias in _ASYNC_MCI_ALIASES:
        mci(f"close {alias}", None, 0, None)
    _ASYNC_MCI_ALIASES = []


def _play_with_mci_async(path: str) -> bool:
    """
    Starts playback and returns immediately, unlike _play_with_mci.

    This is the whole point of the filler cache: the caller is about to
    spend several seconds in Whisper, and must not spend any of them
    waiting on audio.
    """
    global _MCI_COUNTER
    import ctypes

    try:
        mci = ctypes.windll.winmm.mciSendStringW
    except Exception:  # noqa: BLE001 - not Windows, or no winmm
        return False

    _close_async_mci()
    _MCI_COUNTER += 1
    alias = f"jarvis_filler_{os.getpid()}_{_MCI_COUNTER}"

    driver = "waveaudio" if path.lower().endswith(".wav") else "mpegvideo"
    if mci(f'open "{path}" type {driver} alias {alias}', None, 0, None) != 0:
        return False
    if mci(f"play {alias}", None, 0, None) != 0:
        mci(f"close {alias}", None, 0, None)
        return False

    _ASYNC_MCI_ALIASES.append(alias)
    return True


def play_file_async(path: str) -> bool:
    """
    Plays a local audio file without blocking. Returns False if nothing
    on this machine could start it.

    WAV goes through winsound, which is the cheapest path Windows has -
    no decoder, no MCI device to open. Anything else (an MP3 we could
    not convert) falls back to MCI's non-blocking play.
    """
    if not path or not os.path.exists(path):
        return False

    if winsound is not None and path.lower().endswith(".wav"):
        try:
            winsound.PlaySound(
                path,
                winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
            )
            return True
        except Exception as exc:  # noqa: BLE001 - fall through to MCI
            print(f"[Voice] winsound playback failed: {type(exc).__name__}: {exc}")

    if sys.platform == "win32" and _play_with_mci_async(path):
        return True

    # Non-Windows: hand it to a player in the background. Never waited on.
    for cmd in (
        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path],
        ["afplay", path],
        ["mpg123", "-q", path],
    ):
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except FileNotFoundError:
            continue
        except Exception:  # noqa: BLE001 - try the next player
            continue
    return False


def _synthesize_listening_chime() -> bytes:
    """
    Builds the "mic is listening" chime as 16-bit mono PCM WAV bytes.

    Two sine-wave notes (LISTENING_CUE_NOTES), each faded in and out
    over LISTENING_CUE_FADE_SECONDS rather than switched on and off
    abruptly. Both choices are deliberate, not decorative: a sine wave
    has none of a square wave's buzzy harmonics, and the fade removes
    the click a hard edge makes at the start and end of a tone - between
    them, that harmonic buzz and that click are most of what makes
    winsound.Beep read as "harsh and robotic" rather than a soft chime.
    Uses only the standard library (wave, array, math) - no new runtime
    dependency for a sound this simple.
    """
    import array
    import math
    import wave

    sample_rate = LISTENING_CUE_SAMPLE_RATE
    fade_samples = max(1, int(sample_rate * LISTENING_CUE_FADE_SECONDS))

    samples = array.array("h")
    for frequency, duration in LISTENING_CUE_NOTES:
        note_samples = int(sample_rate * duration)
        for i in range(note_samples):
            envelope = 1.0
            if i < fade_samples:
                envelope = i / fade_samples
            elif i > note_samples - fade_samples:
                envelope = (note_samples - i) / fade_samples
            value = math.sin(2 * math.pi * frequency * (i / sample_rate))
            samples.append(int(value * envelope * LISTENING_CUE_VOLUME * 32767))

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(samples.tobytes())
    return buffer.getvalue()


def _ensure_listening_chime() -> Optional[str]:
    """
    Returns the path to the cached chime WAV, writing it once if needed.

    Written next to this file (LISTENING_CUE_CACHE_PATH) so it survives
    a restart - synthesis is trivial, but there is no reason to pay even
    that cost on every single mic-open. Returns None if the cache
    directory could not be created or written to, so the caller can
    fall back to a plain winsound.Beep rather than crash.
    """
    if os.path.isfile(LISTENING_CUE_CACHE_PATH):
        return LISTENING_CUE_CACHE_PATH
    try:
        os.makedirs(os.path.dirname(LISTENING_CUE_CACHE_PATH), exist_ok=True)
        with open(LISTENING_CUE_CACHE_PATH, "wb") as f:
            f.write(_synthesize_listening_chime())
        return LISTENING_CUE_CACHE_PATH
    except Exception as exc:  # noqa: BLE001 - caller falls back to Beep
        print(f"[Voice] Could not write the listening chime: {type(exc).__name__}: {exc}")
        return None
    return False


def _mp3_to_wav(mp3: bytes) -> Optional[bytes]:
    """
    Decodes Edge-TTS's MP3 to 16-bit PCM WAV, or returns None.

    Worth the trouble because WAV is the only format winsound can play,
    and winsound is the cheapest way to start a sound on Windows. PyAV
    is tried first: faster-whisper already pulls it in, so on a working
    Jarvis install it is there without adding a dependency.
    """
    for decoder in (_mp3_to_wav_pyav, _mp3_to_wav_ffmpeg):
        try:
            wav = decoder(mp3)
        except Exception as exc:  # noqa: BLE001 - try the next decoder
            print(f"[Voice] {decoder.__name__} failed: {type(exc).__name__}: {exc}")
            continue
        if wav:
            return wav
    return None


def _mp3_to_wav_pyav(mp3: bytes) -> Optional[bytes]:
    """MP3 -> WAV through PyAV, entirely in memory."""
    try:
        import av
        from av.audio.resampler import AudioResampler
    except ImportError:
        return None

    import wave

    resampler = AudioResampler(format="s16", layout="mono", rate=FILLER_WAV_RATE)
    chunks: list[bytes] = []
    with av.open(io.BytesIO(mp3), format="mp3") as container:
        for frame in container.decode(audio=0):
            for resampled in resampler.resample(frame):
                chunks.append(bytes(resampled.planes[0]))
    # Flush whatever the resampler is still holding.
    for resampled in resampler.resample(None):
        chunks.append(bytes(resampled.planes[0]))

    if not chunks:
        return None

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(FILLER_WAV_RATE)
        out.writeframes(b"".join(chunks))
    return buffer.getvalue()


def _mp3_to_wav_ffmpeg(mp3: bytes) -> Optional[bytes]:
    """MP3 -> WAV through an ffmpeg binary, if one is on PATH."""
    # Checked rather than caught: a missing ffmpeg is the normal case
    # here (nothing in requirements.txt installs it), and it must not
    # print a FileNotFoundError once per cached phrase at every start.
    if shutil.which("ffmpeg") is None:
        return None

    completed = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "quiet",
            "-f", "mp3", "-i", "pipe:0",
            "-ac", "1", "-ar", str(FILLER_WAV_RATE), "-f", "wav", "pipe:1",
        ],
        input=mp3,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if completed.returncode != 0 or not completed.stdout:
        return None
    return completed.stdout


class FillerCache:
    """
    Pre-synthesised acknowledgements, played straight off the disk.

    Built once at startup on a background thread, so the Edge-TTS round
    trip lands while the user is reading the boot log rather than while
    they are waiting to be answered. play() is non-blocking and returns
    "" when it has nothing cached, which is the caller's cue to fall
    back to ordinary speech.
    """

    def __init__(
        self,
        phrases: tuple[str, ...] = FILLER_PHRASES,
        cache_dir: str = FILLER_CACHE_DIR,
        voice: str = "",
    ) -> None:
        self._phrases = tuple(p for p in phrases if p and p.strip())
        self._cache_dir = cache_dir
        self._voice = voice or _env("EDGE_TTS_VOICE", EDGE_TTS_VOICE)
        self._paths: dict[str, str] = {}
        self._lock = threading.Lock()
        self._warming = threading.Event()
        self._last = ""

    @property
    def ready(self) -> bool:
        """True once at least one phrase is playable from disk."""
        with self._lock:
            return bool(self._paths)

    def _path_for(self, phrase: str, suffix: str) -> str:
        # The voice is part of the key: changing EDGE_TTS_VOICE must not
        # keep playing the old voice's cached audio.
        digest = hashlib.sha1(f"{self._voice}|{phrase}".encode("utf-8")).hexdigest()[:16]
        return os.path.join(self._cache_dir, f"{digest}{suffix}")

    def prewarm_async(self) -> None:
        """Builds the cache on a daemon thread. Safe to call twice."""
        if self._warming.is_set():
            return
        self._warming.set()
        threading.Thread(
            target=self._prewarm, name="jarvis-filler-cache", daemon=True
        ).start()

    def _prewarm(self) -> None:
        started = time.monotonic()
        try:
            os.makedirs(self._cache_dir, exist_ok=True)
        except OSError as exc:
            print(f"[Voice] Filler cache unavailable ({exc}); fillers will be spoken live.")
            return

        synthesised = 0
        for phrase in self._phrases:
            # An already-cached phrase costs nothing, which is why the
            # cache lives beside the source and not in the temp dir: it
            # has to survive a reboot.
            existing = next(
                (
                    candidate
                    for candidate in (self._path_for(phrase, s) for s in (".wav", ".mp3"))
                    if os.path.exists(candidate) and os.path.getsize(candidate) > 0
                ),
                None,
            )
            if existing is None:
                existing = self._synthesize(phrase)
                if existing is not None:
                    synthesised += 1
            if existing is not None:
                with self._lock:
                    self._paths[phrase] = existing

        self._prune()

        cached = len(self._paths)
        if cached:
            print(
                f"[Voice] Filler cache ready: {cached}/{len(self._phrases)} phrases "
                f"({synthesised} synthesised) in {time.monotonic() - started:.1f}s."
            )
        else:
            print("[Voice] Filler cache empty; acknowledgements will be spoken live.")

    def _prune(self) -> None:
        """
        Deletes cached audio no phrase claims any more.

        Editing FILLER_PHRASES or the voice changes the hashes, and
        without this the old files sit in the cache forever - a few
        hundred kilobytes of audio that will never be played again.
        """
        keep = set(self._paths.values())
        try:
            entries = os.listdir(self._cache_dir)
        except OSError:
            return

        for entry in entries:
            path = os.path.join(self._cache_dir, entry)
            if path in keep or not os.path.isfile(path):
                continue
            if not entry.endswith((".wav", ".mp3", ".part")):
                continue
            try:
                os.remove(path)
                print(f"[Voice] Removed stale filler audio {entry}.")
            except OSError:
                pass  # in use by a player, or read-only; harmless either way

    def _synthesize(self, phrase: str) -> Optional[str]:
        """Synthesises one phrase to disk. Returns its path, or None."""
        try:
            future = _LOOP.submit(_synthesize_edge(phrase, self._voice))
            mp3 = future.result(timeout=EDGE_TTS_TIMEOUT + 5.0)
        except Exception as exc:  # noqa: BLE001 - an offline start-up is fine
            print(f"[Voice] Could not cache filler {phrase!r}: {type(exc).__name__}: {exc}")
            return None
        if not mp3:
            return None

        wav = _mp3_to_wav(mp3)
        suffix, payload = (".wav", wav) if wav else (".mp3", mp3)
        path = self._path_for(phrase, suffix)
        try:
            # Written under a temp name and moved into place, so a crash
            # mid-write cannot leave a truncated file to be played later.
            temporary = path + ".part"
            with open(temporary, "wb") as handle:
                handle.write(payload)
            os.replace(temporary, path)
        except OSError as exc:
            print(f"[Voice] Could not write filler cache {path}: {exc}")
            return None
        return path

    def pick(self) -> str:
        """A phrase that is not the one used last turn."""
        choices = [p for p in self._phrases if p != self._last] or list(self._phrases)
        if not choices:
            return ""
        phrase = random.choice(choices)
        self._last = phrase
        return phrase

    def play(self, phrase: str = "") -> str:
        """
        Plays a cached filler and returns immediately.

        Returns the phrase actually played, or "" if nothing was cached
        for it - the caller then decides whether to speak it live.
        """
        phrase = phrase or self.pick()
        if not phrase:
            return ""
        with self._lock:
            path = self._paths.get(phrase, "")
        if not path:
            return ""
        if not play_file_async(path):
            return ""
        print(f"[Jarvis] {phrase}")
        return phrase


# ---------------------------------------------------------------------
# Loading a Vosk model takes a second or two, so share one instance
# across every VoiceEngine in the process.
# ---------------------------------------------------------------------

_MODEL_CACHE: dict[str, object] = {}


def _load_model(path: str):
    """Loads (and caches) a Vosk model. Returns None on failure."""
    if path in _MODEL_CACHE:
        return _MODEL_CACHE[path]
    model = Model(path)
    _MODEL_CACHE[path] = model
    return model


class VoiceEngine:
    """
    Hybrid speech out, offline speech in.

    Nothing here is allowed to raise during normal operation: a dead
    speaker, a missing model, an expired ElevenLabs key, or an absent
    microphone each downgrade one tier so the assistant loop keeps
    running.
    """

    def __init__(
        self,
        rate: int = TTS_RATE,
        voice_hint: Optional[str] = TTS_VOICE_HINT,
        enable_tts: bool = True,
        enable_stt: bool = True,
        auto_download: bool = AUTO_DOWNLOAD_MODEL,
        model_path: Optional[str] = None,
        premium_max_chars: Optional[int] = None,
        elevenlabs_voice: Optional[str] = None,
        edge_voice: Optional[str] = None,
        offline_only: Optional[bool] = None,
    ) -> None:
        self._rate = rate
        self._voice_hint = voice_hint
        self._enable_tts = enable_tts

        # -- hybrid TTS configuration ---------------------------------
        self._premium_max_chars = (
            premium_max_chars
            if premium_max_chars is not None
            else _int_env("TTS_PREMIUM_MAX_CHARS", TTS_PREMIUM_MAX_CHARS)
        )
        self._elevenlabs_voice = (
            elevenlabs_voice or _env("ELEVENLABS_VOICE", ELEVENLABS_VOICE_NAME)
        )
        self._edge_voice = edge_voice or _env("EDGE_TTS_VOICE", EDGE_TTS_VOICE)
        self._offline_only = (
            offline_only
            if offline_only is not None
            else _env_flag("JARVIS_TTS_OFFLINE_ONLY")
        )

        self._tts = None
        if enable_tts and _TTS_AVAILABLE:
            self._tts = self._init_tts()
        elif enable_tts:
            print(f"[Voice] pyttsx3 unavailable ({_TTS_IMPORT_ERROR}).")
            print("[Voice] Fix with: pip install pyttsx3")

        if enable_tts:
            self._report_tts_tiers()

        # Pre-synthesised acknowledgements. Not warmed here: the caller
        # decides when to pay for it (main.py does it at startup, off
        # the critical path), and a self-test that never speaks a filler
        # should not open a network connection for one.
        self._fillers = FillerCache(voice=self._edge_voice)

        self._model = None
        self._audio = None
        if enable_stt:
            self._init_stt(auto_download=auto_download, model_path=model_path)

    # -- setup ---------------------------------------------------------

    def prewarm_fillers(self) -> None:
        """
        Builds the cached filler audio, and the listening chime, on a
        background thread.

        Call once at startup. Without it play_filler() still works, it
        just pays the Edge-TTS round trip the cache exists to avoid -
        and play_listening_cue() still works too, it just pays a few
        milliseconds of synthesis on its first call instead of at
        startup.
        """
        # The chime needs no TTS tier at all - pure math, no network -
        # so it prewarms unconditionally, on its own thread so a slow
        # disk cannot delay startup.
        threading.Thread(
            target=_ensure_listening_chime, name="jarvis-chime-prewarm", daemon=True
        ).start()

        if self._offline_only or not self.neural_available:
            print("[Voice] Filler cache skipped (no Edge-TTS); fillers will be spoken live.")
            return
        self._fillers.prewarm_async()

    def play_filler(
        self,
        phrase: str = "",
        seconds: Optional[float] = None,
        text: str = "",
    ) -> str:
        """
        Acknowledges the user immediately and returns without blocking.

        `seconds` is how long they spoke for, and `text` is what they
        said where that is already known; either one being short enough
        suppresses the filler entirely (see FILLER_MIN_SECONDS). Silence
        is the right answer to "thank you" - the reply itself is a
        second away, and a filler in front of it just talks over it.

        Plays a cached file when there is one - which costs milliseconds,
        not a network round trip - and falls back to ordinary
        fire-and-forget speech when the cache is cold or unusable.
        Returns the phrase used, or "" if nothing was said.
        """
        if seconds is not None and seconds < FILLER_MIN_SECONDS:
            print(f"[Voice] {seconds:.1f}s of speech; short reply expected, no filler.")
            return ""
        words = len(text.split()) if text else 0
        if text and words < FILLER_MIN_WORDS:
            plural = "" if words == 1 else "s"
            print(f"[Voice] {words} word{plural} heard; short reply expected, no filler.")
            return ""

        phrase = phrase or self._fillers.pick()
        if not phrase:
            return ""

        if self._fillers.play(phrase):
            return phrase

        # Cold cache (startup still running, or offline). Speaking it
        # live is slower than the cache but still better than silence.
        self.speak_nowait(phrase)
        return phrase

    def play_listening_cue(self) -> None:
        """
        A short, soft two-note chime marking the instant the mic goes hot.

        Fired from session_manager right before each ACTIVE-mode
        listen() call, so the user hears an audible "go ahead" instead
        of guessing whether Jarvis is actually listening yet.

        Three tiers, each falling through to the next on any failure:

            1. the synthesised chime (_ensure_listening_chime), played
               through play_file_async - winsound.PlaySound with
               SND_ASYNC, the same non-blocking WAV path this module
               already uses for cached filler audio.
            2. a plain winsound.Beep, rougher but still a real tone.
            3. the terminal bell - the one cue every terminal can
               attempt, even muted ones.

        Everything here runs on its own short-lived daemon thread, not
        the caller's: winsound.Beep in particular blocks for its full
        duration, and the whole point of this cue is to mark the mic
        opening - it must never be the thing that delays it.
        """

        def _play() -> None:
            path = _ensure_listening_chime()
            if path is not None and play_file_async(path):
                return
            if winsound is not None:
                try:
                    winsound.Beep(LISTENING_CUE_FREQUENCY_HZ, LISTENING_CUE_DURATION_MS)
                    return
                except Exception as exc:  # noqa: BLE001 - fall through to the terminal bell
                    print(f"[Voice] winsound.Beep failed: {type(exc).__name__}: {exc}")
            print("\a", end="", flush=True)

        threading.Thread(target=_play, name="jarvis-listening-cue", daemon=True).start()

    def _report_tts_tiers(self) -> None:
        """
        Prints which speech tiers are live at startup, so a missing key
        or package is discovered now rather than mid-sentence.
        """
        if self._offline_only:
            print("[Voice] JARVIS_TTS_OFFLINE_ONLY set - cloud voices disabled.")

        if self.premium_available:
            print(
                f"[Voice] Tier 1 ElevenLabs: ready "
                f"(voice '{self._elevenlabs_voice}', under {self._premium_max_chars} chars)."
            )
        elif not self._offline_only:
            if not _HTTPX_AVAILABLE:
                print(f"[Voice] Tier 1 ElevenLabs: off (httpx missing: {_HTTPX_IMPORT_ERROR}).")
            elif not elevenlabs_api_key():
                print("[Voice] Tier 1 ElevenLabs: off (no ELEVENLABS_API_KEY in .env).")

        if self.neural_available:
            print(f"[Voice] Tier 2 Edge-TTS: ready (voice '{self._edge_voice}').")
        elif not self._offline_only:
            print(f"[Voice] Tier 2 Edge-TTS: off ({_EDGE_IMPORT_ERROR or 'disabled'}).")
            print("[Voice] Fix with: pip install edge-tts")

        print(
            "[Voice] Tier 3 pyttsx3 (offline): "
            + ("ready." if self._tts is not None else "off - replies will be printed only.")
        )

    def _init_tts(self):
        """Builds the pyttsx3 engine, applying rate/voice preferences."""
        try:
            engine = pyttsx3.init()
            engine.setProperty("rate", self._rate)

            if self._voice_hint:
                wanted = self._voice_hint.lower()
                for voice in engine.getProperty("voices"):
                    if wanted in voice.name.lower() or wanted in str(voice.id).lower():
                        engine.setProperty("voice", voice.id)
                        break
                else:
                    print(f"[Voice] No installed voice matched '{self._voice_hint}'. Using default.")
            return engine
        except Exception as exc:  # noqa: BLE001 - bad audio stack must not be fatal
            print(f"[Voice] Could not start the offline speech synthesiser: {exc}")
            return None

    def _init_stt(self, auto_download: bool, model_path: Optional[str]) -> None:
        """
        Resolves the Vosk model and prepares PyAudio.

        Every failure path here is non-fatal and ends with a printed
        explanation plus a fallback to typed input.
        """
        if not _VOSK_AVAILABLE:
            print(f"[Voice] Vosk unavailable ({_VOSK_IMPORT_ERROR}). Falling back to typed input.")
            print("[Voice] Fix with: pip install vosk")
            return

        if not _AUDIO_AVAILABLE:
            print(f"[Voice] PyAudio unavailable ({_AUDIO_IMPORT_ERROR}). Falling back to typed input.")
            print("[Voice] Fix with: pip install pyaudio")
            return

        # Locate the model, downloading it on first run if allowed.
        # quiet=True because this class owns the user-facing messaging
        # below - otherwise the install instructions print twice.
        resolved = model_path
        if resolved is None:
            try:
                import vosk_manager

                resolved = vosk_manager.ensure_model(auto_download=auto_download, quiet=True)
            except ImportError:
                print("[Voice] vosk_manager.py not found; cannot locate the speech model.")
                resolved = None

        if resolved is None:
            print("[Voice] No offline speech model available - falling back to typed input.")
            self._print_model_instructions()
            return

        try:
            self._model = _load_model(resolved)
        except Exception as exc:  # noqa: BLE001 - corrupt model must not be fatal
            print(f"[Voice] Failed to load the speech model at {resolved}: {exc}")
            print("[Voice] Try re-downloading it:  python vosk_manager.py --force")
            self._model = None
            return

        try:
            self._audio = pyaudio.PyAudio()
        except Exception as exc:  # noqa: BLE001 - no audio device
            print(f"[Voice] Could not open the audio system ({exc}). Falling back to typed input.")
            self._audio = None
            return

        print(f"[Voice] Offline speech recognition ready (Vosk, {SAMPLE_RATE} Hz). No audio leaves this machine.")

    @staticmethod
    def _print_model_instructions() -> None:
        """Tells the user exactly how to install the missing model."""
        print("-" * 68)
        print("  OFFLINE SPEECH MODEL NOT INSTALLED")
        print("-" * 68)
        try:
            import vosk_manager

            print(vosk_manager.manual_instructions())
        except ImportError:
            print("  Run:  python vosk_manager.py")
        print("-" * 68)

    # -- capabilities --------------------------------------------------

    @property
    def can_speak(self) -> bool:
        """True when at least one speech tier can produce audio."""
        return self.premium_available or self.neural_available or self._tts is not None

    @property
    def premium_available(self) -> bool:
        """True when the ElevenLabs tier is usable."""
        return (
            self._enable_tts
            and not self._offline_only
            and _HTTPX_AVAILABLE
            and elevenlabs_api_key() is not None
        )

    @property
    def neural_available(self) -> bool:
        """True when the Edge-TTS tier is usable."""
        return self._enable_tts and not self._offline_only and _EDGE_AVAILABLE

    @property
    def can_listen(self) -> bool:
        return self._model is not None and self._audio is not None

    # -- output: routing -----------------------------------------------

    def route_for(self, text: str) -> list[str]:
        """
        The ordered list of backends to try for `text`.

        Exposed rather than inlined so the routing decision can be
        asserted in a test without synthesising anything.
        """
        chain: list[str] = []
        if len(text) < self._premium_max_chars and self.premium_available:
            chain.append("elevenlabs")
        if self.neural_available:
            chain.append("edge")
        if self._tts is not None:
            chain.append("pyttsx3")
        return chain

    async def _try_backend(self, backend: str, text: str) -> bool:
        """Attempts one backend. Returns True only if audio actually played."""
        if backend == "elevenlabs":
            audio = await _synthesize_elevenlabs(text, self._elevenlabs_voice)
            return await _play_bytes(audio) if audio else False

        if backend == "edge":
            audio = await _synthesize_edge(text, self._edge_voice)
            return await _play_bytes(audio) if audio else False

        if backend == "pyttsx3":
            # pyttsx3 is a blocking, non-thread-safe COM object; keep it
            # off the event loop.
            return await asyncio.to_thread(self._speak_offline, text)

        return False

    def _speak_offline(self, text: str) -> bool:
        """Blocking pyttsx3 playback. Returns False if it could not speak."""
        if self._tts is None:
            return False
        try:
            self._tts.say(text)
            self._tts.runAndWait()
            return True
        except RuntimeError as exc:
            # pyttsx3 raises "run loop already started" if a previous
            # runAndWait() was interrupted. Rebuild the engine once.
            print(f"[Voice] Offline speech engine hiccup ({exc}); reinitialising.")
            self._tts = self._init_tts()
            if self._tts is None:
                return False
            try:
                self._tts.say(text)
                self._tts.runAndWait()
                return True
            except Exception as exc2:  # noqa: BLE001
                print(f"[Voice] Offline speech failed again, staying silent: {exc2}")
                return False
        except Exception as exc:  # noqa: BLE001 - never let TTS kill the loop
            print(f"[Voice] Offline speech failed: {exc}")
            return False

    # -- output: entry points ------------------------------------------

    async def speak_async(self, text: str, also_print: bool = True) -> Optional[str]:
        """
        Says `text` out loud, routing by length, and returns the name of
        the backend that actually spoke (or None if none could).

        Serialised against other utterances so two replies never overlap.
        """
        if not text or not text.strip():
            return None
        text = text.strip()

        if also_print:
            print(f"[Jarvis] {text}")

        chain = self.route_for(text)
        if not chain:
            return None

        async with _speak_lock():
            for index, backend in enumerate(chain):
                reason = (
                    f"{len(text)} chars < {self._premium_max_chars}"
                    if backend == "elevenlabs"
                    else f"{len(text)} chars"
                )
                label = "Route" if index == 0 else "Fallback"
                print(f"[Voice] {label}: {backend} ({reason})")

                if await self._try_backend(backend, text):
                    return backend

        print("[Voice] Every speech tier failed; the reply was printed only.")
        return None

    def speak(self, text: str, also_print: bool = True) -> Optional[str]:
        """
        Blocking wrapper around speak_async().

        Synthesis and playback still happen off the calling thread; this
        just waits for them, which is what the assistant loop wants when
        it is about to start listening for a reply.
        """
        try:
            future = _LOOP.submit(self.speak_async(text, also_print=also_print))
            return future.result()
        except Exception as exc:  # noqa: BLE001 - speech must never kill the loop
            print(f"[Voice] Speech dispatch failed: {type(exc).__name__}: {exc}")
            return None

    def speak_nowait(self, text: str, also_print: bool = True) -> None:
        """
        Fire-and-forget speech: returns immediately and plays behind you.

        Use for status announcements the assistant does not need to
        finish before doing something else. Do NOT use immediately
        before listen(), or the microphone will hear the speakers.
        """
        try:
            _LOOP.submit(self.speak_async(text, also_print=also_print))
        except Exception as exc:  # noqa: BLE001
            print(f"[Voice] Speech dispatch failed: {type(exc).__name__}: {exc}")

    # -- input ---------------------------------------------------------

    @staticmethod
    def _typed_fallback() -> str:
        """Keyboard input, used whenever the microphone path is unavailable."""
        try:
            return input("[You] (type your command) ").strip()
        except (EOFError, KeyboardInterrupt):
            return ""

    def open_input_stream(self, frames_per_buffer: int = CHUNK_SIZE):
        """
        Opens the microphone as 16 kHz mono 16-bit PCM, or returns None.

        Public because whisper_manager.py opens its own capture stream
        for ACTIVE mode and must do it through this PyAudio instance -
        one process should own one PyAudio, and the format is the same
        one Vosk, openWakeWord, and Whisper all expect.

        `frames_per_buffer` is a caller's choice: Vosk reads 0.25 s
        blocks, while Whisper's endpointer wants 30 ms frames so it can
        detect the end of a sentence promptly.
        """
        if self._audio is None:
            return None
        try:
            return self._audio.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=SAMPLE_RATE,
                input=True,
                frames_per_buffer=frames_per_buffer,
            )
        except Exception as exc:  # noqa: BLE001 - device busy or absent
            print(f"[Voice] Microphone unavailable ({exc}). Falling back to typed input.")
            return None

    # Kept so existing calls (and the tests) still resolve.
    _open_input_stream = open_input_stream

    def stream_utterances(
        self,
        *,
        silence_timeout: Optional[float] = None,
        phrase_limit: float = LISTEN_PHRASE_LIMIT,
        cancel: Optional[threading.Event] = None,
        label: str = "Listening",
        detailed: bool = False,
    ) -> Iterator[Union[str, Utterance]]:
        """
        Yields spoken utterances one at a time, holding a single
        microphone stream open for the life of the generator.

        This is the primitive both listening modes are built on:

            standby - silence_timeout=None, runs until the caller breaks
                      out of the loop (wake phrase heard)
            active  - silence_timeout=15, ends the session on silence

        `cancel` is a threading.Event checked between audio chunks, so an
        external timer can end a listen promptly without interrupting a
        blocking read mid-frame - the read is only 0.25 s long, which
        bounds the reaction time. Nothing else is ever allowed to touch
        the stream from another thread.

        `silence_timeout` counts seconds since the last sign of speech
        (a partial result or a completed utterance), not since the
        generator started, so a talkative user never times out.

        IMPORTANT for callers: the stream stays open while the consumer
        is running its loop body. Anything slow in that body - a spoken
        reply, an API call - must break out of the loop first, so the
        generator closes and stale audio (including Jarvis's own voice)
        is not queued up behind it.

        With `detailed=True` each item is an Utterance instead of a
        string, carrying the raw PCM the recogniser heard and Vosk's
        per-word timings. That is what lets STANDBY hand the audio
        *after* a wake phrase to Whisper instead of throwing it away -
        see session_manager and whisper_manager.
        """
        if not self.can_listen:
            # Typed fallback: one line per iteration, blank line ends it.
            while True:
                typed = self._typed_fallback()
                if not typed:
                    return
                yield typed

        try:
            recognizer = KaldiRecognizer(self._model, SAMPLE_RATE)
            if detailed:
                # Per-word start/end times, needed to find where the
                # wake phrase ends inside the captured audio.
                recognizer.SetWords(True)
        except Exception as exc:  # noqa: BLE001
            print(f"[Voice] Could not start the recogniser: {exc}")
            return

        stream = self._open_input_stream()
        if stream is None:
            while True:
                typed = self._typed_fallback()
                if not typed:
                    return
                yield typed

        print(f"[Voice] {label}... (speak now)")
        last_voice = time.monotonic()      # last evidence of speech
        phrase_started: Optional[float] = None
        last_partial = ""

        # Raw PCM of the utterance being captured, plus how many samples
        # the recogniser had already consumed when it started. Vosk word
        # timings are measured from the start of the *stream*, so that
        # offset is what converts a word time into an index into `chunks`.
        chunks: list[bytes] = []
        samples_before = 0
        samples_fed = 0

        def _package(result: dict, text: str, buffered: list, start_samples: int):
            """Plain text, or a full Utterance when detailed=True."""
            if not detailed:
                return text
            return Utterance(
                text=text,
                audio=b"".join(buffered),
                words=result.get("result", []) or [],
                stream_start=start_samples / SAMPLE_RATE,
            )

        try:
            while True:
                if cancel is not None and cancel.is_set():
                    # Only honoured between phrases: cutting someone off
                    # mid-sentence loses the command they just spoke.
                    if phrase_started is None:
                        return

                now = time.monotonic()

                if (
                    silence_timeout is not None
                    and phrase_started is None
                    and now - last_voice > silence_timeout
                ):
                    return

                # Someone is rambling - cut them off and use what we have.
                if phrase_started is not None and now - phrase_started > phrase_limit:
                    print("\r[Voice] Reached the phrase limit. ")
                    text = json.loads(recognizer.FinalResult()).get("text", "").strip()
                    phrase_started = None
                    last_partial = ""
                    last_voice = time.monotonic()
                    if text:
                        print(f"[You] {text}")
                        yield text
                    else:
                        print("[Voice] Could not make out what was said.")
                    continue

                try:
                    data = stream.read(CHUNK_SIZE, exception_on_overflow=False)
                except Exception as exc:  # noqa: BLE001 - read error mid-capture
                    print(f"[Voice] Microphone read failed: {exc}")
                    return

                if recognizer.AcceptWaveform(data):
                    # End of an utterance.
                    text = json.loads(recognizer.Result()).get("text", "").strip()
                    phrase_started = None
                    last_partial = ""
                    if text:
                        print(f"\r[You] {text}" + " " * 20)
                        last_voice = time.monotonic()
                        yield text
                        # The consumer may have spoken while suspended;
                        # restart the silence clock from now, not from
                        # before its reply.
                        last_voice = time.monotonic()
                    # Empty result = a stretch of silence; keep waiting.
                else:
                    partial = json.loads(recognizer.PartialResult()).get("partial", "").strip()
                    if partial and partial != last_partial:
                        last_partial = partial
                        last_voice = time.monotonic()
                        if phrase_started is None:
                            phrase_started = last_voice
                        # Live feedback that recognition is working.
                        print(f"\r[Voice] ...{partial}", end="", flush=True)
        finally:
            # Runs on return, on GeneratorExit (consumer broke out of the
            # loop), and on error - the mic is never left open.
            try:
                stream.stop_stream()
                stream.close()
            except Exception:  # noqa: BLE001 - teardown must stay quiet
                pass

    def listen(
        self,
        prompt: Optional[str] = None,
        timeout: Optional[float] = None,
        phrase_limit: Optional[float] = None,
        cancel: Optional[threading.Event] = None,
    ) -> str:
        """
        Captures one spoken utterance and returns it as text, recognised
        entirely on this machine.

        Returns "" if nothing intelligible was heard within `timeout`
        seconds (default LISTEN_TIMEOUT). Falls back to a typed prompt
        whenever the microphone path is unavailable, so the caller can
        always rely on getting a string back.
        """
        if prompt:
            # Blocking on purpose: the prompt must finish playing before
            # the microphone opens, or Vosk transcribes our own speakers.
            self.speak(prompt)

        if not self.can_listen:
            return self._typed_fallback()

        for text in self.stream_utterances(
            silence_timeout=timeout if timeout is not None else LISTEN_TIMEOUT,
            phrase_limit=phrase_limit if phrase_limit is not None else LISTEN_PHRASE_LIMIT,
            cancel=cancel,
        ):
            # Breaking out closes the generator, which closes the stream.
            return text

        print("[Voice] Heard nothing.")
        return ""

    def confirm(self, question: str) -> bool:
        """
        Spoken yes/no gate, judged by a fast LLM call rather than a word
        list, so context, code-switching, and mishearings all read
        correctly. Anything that is not a clear affirmative is treated
        as "no" - an ambiguous answer, or an unreachable judge, must
        never authorise an action.
        """
        answer = self.listen(prompt=question)
        if not answer or not answer.strip():
            return False

        # Imported here, not at module scope: voice_engine is the lower
        # layer, and the brain pulls in the whole tool stack behind it.
        try:
            import llm_brain
        except Exception as exc:  # noqa: BLE001 - no brain means no verdict
            print(f"[Voice] Confirmation judge unavailable: {type(exc).__name__}: {exc}")
            return False

        verdict = llm_brain.judge_confirmation(answer)
        if verdict is None:
            print("[Voice] No confirmation verdict; treating as a refusal.")
            return False

        print(f"[Voice] Confirmation {answer!r} judged as {verdict}.")
        return verdict

    def shutdown(self) -> None:
        """Releases the TTS engine, audio system, and loop thread."""
        if self._tts is not None:
            try:
                self._tts.stop()
            except Exception:  # noqa: BLE001 - teardown must stay quiet
                pass
            self._tts = None

        if self._audio is not None:
            try:
                self._audio.terminate()
            except Exception:  # noqa: BLE001 - teardown must stay quiet
                pass
            self._audio = None

        _LOOP.shutdown()


if __name__ == "__main__":
    engine = VoiceEngine(enable_stt=False)
    print(f"\n[Voice] can_speak={engine.can_speak}  can_listen={engine.can_listen}\n")

    short = "Good evening. All systems are online."
    long_text = (
        "Here is the longer explanation you asked for. Routing by length means "
        "the premium voice is reserved for the short conversational replies you "
        "actually listen to closely, while anything at or over the two hundred "
        "character threshold - like this passage - is handed to the free neural "
        "voice instead, which keeps the character quota where it is worth paying for."
    )

    print(f"--- short ({len(short)} chars) -> {engine.route_for(short)}")
    engine.speak(short)
    print(f"\n--- long ({len(long_text)} chars) -> {engine.route_for(long_text)}")
    engine.speak(long_text)

    engine.shutdown()
