"""
Speech-to-text for the web hub's Talk button (api_server.py's POST /voice).

The browser records with MediaRecorder and hands over whatever container that
browser makes - WebM/Opus in Chrome and Firefox, MP4/AAC in Safari. This module
turns those bytes into text:

    OPENAI_API_KEY set     -> OpenAI's Whisper API (POST {OPENAI_BASE_URL}/audio/transcriptions)
    OPENAI_API_KEY not set -> a MOCK: the receipt is logged and a clearly-labelled
                              stand-in sentence comes back, so the whole path
                              (microphone -> web app -> here -> brain -> reply)
                              can be checked before any key exists

Why a mock is labelled and not a canned sentence: its text is routed into the
brain and saved in the person's chat history like anything they said, so it has
to say what it is. `Transcription.engine` is "openai" or "mock" for the caller.

Privacy: this is the one place in the project where recorded speech leaves the
machine, and only when OPENAI_API_KEY is set. The voice assistant (main.py,
wake word -> Vosk/faster-whisper) is untouched and stays fully on-device.

.env (all optional):
    OPENAI_API_KEY            enables the Whisper API
    OPENAI_BASE_URL           default https://api.openai.com/v1 (a proxy, or a
                              local stand-in for testing)
    JARVIS_OPENAI_STT_MODEL   default whisper-1
    JARVIS_WHISPER_LANGUAGE   blank = auto-detect (default), or th / en to pin -
                              the same setting whisper_manager.py reads
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

import llm_brain

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "whisper-1"
REQUEST_TIMEOUT_S = 60.0

ENGINE_OPENAI = "openai"
ENGINE_MOCK = "mock"

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


def is_configured() -> bool:
    """True when a real engine will run (False means the mock answers)."""
    return bool(_setting("OPENAI_API_KEY"))


def transcribe(audio: bytes, content_type: str | None) -> Transcription:
    """
    The words in `audio`, or SttError. Text may be empty: nothing was said.
    Blocking (network) - call it from a worker thread, not the event loop.
    """
    if _setting("OPENAI_API_KEY"):
        return _transcribe_openai(audio, content_type)
    return _transcribe_mock(audio, content_type)


def _transcribe_mock(audio: bytes, content_type: str | None) -> Transcription:
    kind = bare_content_type(content_type) or "unknown-type"
    print(
        f"[STT] MOCK: received {len(audio):,} bytes of {kind} audio. "
        "OPENAI_API_KEY is not set, so nothing was transcribed - returning a stand-in sentence."
    )
    return Transcription(
        text=(
            f"This is a mock transcription. The voice pipeline received {len(audio):,} bytes of "
            f"{kind} audio, but no speech-to-text key is configured, so I could not hear what was said."
        ),
        engine=ENGINE_MOCK,
    )


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
