"""
Speech-to-text for the web hub's Talk button (api_server.py's POST /voice).

The browser records with MediaRecorder and hands over whatever container that
browser makes - WebM/Opus in Chrome and Firefox, MP4/AAC in Safari. This module
turns those bytes into text:

    OPENAI_API_KEY set     -> OpenAI's Whisper API (POST {OPENAI_BASE_URL}/audio/transcriptions)
    OPENAI_API_KEY not set -> faster-whisper ON THIS MACHINE (the same model the
                              voice assistant uses, whisper_manager.py)

Nothing else answers in their place: with no key and no usable faster-whisper,
transcription FAILS with a sentence that says how to fix it, rather than inventing
words that would be routed into the brain and saved as if the person had said them.

Privacy: with no key, recorded speech never leaves the machine. With a key it goes
to OpenAI - the one place in the project where that happens, and only for this
endpoint. The voice assistant (main.py, wake word -> Vosk/faster-whisper) is untouched.

On-device settings are whisper_manager's, so the web hub and the voice assistant
always agree (see its docstring for the measured CPU timings - "small" decodes a
3 s clip in about 6 s on a CPU-only machine; JARVIS_WHISPER_MODEL=base is ~3x faster,
and a CUDA GPU makes either near-instant):
    JARVIS_WHISPER_MODEL, _DEVICE, _COMPUTE, _LANGUAGE, _BEAM, _PROMPT
The model loads in the background when the server starts (warm_up), so the first
Talk press does not wait for it.

.env (all optional):
    OPENAI_API_KEY            enables the Whisper API
    OPENAI_BASE_URL           default https://api.openai.com/v1 (a proxy, or a
                              local stand-in for testing)
    JARVIS_OPENAI_STT_MODEL   default whisper-1
    JARVIS_WHISPER_LANGUAGE   blank = auto-detect (default), or th / en to pin;
                              applies to both engines
"""

from __future__ import annotations

import io
import threading
from dataclasses import dataclass

import httpx

import llm_brain

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "whisper-1"
REQUEST_TIMEOUT_S = 60.0
# How long a request waits for the on-device model to finish loading (first press after a cold start).
LOCAL_LOAD_TIMEOUT_S = 90.0

ENGINE_OPENAI = "openai"
ENGINE_LOCAL = "local"

# OpenAI decides how to decode an upload from its FILE NAME, not its bytes, so
# the browser's content type has to become an extension it knows.
_EXTENSIONS = {
    "audio/webm": ".webm",
    "video/webm": ".webm",  # Chrome labels an audio-only recording either way
    "audio/ogg": ".ogg",
    "audio/mp4": ".mp4",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/flac": ".flac",
}


class SttError(Exception):
    """Transcription failed. The message is safe to show the person as it stands."""


@dataclass(frozen=True)
class Transcription:
    text: str
    engine: str


def _setting(name: str, default: str = "") -> str:
    # llm_brain.load_api_key reads the environment and, once per process, the
    # project's .env - the same lookup every other key here goes through.
    return llm_brain.load_api_key(name) or default


def bare_content_type(content_type: str | None) -> str:
    """'audio/webm;codecs=opus' -> 'audio/webm'; anything unusable -> ''."""
    return (content_type or "").split(";", 1)[0].strip().lower()


def file_name_for(content_type: str | None) -> str:
    return "voice" + _EXTENSIONS.get(bare_content_type(content_type), ".webm")


def engine() -> str:
    """Which engine a recording goes to right now."""
    return ENGINE_OPENAI if _setting("OPENAI_API_KEY") else ENGINE_LOCAL


# ---------------------------------------------------------------------
# On-device engine (faster-whisper, through whisper_manager)
# ---------------------------------------------------------------------

_local = None
_local_guard = threading.Lock()  # creation of the one transcriber
_decode_lock = threading.Lock()  # one decode at a time: the model is one set of weights on one machine


def _transcriber():
    """
    The process's one on-device transcriber, built on first use. whisper_manager is
    imported here, not at the top: it pulls in faster-whisper, ctranslate2 and PyAV,
    which would make importing api_server (and /health) slow for nothing.
    """
    global _local
    with _local_guard:
        if _local is None:
            import whisper_manager

            _local = whisper_manager.WhisperTranscriber(voice=None)
        return _local


def _local_available() -> bool:
    import whisper_manager

    return whisper_manager.is_available()


