"""
Module 9: ACTIVE-mode transcription (faster-whisper, Thai + English)

The second half of a hybrid speech-to-text stack:

    STANDBY   Vosk        tiny, always-on, English keyword spotting.
                          Its whole job is noticing "hey jarvis". It
                          runs for hours, so it has to be cheap.
    ACTIVE    Whisper     loaded once, used only inside a session.
                          Multilingual, punctuated, and comfortable
                          with Thai-English code-switching - which is
                          exactly what a command like
                          "log ค่ากาแฟ 120 บาท" needs.

Vosk cannot do the second job: its model here is English-only, so Thai
comes back as either nothing or as English words that sound vaguely
similar. Whisper cannot do the first: even the base model is far too
heavy to run continuously against an open microphone. Hence both.

-------------------------------------------------------------------
THE HANDOFF (why this module takes raw audio in)
-------------------------------------------------------------------
People do not pause after a wake word. "Jarvis, log my coffee" arrives
as one continuous breath, and by the time Vosk has decided the phrase
started with a wake word, the rest of the sentence has already been
spoken - and, in the naive design, thrown away.

So STANDBY keeps the PCM alongside the transcript (voice_engine's
Utterance), slices it at the end of the wake phrase using Vosk's word
timings, and passes the remainder here as `priming_audio`. Whisper
transcribes that audio itself rather than trusting Vosk's English
guess at it, which is what makes a Thai spillover command work at all.

If the priming audio holds a complete phrase, no further listening is
needed: the user has already finished talking, so it is transcribed
immediately and the microphone is never reopened.

-------------------------------------------------------------------
ENDPOINTING
-------------------------------------------------------------------
Whisper transcribes a finished recording; it does not stream. Something
has to decide when the user stopped talking, and that is the energy
gate in `_record`: 30 ms frames, an RMS threshold calibrated against
the room's own noise floor at the start of each listen, speech declared
after a few voiced frames and ended after END_SILENCE seconds of quiet.

Deliberately simple, and deliberately not a neural VAD: this runs on
the main thread between microphone reads, and the cost of a wrong
decision is small (a slightly clipped sentence, or one extra second of
patience). Whisper's own VAD filter then trims the recording before
transcription, so leading and trailing silence never reaches the model.

-------------------------------------------------------------------
CONFIGURATION (.env)
-------------------------------------------------------------------
    JARVIS_WHISPER_MODEL     tiny | base | small (default) | medium | large-v3
    JARVIS_WHISPER_DEVICE    auto (default, resolves to cuda when a GPU
                             is visible to ctranslate2) | cpu | cuda
    JARVIS_WHISPER_COMPUTE   auto (default) | int8 | int8_float16 | float16 | float32
    JARVIS_WHISPER_LANGUAGE  blank = auto-detect (default), or th / en to pin
    JARVIS_WHISPER_PROMPT    override the code-switching hint below
    JARVIS_WHISPER_BEAM      beam size, default 5 (1 is ~10% faster)
    JARVIS_VAD_THRESHOLD     blank = auto-calibrate, or an RMS integer

-------------------------------------------------------------------
PERFORMANCE, MEASURED ON THIS MACHINE (CPU, int8)
-------------------------------------------------------------------
Decoding a ~3 s command, English / Thai:

    small   6.3 s / 6.5 s    Thai transcribed exactly
    base    2.0 s / 2.2 s    Thai noticeably degraded

"small" is the default because a wrong amount logged to a database is
worse than a slow one, but the trade is real: if the pause after each
command is intolerable, set JARVIS_WHISPER_MODEL=base (already
downloaded alongside small), or run on a GPU with
JARVIS_WHISPER_DEVICE=cuda, where both are far faster.

Do NOT drop the initial prompt to save tokens. Without it Thai output
degrades badly AND the decoder occasionally falls into a repetition
loop that took 30-70 s to terminate in testing - a hang, from the
user's point of view. The prompt is a correctness feature here, not a
nicety.

Model weights are downloaded once from Hugging Face into .models/whisper
next to this file - the same place vosk_manager.py keeps its model, and
for the same reason: a `pip install --force-reinstall` must not wipe a
gigabyte of weights.

Run standalone:

    python whisper_manager.py            # download + self-test the model
    python whisper_manager.py --status   # report configuration and cache
    python whisper_manager.py --listen   # transcribe one spoken phrase
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Callable, Optional

import numpy as np

try:  # Windows only; everywhere else the cue is simply skipped.
    import winsound
except ImportError:  # pragma: no cover - non-Windows
    winsound = None  # type: ignore[assignment]

try:
    from faster_whisper import WhisperModel

    _WHISPER_AVAILABLE = True
    _WHISPER_IMPORT_ERROR = ""
except Exception as exc:  # noqa: BLE001 - optional; Vosk still handles English
    WhisperModel = None  # type: ignore[assignment]
    _WHISPER_AVAILABLE = False
    _WHISPER_IMPORT_ERROR = str(exc)


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

# Weights live beside the project, not in site-packages.
MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".models", "whisper")

# "small" is the sweet spot for bilingual command recognition: roughly
# 460 MB, a few hundred milliseconds per short utterance on CPU with
# int8, and markedly better at Thai than "base". Drop to "base" on a
# slow machine; "tiny" is not worth using for Thai.
DEFAULT_MODEL_SIZE = "small"

# Audio format. Fixed by Whisper (16 kHz mono) and matched by every
# other audio path in this project.
SAMPLE_RATE = 16000

# Endpointer geometry.
FRAME_MS = 30                      # granularity of the energy gate
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
START_FRAMES = 3                   # ~90 ms of voice to call it speech
END_SILENCE = 0.8                  # quiet after speech that ends a phrase
MAX_PHRASE = 20.0                  # hard cap on one utterance
MIN_SPEECH = 0.25                  # shorter than this is a cough, not a command

# Fallback RMS gate, used when calibration is impossible. Auto
# calibration (noise floor x NOISE_MULTIPLIER) normally wins.
DEFAULT_RMS_THRESHOLD = 500.0
NOISE_MULTIPLIER = 3.0
CALIBRATION_FRAMES = 10            # ~300 ms of room tone

# Whisper is prompted rather than pinned to a language. Pinning to "en"
# would make Thai come back as English nonsense; pinning to "th" does
# the reverse. The prompt biases the decoder towards the vocabulary this
# assistant actually hears - a bilingual speaker mixing Thai with
# English product and category names - without constraining it.
DEFAULT_PROMPT = (
    "คำสั่งเสียงถึงผู้ช่วยส่วนตัวชื่อ Jarvis "
    "ผู้ใช้พูดไทยปนอังกฤษ เช่น log ค่ากาแฟ 120 บาท category food, "
    "เปิด Subtrack, run workflow, check database. "
    "Bilingual Thai-English voice commands for a personal assistant."
)


def safe_print(message: str) -> None:
    """
    print() that cannot crash on a Thai transcript.

    Windows consoles still default to a legacy code page (cp874/cp1252),
    where printing Thai raises UnicodeEncodeError. Losing a command to a
    console encoding would be an absurd way for a voice assistant to
    fail, so unprintable characters are escaped instead. main.py also
    switches stdout to UTF-8 at startup, which makes this the fallback
    rather than the norm.
    """
    try:
        print(message)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", "") or "ascii"
        print(message.encode(encoding, errors="backslashreplace").decode(encoding))


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def model_size() -> str:
    return _env("JARVIS_WHISPER_MODEL") or DEFAULT_MODEL_SIZE


def cuda_available() -> bool:
    """
    True if ctranslate2 can actually see a usable CUDA device.

    Asked directly rather than left to faster-whisper's "auto", because
    "auto" silently settles for the CPU and the difference is the whole
    six seconds this decision is about: the same command decodes in well
    under a second on a GPU. ctranslate2 is already installed (it is
    what faster-whisper runs on), so this costs no new dependency.
    """
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception as exc:  # noqa: BLE001 - a CPU-only build says so by raising
        print(f"[Whisper] No CUDA device ({type(exc).__name__}: {exc}); using the CPU.")
        return False


def _device() -> str:
    choice = (_env("JARVIS_WHISPER_DEVICE") or "auto").lower()
    if choice in ("cpu", "cuda"):
        return choice
    # Prefer the GPU whenever there is one, and resolve it here rather
    # than passing "auto" through, so self._device names the device that
    # was actually chosen and _compute_type can pick to match it.
    return "cuda" if cuda_available() else "cpu"


def _compute_type(device: str) -> str:
    choice = (_env("JARVIS_WHISPER_COMPUTE") or "auto").lower()
    if choice != "auto":
        return choice
    # float16 is the point of having a GPU. int8 is the right default on
    # CPU: ~4x faster than float32, with no accuracy loss that matters
    # for short commands.
    return "float16" if device == "cuda" else "int8"


def _language() -> Optional[str]:
    """None = let Whisper detect it, which is what code-switching needs."""
    return _env("JARVIS_WHISPER_LANGUAGE") or None


def _initial_prompt() -> str:
    return _env("JARVIS_WHISPER_PROMPT") or DEFAULT_PROMPT


def _beam_size() -> int:
    """
    Beam width. 5 is Whisper's default and the best Thai accuracy seen
    in testing; 1 (greedy) is roughly 10% faster - not enough to be
    worth the accuracy on short commands, but exposed for slow machines.
    """
    raw = _env("JARVIS_WHISPER_BEAM")
    if not raw:
        return 5
    try:
        return max(1, int(raw))
    except ValueError:
        print(f"[Whisper] Ignoring invalid JARVIS_WHISPER_BEAM={raw!r}.")
        return 5


def _rms_threshold_override() -> Optional[float]:
    raw = _env("JARVIS_VAD_THRESHOLD")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        print(f"[Whisper] Ignoring invalid JARVIS_VAD_THRESHOLD={raw!r}.")
        return None


def is_available() -> bool:
    """True if the faster-whisper package imported."""
    return _WHISPER_AVAILABLE


def import_error() -> str:
    return _WHISPER_IMPORT_ERROR


# ---------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------


def capture_beep() -> None:
    """
    Short cue played the instant the microphone closes.

    Transcription plus the LLM reply is 7-10 seconds of total silence,
    which reads as "it did not hear me" and gets the sentence repeated
    over the top of the answer. The beep says "captured, working on it".
    Non-blocking and never fatal: a missing sound device must not cost
    us the turn.
    """
    if winsound is None:
        return
    try:
        winsound.MessageBeep(winsound.MB_OK)
    except Exception:  # noqa: BLE001 - a UX nicety, never a failure
        pass


def pcm_to_float(pcm: bytes) -> np.ndarray:
    """16-bit PCM bytes -> the float32 [-1, 1] array Whisper expects."""
    if not pcm:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


def _rms(frame: bytes) -> float:
    if not frame:
        return 0.0
    samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples * samples)))


def speech_seconds(pcm: bytes, threshold: float = DEFAULT_RMS_THRESHOLD) -> float:
    """
    Rough count of how much of `pcm` is above the noise gate.

    Used to decide whether a spillover buffer is a real command or just
    the tail of the wake word plus room tone.
    """
    if not pcm:
        return 0.0
    frame_bytes = FRAME_SAMPLES * 2
    voiced = sum(
        1
        for i in range(0, len(pcm) - frame_bytes, frame_bytes)
        if _rms(pcm[i:i + frame_bytes]) > threshold
    )
    return voiced * FRAME_MS / 1000.0


# ---------------------------------------------------------------------
# The transcriber
# ---------------------------------------------------------------------


class WhisperTranscriber:
    """
    ACTIVE-mode ears. Owns the Whisper model and the endpointer.

    The microphone is borrowed from VoiceEngine rather than opened
    independently: one process, one PyAudio, one input stream at a time.
    """

    def __init__(self, voice, size: Optional[str] = None) -> None:
        self._voice = voice
        self._size = size or model_size()
        self._device = _device()
        self._compute = _compute_type(self._device)
        self._model = None
        self._load_error = ""
        self._loading = threading.Event()
        self._loaded = threading.Event()
        self._lock = threading.Lock()

    # -- model lifecycle -----------------------------------------------

    @property
    def available(self) -> bool:
        """True once the model is loaded and usable."""
        return self._model is not None

    @property
    def description(self) -> str:
        return f"faster-whisper {self._size} ({self._device}/{self._compute})"

    def preload_async(self) -> None:
        """
        Loads the model on a daemon thread.

        Called at startup so the first wake word does not pay for it.
        Loading "small" takes a few seconds cold, and the first run also
        downloads ~460 MB - neither should happen while a user is
        standing there mid-sentence. If the wake word arrives before
        loading finishes, ensure_loaded() waits for it.
        """
        if not _WHISPER_AVAILABLE:
            print(f"[Whisper] faster-whisper not installed ({_WHISPER_IMPORT_ERROR}).")
            print("[Whisper] ACTIVE mode will fall back to Vosk (English only).")
            return
        if self._loading.is_set():
            return
        self._loading.set()
        thread = threading.Thread(target=self._load, name="jarvis-whisper-load", daemon=True)
        thread.start()

    def _load(self) -> None:
        started = time.monotonic()
        os.makedirs(MODELS_DIR, exist_ok=True)
        print(f"[Whisper] Loading {self._size} ({self._device}/{self._compute})...")
        try:
            model = WhisperModel(
                self._size,
                device=self._device,
                compute_type=self._compute,
                download_root=MODELS_DIR,
            )
        except Exception as exc:  # noqa: BLE001 - retried on the CPU below
            model = None
            self._load_error = f"{type(exc).__name__}: {exc}"
            print(f"[Whisper] Could not load the model: {self._load_error}")

        # A CUDA load can fail for reasons that have nothing to do with
        # the weights - no cuDNN, a driver too old, a GPU already full.
        # None of those should cost the user multilingual transcription,
        # so drop to the CPU tier before giving up on Whisper entirely.
        if model is None and self._device == "cuda":
            print("[Whisper] Retrying on the CPU (int8).")
            # int8 outright, not _compute_type("cpu"): an explicit
            # JARVIS_WHISPER_COMPUTE=float16 was meant for the GPU that
            # just failed, and the CPU cannot run it.
            self._device, self._compute = "cpu", "int8"
            try:
                model = WhisperModel(
                    self._size,
                    device=self._device,
                    compute_type=self._compute,
                    download_root=MODELS_DIR,
                )
                self._load_error = ""
            except Exception as exc:  # noqa: BLE001 - stays optional; Vosk covers English
                self._load_error = f"{type(exc).__name__}: {exc}"
                print(f"[Whisper] CPU load also failed: {self._load_error}")

        if model is None:
            print("[Whisper] ACTIVE mode will fall back to Vosk (English only).")
            self._loaded.set()
            return

        with self._lock:
            self._model = model
        self._loaded.set()
        print(f"[Whisper] Ready in {time.monotonic() - started:.1f}s. {self.description}.")

    def ensure_loaded(self, timeout: Optional[float] = None) -> bool:
        """
        Blocks until the model is loaded (or failed). Returns usability.

        Starts the load if nobody called preload_async() - so a caller
        that forgets still works, just slower.
        """
        if self._model is not None:
            return True
        if not _WHISPER_AVAILABLE:
            return False
        if not self._loading.is_set():
            self.preload_async()
        if not self._loaded.wait(timeout):
            print("[Whisper] Still loading; using Vosk for this command.")
            return False
        return self._model is not None

    # -- transcription -------------------------------------------------

    def transcribe(self, pcm: bytes) -> str:
        """
        Transcribes finished audio. Returns "" if there is nothing in it.

        Never raises: a transcription failure degrades to "I heard
        nothing", which the session loop already knows how to handle.
        """
        if self._model is None or not pcm:
            return ""

        audio = pcm_to_float(pcm)
        if audio.size < SAMPLE_RATE * MIN_SPEECH:
            return ""

        started = time.monotonic()
        try:
            segments, info = self._model.transcribe(
                audio,
                language=_language(),          # None -> auto-detect
                task="transcribe",             # never "translate"
                initial_prompt=_initial_prompt(),
                beam_size=_beam_size(),
                vad_filter=True,               # trim silence before decoding
                # Each command is independent; carrying context across
                # them makes Whisper repeat the previous sentence when
                # this one is short or noisy.
                condition_on_previous_text=False,
            )
            text = " ".join(segment.text.strip() for segment in segments).strip()
        except Exception as exc:  # noqa: BLE001 - must not break the session loop
            print(f"[Whisper] Transcription failed: {type(exc).__name__}: {exc}")
            return ""

        elapsed = time.monotonic() - started
        detected = getattr(info, "language", "?")
        confidence = getattr(info, "language_probability", 0.0) or 0.0
        print(
            f"[Whisper] {len(audio) / SAMPLE_RATE:.1f}s audio -> "
            f"{elapsed:.1f}s decode, language={detected} ({confidence:.0%})."
        )
        return text

    # -- capture -------------------------------------------------------

    def _record(
        self,
        timeout: float,
        cancel: Optional[threading.Event] = None,
    ) -> bytes:
        """
        Captures one utterance and returns its PCM (b"" if none).

        `timeout` is patience for speech to START; once it has, the
        phrase runs until END_SILENCE of quiet or MAX_PHRASE.

        `cancel` is the session's inactivity Event, checked once per
        30 ms frame - the timer thread only ever sets a flag, and this
        is where it is read. Like the Vosk path, cancellation is honoured
        only before speech starts, never mid-sentence.
        """
        stream = self._voice.open_input_stream(frames_per_buffer=FRAME_SAMPLES)
        if stream is None:
            return b""

        threshold = _rms_threshold_override()
        calibrating = threshold is None
        noise: list[float] = []

        frames: list[bytes] = []
        voiced_run = 0
        speech_started: Optional[float] = None
        last_voice = 0.0
        started = time.monotonic()

        print("[Whisper] Listening... (speak now)")
        try:
            while True:
                now = time.monotonic()

                if speech_started is None:
                    if cancel is not None and cancel.is_set():
                        return b""
                    if now - started > timeout:
                        return b""
                elif now - speech_started > MAX_PHRASE:
                    print("[Whisper] Reached the phrase limit.")
                    break

                try:
                    frame = stream.read(FRAME_SAMPLES, exception_on_overflow=False)
                except Exception as exc:  # noqa: BLE001 - read error mid-capture
                    print(f"[Whisper] Microphone read failed: {exc}")
                    return b"".join(frames)

                level = _rms(frame)

                if calibrating:
                    noise.append(level)
                    if len(noise) >= CALIBRATION_FRAMES:
                        floor = float(np.median(noise))
                        threshold = max(DEFAULT_RMS_THRESHOLD * 0.4, floor * NOISE_MULTIPLIER)
                        calibrating = False
                        print(f"[Whisper] Noise floor {floor:.0f}, gate {threshold:.0f}.")
                    continue

                if level > threshold:
                    voiced_run += 1
                    last_voice = now
                    if speech_started is None and voiced_run >= START_FRAMES:
                        speech_started = now
                        print("\r[Whisper] Speech detected...", end="", flush=True)
                else:
                    voiced_run = 0

                # Keep a little pre-roll so the first consonant of the
                # sentence is not clipped off the front.
                frames.append(frame)
                if speech_started is None and len(frames) > START_FRAMES * 4:
                    frames.pop(0)

                if speech_started is not None and now - last_voice > END_SILENCE:
                    break

            return b"".join(frames)
        finally:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:  # noqa: BLE001 - teardown must stay quiet
                pass

    def listen(
        self,
        timeout: float = 15.0,
        cancel: Optional[threading.Event] = None,
        priming_audio: bytes = b"",
        on_capture: Optional[Callable[[float], None]] = None,
    ) -> str:
        """
        One ACTIVE-mode turn: capture, then transcribe. Returns "" when
        nothing usable was heard.

        `priming_audio` is the handoff from STANDBY - the PCM that
        followed the wake phrase. When it already contains a complete
        command (the usual case, because Vosk only finalises an
        utterance after the speaker stops), it is transcribed on its own
        and the microphone is never opened: the user has finished
        talking, so listening again would only add latency and record
        Jarvis's own acknowledgement.

        `on_capture` is called once, at the exact moment the microphone
        closes and before the decoder is touched. Decoding a command
        takes seconds, and that gap reads as "it did not hear me" unless
        something fills it - so the acknowledgement is fired from here
        rather than after the transcript comes back. It must not block:
        whatever time it takes is added to the pause it exists to hide.

        It receives the seconds of voiced audio captured, which is the
        only thing known about the utterance at this point - nothing has
        been transcribed yet - and is enough to tell a two-word
        pleasantry from a real command.
        """

        def acknowledge(voiced: float) -> None:
            # The beep is unconditional: it is the "captured" cue, and
            # it is short enough never to be in the way. Only the spoken
            # filler is a judgement call, and the callback makes it.
            capture_beep()
            if on_capture is None:
                return
            try:
                on_capture(voiced)
            except Exception as exc:  # noqa: BLE001 - a filler is never worth a crash
                print(f"[Whisper] Acknowledgement failed: {type(exc).__name__}: {exc}")

        if not self.ensure_loaded(timeout=1.0):
            return ""

        if priming_audio:
            voiced = speech_seconds(priming_audio)
            print(
                f"[Whisper] Spillover audio from standby: "
                f"{len(priming_audio) / 2 / SAMPLE_RATE:.1f}s ({voiced:.1f}s voiced)."
            )
            if voiced >= MIN_SPEECH:
                acknowledge(voiced)
                text = self.transcribe(priming_audio)
                if text:
                    safe_print(f"[You] {text}")
                    return text
                print("[Whisper] Nothing intelligible in the spillover; listening live.")

        pcm = self._record(timeout=timeout, cancel=cancel)
        if not pcm:
            return ""

        # Gated on actual speech, not merely on bytes: acknowledging a
        # cough or a door closing is worse than staying quiet.
        voiced = speech_seconds(pcm)
        if voiced >= MIN_SPEECH:
            acknowledge(voiced)

        text = self.transcribe(pcm)
        if text:
            safe_print(f"[You] {text}")
        else:
            print("[Whisper] Could not make out what was said.")
        return text


# ---------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------


def status() -> int:
    print("faster-whisper installed:", _WHISPER_AVAILABLE or f"NO ({_WHISPER_IMPORT_ERROR})")
    device = _device()
    print(f"  model        {model_size()}")
    print(f"  device       {device}")
    print(f"  compute      {_compute_type(device)}")
    print(f"  language     {_language() or 'auto-detect (Thai/English code-switching)'}")
    print(f"  beam size    {_beam_size()}")
    print(f"  cache        {MODELS_DIR}")
    if os.path.isdir(MODELS_DIR):
        total = sum(
            os.path.getsize(os.path.join(root, name))
            for root, _, names in os.walk(MODELS_DIR)
            for name in names
        )
        print(f"  cached size  {total / 1e6:.0f} MB")
    else:
        print("  cached size  not downloaded yet")
    return 0


def main() -> int:
    args = set(sys.argv[1:])

    if "--status" in args:
        return status()

    if not _WHISPER_AVAILABLE:
        print(f"faster-whisper is not installed: {_WHISPER_IMPORT_ERROR}")
        print("Fix with: pip install faster-whisper")
        return 1

    transcriber = WhisperTranscriber(voice=None)
    transcriber.preload_async()
    if not transcriber.ensure_loaded(timeout=900):
        return 1

    if "--listen" in args:
        from voice_engine import VoiceEngine

        transcriber._voice = VoiceEngine(enable_tts=False)
        safe_print(transcriber.listen(timeout=10.0) or "(nothing heard)")
        return 0

    # Self-test on synthetic audio: proves the model decodes without
    # needing a microphone or a person.
    print("\nSelf-test: transcribing 2 seconds of silence (expect empty)...")
    print(repr(transcriber.transcribe(b"\x00\x00" * SAMPLE_RATE * 2)))
    print("\nModel is ready. Run with --listen to try the microphone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