def warm_up() -> None:
    """
    Starts loading the on-device model in the background when it is the engine in
    use, so the first Talk press does not pay for it (a few seconds cold). Never
    blocks and never raises: a model that cannot load is reported by the request
    that needed it.
    """
    if engine() != ENGINE_LOCAL:
        return
    try:
        if not _local_available():
            import whisper_manager

            print(f"[STT] faster-whisper is not installed ({whisper_manager.import_error()}); voice needs it or OPENAI_API_KEY.")
            return
        _transcriber().preload_async()
        print("[STT] No OPENAI_API_KEY: speech-to-text runs on this machine (faster-whisper); loading it in the background.")
    except Exception as exc:  # noqa: BLE001 - warming up is a courtesy
        print(f"[STT] Could not start the on-device model yet ({type(exc).__name__}: {exc}).")


def is_ready() -> bool:
    """True when a recording can be transcribed without waiting for a model to load."""
    if engine() == ENGINE_OPENAI:
        return True
    return _local is not None and _local.available


def transcribe(audio: bytes, content_type: str | None) -> Transcription:
    """
    The words in `audio`, or SttError. Text may be empty: nothing was said.
    Blocking (network, or CPU) - call it from a worker thread, not the event loop.
    """
    if engine() == ENGINE_OPENAI:
        return _transcribe_openai(audio, content_type)
    return _transcribe_local(audio, content_type)


def _transcribe_local(audio: bytes, content_type: str | None) -> Transcription:
    kind = bare_content_type(content_type) or "unknown-type"
    if not _local_available():
        print("[STT] No OPENAI_API_KEY and faster-whisper is not installed: cannot transcribe.")
        raise SttError(
            "Speech-to-text isn't set up: add OPENAI_API_KEY to .env, or install faster-whisper "
            "(pip install -r requirements.txt)."
        )

    transcriber = _transcriber()
    print(f"[STT] Local {transcriber.description}: {len(audio):,} bytes of {kind} audio.")
    if not transcriber.ensure_loaded(timeout=LOCAL_LOAD_TIMEOUT_S):
        if transcriber.load_error:
            raise SttError("The on-device speech model could not be loaded - see the server console.")
        raise SttError("The on-device speech model is still loading - try again in a moment.")

    with _decode_lock:
        try:
            text = transcriber.transcribe_recording(io.BytesIO(audio))
        except Exception as exc:  # noqa: BLE001 - PyAV and ctranslate2 raise their own types
            print(f"[STT] faster-whisper failed: {type(exc).__name__}: {exc}")
            raise SttError("Speech-to-text could not read that recording.") from exc
    return Transcription(text=text, engine=ENGINE_LOCAL)


# ---------------------------------------------------------------------
# OpenAI engine
# ---------------------------------------------------------------------


def _transcribe_openai(audio: bytes, content_type: str | None) -> Transcription:
    key = _setting("OPENAI_API_KEY")
    base = _setting("OPENAI_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    model = _setting("JARVIS_OPENAI_STT_MODEL", DEFAULT_MODEL)
    language = _setting("JARVIS_WHISPER_LANGUAGE")

    form = {"model": model}
    if language:
        form["language"] = language

    print(f"[STT] OpenAI {model}: sending {len(audio):,} bytes of {bare_content_type(content_type) or 'unknown-type'} audio.")
    try:
        response = httpx.post(
            f"{base}/audio/transcriptions",
            headers={"Authorization": f"Bearer {key}"},
            data=form,
            files={"file": (file_name_for(content_type), audio, bare_content_type(content_type) or "application/octet-stream")},
            timeout=REQUEST_TIMEOUT_S,
        )
    except httpx.TimeoutException as exc:
        print(f"[STT] OpenAI timed out: {exc}")
        raise SttError("Speech-to-text took too long to answer.") from exc
    except httpx.HTTPError as exc:
        print(f"[STT] OpenAI unreachable: {type(exc).__name__}: {exc}")
        raise SttError("Speech-to-text is unreachable right now.") from exc

    if response.status_code != 200:
        if response.status_code in (401, 403):
            # That body quotes the key it rejected (masked, but still): the status says all there is to say.
            print(f"[STT] OpenAI answered {response.status_code}: check OPENAI_API_KEY in .env.")
            raise SttError("Speech-to-text rejected the configured OpenAI key.")
        # The body is for the console only; the person gets a sentence below.
        print(f"[STT] OpenAI answered {response.status_code}: {response.text[:300]}")
        if response.status_code == 429:
            raise SttError("Speech-to-text is rate-limited or out of quota.")
        if response.status_code in (400, 415):
            raise SttError("Speech-to-text could not read that recording.")
        raise SttError(f"Speech-to-text failed ({response.status_code}).")

    try:
        text = str(response.json().get("text") or "").strip()
    except (ValueError, AttributeError) as exc:
        print(f"[STT] OpenAI sent a reply that is not the expected JSON: {response.text[:300]}")
        raise SttError("Speech-to-text sent a reply I couldn't read.") from exc

    print(f"[STT] OpenAI transcribed {len(text)} characters.")
    return Transcription(text=text, engine=ENGINE_OPENAI)
