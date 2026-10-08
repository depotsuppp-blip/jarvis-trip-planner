"""
Module 12: Endpointing - Silero VAD and push-to-talk

Decides WHEN THE USER STOPPED TALKING, which is the half of speech-to-
text that happens before a single sample reaches a model.

Whisper transcribes a finished recording; it does not stream. So
something has to cut the recording, and the quality of that cut sets two
numbers the user feels directly: how long Jarvis waits after the last
word, and how much audio Whisper is made to decode.

-------------------------------------------------------------------
WHAT THIS REPLACES
-------------------------------------------------------------------
whisper_manager._record used an energy gate: the RMS of 30 ms frames
against a threshold of 3x the room's noise floor, measured over the
first 300 ms of each listen and then frozen. It ended a phrase after 0.8 s
with no frame above the gate, and otherwise at a 20 s cap. Energy cannot
tell a voice from a noise, so it fails in three ways:

    * a noise that comes and goes (a fan surging, keystrokes, a passing
      car) crosses the gate, every crossing restarts the "has it gone
      quiet" clock, and the 20 s cap becomes the only way out. That is
      the "huge audio chunk" - and Whisper then decodes all of it.
    * the floor is measured in the first 300 ms, which can be the
      listening chime or the user's first word. A floor measured on a
      voice puts the gate above the voice, and the command is never heard.
    * a hum that starts after calibration is, to a frozen gate, speech
      for as long as it lasts.

A trained model can: Silero VAD scores each 32 ms window with the
probability that it contains human speech, and fans, hums, keys and
clicks stayed below the speech threshold however loud they were (tested
up to 1,900 RMS; see the notes at the end of this docstring).

-------------------------------------------------------------------
WHERE THE MODEL COMES FROM (no new dependency)
-------------------------------------------------------------------
faster-whisper ships Silero VAD as an ONNX file and onnxruntime is
already installed for openWakeWord, so this module drives that file
directly: no torch (the project avoids it), no webrtcvad (a separate
native package to add), nothing to pip install. It costs about 0.2 ms of
CPU per 32 ms of audio.

It is run the way a live microphone needs - one window at a time, with
the recurrent state carried from window to window - and its output was
checked to be numerically identical to faster-whisper's own batch run
over the same audio. If the model cannot be loaded for any reason the
energy gate takes over (see EnergyVAD) and the console says why, so a
broken install degrades to the old behaviour instead of to silence.

-------------------------------------------------------------------
THE ENDPOINTING RULES (Endpointer)
-------------------------------------------------------------------
    start    START_FRAMES (3, ~100 ms) windows in a row at or above the
             speech probability. The audio from PRE_ROLL_MS before that
             point is kept, so the first consonant is not clipped.
    settle   nothing may START an utterance in the first SETTLE_MS (400)
             after the microphone opens. That is when the room is still
             ringing from the acknowledgement or chime that preceded the
             listen. Silero judges what a sound is, not how loud it is
             (a decayed copy of speech at a tenth of the level still
             scored 0.93 in testing), so the fading tail of Jarvis's own
             "...sir" can read as a voice. A phantom capture there would
             cost a whole Whisper decode and leave the microphone shut
             when the real command came.
             Speech that begins inside the window is not lost: the
             pre-roll holds it and the utterance starts the moment the
             window ends. The one casualty is a sound that begins AND
             ends inside it - a bare "yes" blurted before the chime has
             finished - which cannot be told from an echo and is not
             heard. Nobody answers inside 0.4 s of a cue, and the cost
             of being wrong the other way is a wasted decode.
    stay     once started, a window counts as speech while its
             probability is at or above (start - 0.15). The gap is
             hysteresis: it stops one hesitant window from ending a
             sentence.
    end      END_SILENCE_MS (600) of consecutive windows below that.
             Trailing silence is trimmed to TAIL_MS, so the recording
             handed to Whisper ends where the speech ends.

             This window is the one real trade-off in the module, so
             here is what it does. Silero's boundaries sit a little
             inside the speech, so a pause counts as 50-100 ms longer
             than it sounds. Measured on six synthetic
             sentences (two voices) with a pause inserted mid-way, the
             longest pause a sentence survived was:

                 window 400 ms -> ~350 ms     window 600 ms -> ~550 ms
                 window 500 ms -> ~450 ms     window 800 ms -> ~750 ms

             (within 50 ms either side across the sentences). The energy
             gate this replaces waited 800 ms, and when a noise kept
             crossing its gate it bridged every pause, which is how it
             made recordings that never ended. 600 ms is the default because a command cut in
             half costs a whole extra turn, while 100 ms either way costs
             almost nothing against a multi-second decode. Speak in one
             breath? Try 450. Dictate field by field ("log lunch ...
             twelve dollars ... food")? Try 800.
    discard  an utterance with under MIN_SPEECH of speech (a cough, a
             door) is dropped and listening continues in the same turn,
             without another chime.
    cap      MAX_PHRASE (12 s), a net for the room where nothing else
             works. Hitting it prints the way out (push-to-talk).

Every duration that decides where an utterance ends is counted in frames,
not read off the clock, so the same audio gives the same cut however fast
it is fed in - which is what makes the whole thing testable without a
microphone or a wait. (Only the patience for speech to START, the
`timeout`, uses the clock.)

-------------------------------------------------------------------
PUSH-TO-TALK
-------------------------------------------------------------------
A VAD detects human speech, not THE USER's speech. A TV, a colleague or
a podcast in the next room is speech to it too, and no threshold fixes
that. Push-to-talk takes the decision away from the model:

    JARVIS_PTT=off     (default) the VAD decides.
    JARVIS_PTT=auto    the VAD decides, until the key is held. Holding
                       it for PTT_HOLD_MS starts the utterance at the
                       key-down, switches the VAD off for it, and
                       releasing the key ends it. Nothing to reconfigure
                       when the room gets loud: just hold the key.
    JARVIS_PTT=hold    only the key records; ambient sound cannot start
                       a capture at all.

A key held shorter than PTT_HOLD_MS is ignored, so tapping it while
typing does nothing. A PTT utterance is also sent to Whisper with its
own VAD filter off (CaptureResult.ptt): the point of holding the key is
that the model's idea of "speech" was wrong.

Detection is a poll of Windows' GetAsyncKeyState once per frame: it
needs no hook, no admin rights and no package, and it cannot swallow or
delay a keystroke. The flip side is the same fact - the key still
reaches whatever window has focus. That makes SPACE a poor default (it
types spaces into the editor you are looking at while you talk), so the
default key is Right Ctrl, which does nothing on its own. Mouse side
buttons ("mouse4", "mouse5") and function keys work too:

    JARVIS_PTT_KEY=right ctrl | mouse4 | mouse5 | f8 | caps lock |
                   space | a-z | 0-9 | vk:0x75

-------------------------------------------------------------------
THE STREAMING SEAM
-------------------------------------------------------------------
capture_utterance() hands each chunk to an optional `on_audio` callback
as it is captured - the pre-roll first, then one 32 ms window at a time -
before it returns the finished recording. A batch recogniser ignores the
callback and uses CaptureResult.pcm (Whisper today). A streaming one
(a Deepgram WebSocket, whisper-streaming) opens its connection when the
first chunk arrives, forwards the rest, and asks for the transcript when
capture returns. Nothing in this module needs to change for that; see
the notes on `on_audio` for the one caveat (push-to-talk trimming).

-------------------------------------------------------------------
CONFIGURATION (.env)
-------------------------------------------------------------------
    JARVIS_VAD                silero (default) | energy (the old RMS gate)
    JARVIS_VAD_END_SILENCE_MS quiet that ends a phrase, default 600.
                              Lower is snappier and clips thinking
                              pauses (see THE ENDPOINTING RULES);
                              400 is about the floor.
    JARVIS_VAD_MAX_PHRASE     seconds, default 12
    JARVIS_VAD_SPEECH_PROB    0.1-0.95, default 0.5. Raise it if a noise
                              still starts captures; lower it for a quiet
                              or distant voice.
    JARVIS_VAD_THRESHOLD      an RMS integer; used by the energy gate only
    JARVIS_PTT, JARVIS_PTT_KEY   as above

What was measured, and what was not
-----------------------------------
Everything above was measured on synthetic speech (two Windows SAPI
voices) mixed into synthetic noise, through the real model and the real
capture loop, against the original energy gate from git on identical
audio: steady noise, a 120 Hz hum, a surging fan, keystrokes, a hum that
starts after calibration, speech from the first instant, and a second
voice talking in the background. Highest speech probability seen from
noise alone: 0.39 (white noise); hum 0.39, surging fan 0.08, keystrokes
0.06. NOT measured: a real microphone, a real room's reverberation, real
speakers bleeding into the mic, accents, or Thai. `python vad_capture.py
--listen` is the quickest way to try your own.

Run standalone:

    python vad_capture.py --status    # backend, thresholds, PTT key
    python vad_capture.py --listen    # capture one utterance, live
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, List, Optional

import numpy as np


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

SAMPLE_RATE = 16000

# Silero VAD scores exactly 512 samples at 16 kHz, so the capture frame
# is 512 samples: 32 ms. whisper_manager opens the microphone with this.
FRAME_SAMPLES = 512
FRAME_MS = FRAME_SAMPLES * 1000 / SAMPLE_RATE
CONTEXT_SAMPLES = 64                # tail of the previous window, fed with the next

START_FRAMES = 3                    # windows in a row that confirm speech has begun
SETTLE_MS = 400                     # after the mic opens, nothing may START an utterance
PRE_ROLL_MS = 320                   # audio kept from before that point
END_SILENCE_MS = 600                # quiet that ends a phrase
TAIL_MS = 160                       # trailing silence kept after a silence endpoint
MAX_PHRASE = 12.0                   # seconds; the net for a room where nothing works
MIN_SPEECH = 0.25                   # shorter than this is a cough, not a command

SPEECH_PROB = 0.5                   # probability that starts (and, minus the gap, sustains) speech
NEG_PROB_GAP = 0.15                 # hysteresis, as in faster-whisper's own VAD

PTT_DEFAULT_KEY = "right ctrl"
PTT_HOLD_MS = 250                   # a tap shorter than this is not a press-to-talk
PTT_PRE_ROLL_FRAMES = 4             # frames kept from before the key went down
PTT_TAIL_MS = 160                   # kept after the key comes up, so the last syllable survives
PTT_MAX_PHRASE = 30.0               # Whisper's window; a held key is the user's call

# Energy fallback (only used when Silero cannot be loaded, or on request).
ENERGY_MIN_GATE = 200.0
ENERGY_NOISE_MULTIPLIER = 3.0
ENERGY_CALIBRATION_FRAMES = 10
ENERGY_FLOOR_FALL = 0.2             # how fast the floor follows a quieter room
ENERGY_FLOOR_RISE = 0.02            # ...and a louder one (slowly, so speech cannot drag it up)


def _ms_to_frames(ms: float) -> int:
    return max(1, int(round(ms / FRAME_MS)))


def _s_to_frames(seconds: float) -> int:
    return max(1, int(round(seconds * 1000 / FRAME_MS)))


_ENV_LOADED = False


def _load_env() -> None:
    """Reads .env once, so this module also works run on its own."""
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=False)
    except Exception:  # noqa: BLE001 - .env is a convenience, never a requirement
        pass


def _env(name: str, default: str = "") -> str:
    _load_env()
    return os.getenv(name, default).strip()


def _number_env(name: str, default: float, low: float, high: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        print(f"[VAD] Ignoring invalid {name}={raw!r}.")
        return default
    if not low <= value <= high:
        print(f"[VAD] {name}={raw} is outside {low:g}-{high:g}; using {default:g}.")
        return default
    return value


@dataclass(frozen=True)
class CaptureConfig:
    """Every number the Endpointer works from, in frames."""

    start_prob: float = SPEECH_PROB
    end_prob: float = SPEECH_PROB - NEG_PROB_GAP
    start_frames: int = START_FRAMES
    settle_frames: int = _ms_to_frames(SETTLE_MS)
    pre_roll_frames: int = _ms_to_frames(PRE_ROLL_MS)
    end_silence_frames: int = _ms_to_frames(END_SILENCE_MS)
    tail_frames: int = _ms_to_frames(TAIL_MS)
    max_frames: int = _s_to_frames(MAX_PHRASE)
    min_speech_frames: int = _s_to_frames(MIN_SPEECH)
    ptt_mode: str = "off"                    # off | auto | hold
    ptt_hold_frames: int = _ms_to_frames(PTT_HOLD_MS)
    ptt_pre_roll_frames: int = PTT_PRE_ROLL_FRAMES
    ptt_tail_frames: int = _ms_to_frames(PTT_TAIL_MS)
    ptt_max_frames: int = _s_to_frames(PTT_MAX_PHRASE)

    @classmethod
    def from_env(cls, ptt_mode: str = "off") -> "CaptureConfig":
        start_prob = _number_env("JARVIS_VAD_SPEECH_PROB", SPEECH_PROB, 0.1, 0.95)
        end_ms = _number_env("JARVIS_VAD_END_SILENCE_MS", END_SILENCE_MS, 150, 3000)
        max_s = _number_env("JARVIS_VAD_MAX_PHRASE", MAX_PHRASE, 2, 60)
        return cls(
            start_prob=start_prob,
            end_prob=max(start_prob - NEG_PROB_GAP, 0.05),
            end_silence_frames=_ms_to_frames(end_ms),
            max_frames=_s_to_frames(max_s),
            ptt_mode=ptt_mode,
        )


# ---------------------------------------------------------------------
# Voice activity detectors
# ---------------------------------------------------------------------
#
# A detector is a factory for per-capture streams. The stream owns the
# state that must not leak between utterances (Silero's recurrent state,
# the energy gate's noise floor); the factory owns what is expensive and
# shareable (the loaded model). Either one answers the same question for
# a 512-sample window: how likely is this to be speech, 0.0 to 1.0.


def _rms(frame: bytes) -> float:
    samples = np.frombuffer(frame, dtype=np.int16)
    if samples.size == 0:
        return 0.0
    samples = samples.astype(np.float32)
    return float(np.sqrt(np.mean(samples * samples)))


def _window(frame: bytes) -> np.ndarray:
    """One capture frame as float32 [-1, 1], padded or cut to exactly 512 samples."""
    samples = np.frombuffer(frame, dtype=np.int16)
    if samples.size != FRAME_SAMPLES:
        fixed = np.zeros(FRAME_SAMPLES, dtype=np.int16)
        n = min(samples.size, FRAME_SAMPLES)
        fixed[:n] = samples[:n]
        samples = fixed
    return samples.astype(np.float32) / 32768.0


class _SileroStream:
    """One capture's worth of Silero state over a shared ONNX session."""

    def __init__(self, session) -> None:
        self._session = session
        self.reset()

    def reset(self) -> None:
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._context = np.zeros(CONTEXT_SAMPLES, dtype=np.float32)

    def prob(self, frame: bytes) -> float:
        samples = _window(frame)
        window = np.concatenate([self._context, samples])[None, :]
        out, self._h, self._c = self._session.run(
            None, {"input": window, "h": self._h, "c": self._c}
        )
        self._context = samples[-CONTEXT_SAMPLES:]
        return float(out.reshape(-1)[0])


_silero_lock = threading.Lock()
_silero_session = None
_silero_error = ""


def _silero_model_path() -> Optional[str]:
    """The Silero ONNX file inside the installed faster-whisper, if any."""
    try:
        import faster_whisper
    except Exception:  # noqa: BLE001 - no faster-whisper, no bundled model
        return None
    assets = os.path.join(os.path.dirname(faster_whisper.__file__), "assets")
    try:
        names = sorted(
            name for name in os.listdir(assets)
            if name.startswith("silero_vad") and name.endswith(".onnx")
        )
    except OSError:
        return None
    return os.path.join(assets, names[-1]) if names else None


def _load_silero():
    """
    The shared ONNX session, or None with `_silero_error` explaining why.

    Checked, not trusted: the file's input names must be the ones this
    wrapper drives, and one silent window has to come back as a
    probability. A faster-whisper release that bundles a model with a
    different signature therefore lands on the energy fallback with a
    message, instead of raising from inside the microphone loop.
    """
    global _silero_session, _silero_error
    with _silero_lock:
        if _silero_session is not None or _silero_error:
            return _silero_session

        path = _silero_model_path()
        if path is None:
            _silero_error = "the Silero model bundled with faster-whisper was not found"
            return None
        try:
            import onnxruntime

            options = onnxruntime.SessionOptions()
            options.inter_op_num_threads = 1
            options.intra_op_num_threads = 1
            options.enable_cpu_mem_arena = False
            options.log_severity_level = 4
            session = onnxruntime.InferenceSession(
                path, providers=["CPUExecutionProvider"], sess_options=options
            )
            names = sorted(i.name for i in session.get_inputs())
            if names != ["c", "h", "input"]:
                raise RuntimeError(f"unexpected model inputs {names}")
            probe = _SileroStream(session).prob(b"\x00\x00" * FRAME_SAMPLES)
            if not 0.0 <= probe <= 1.0:
                raise RuntimeError(f"unexpected model output {probe!r}")
        except Exception as exc:  # noqa: BLE001 - any failure means "use the energy gate"
            _silero_error = f"{type(exc).__name__}: {exc}"
            return None

        _silero_session = session
        return session


class SileroVAD:
    name = "silero"

    def __init__(self, session) -> None:
        self._session = session

    def new_stream(self) -> _SileroStream:
        return _SileroStream(self._session)


class _EnergyStream:
    """
    The old RMS gate, nearly unchanged. The one repair: the noise floor
    is still learned from the first 300 ms, but is then nudged by every
    frame that is not speech (down quickly, up slowly), so a room that
    DRIFTS - the fan spinning up, the traffic easing - is followed. A
    jump is not: a hum that starts after calibration and lands above the
    gate (3x the floor) is "speech" for as long as it lasts, exactly as
    it was before. That failure is why Silero is the default; this class
    exists so that a broken install still records something.
    """

    def __init__(self, override: Optional[float]) -> None:
        self._override = override
        self.reset()

    def reset(self) -> None:
        self._seen: List[float] = []
        self._floor: Optional[float] = None

    def prob(self, frame: bytes) -> float:
        level = _rms(frame)
        if self._override is not None:
            return 1.0 if level > self._override else 0.0
        if self._floor is None:
            self._seen.append(level)
            if len(self._seen) >= ENERGY_CALIBRATION_FRAMES:
                self._floor = float(np.median(self._seen))
            return 0.0
        if level > max(ENERGY_MIN_GATE, self._floor * ENERGY_NOISE_MULTIPLIER):
            return 1.0
        rate = ENERGY_FLOOR_FALL if level < self._floor else ENERGY_FLOOR_RISE
        self._floor += (level - self._floor) * rate
        return 0.0


class EnergyVAD:
    name = "energy"

    def __init__(self) -> None:
        raw = _env("JARVIS_VAD_THRESHOLD")
        self._override: Optional[float] = None
        if raw:
            try:
                self._override = float(raw)
            except ValueError:
                print(f"[VAD] Ignoring invalid JARVIS_VAD_THRESHOLD={raw!r}.")

    def new_stream(self) -> _EnergyStream:
        return _EnergyStream(self._override)


def load_vad(kind: Optional[str] = None):
    """
    The detector JARVIS_VAD asks for, falling back to the energy gate
    (and saying why) when Silero is requested but cannot be used.
    """
    choice = (kind or _env("JARVIS_VAD") or "silero").lower()
    if choice == "energy":
        return EnergyVAD()
    if choice != "silero":
        print(f"[VAD] Unknown JARVIS_VAD={choice!r}; using Silero.")
    session = _load_silero()
    if session is not None:
        return SileroVAD(session)
    print(f"[VAD] Silero unavailable ({_silero_error}); using the energy gate.")
    return EnergyVAD()


# ---------------------------------------------------------------------
# Push-to-talk
# ---------------------------------------------------------------------

_VK_NAMES = {
    "space": 0x20, "tab": 0x09, "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B,
    "caps lock": 0x14, "capslock": 0x14, "scroll lock": 0x91, "pause": 0x13,
    "shift": 0x10, "left shift": 0xA0, "right shift": 0xA1,
    "ctrl": 0x11, "control": 0x11, "left ctrl": 0xA2, "right ctrl": 0xA3,
    "alt": 0x12, "left alt": 0xA4, "right alt": 0xA5,
    "mouse3": 0x04, "mouse middle": 0x04, "middle mouse": 0x04,
    "mouse4": 0x05, "xbutton1": 0x05, "mouse5": 0x06, "xbutton2": 0x06,
}


def virtual_key(name: str) -> Optional[int]:
    """A key name from JARVIS_PTT_KEY as a Windows virtual-key code, or None."""
    text = " ".join(name.lower().replace("_", " ").replace("-", " ").split())
    if text.startswith("vk:"):
        try:
            return int(text[3:], 0)
        except ValueError:
            return None
    # "rctrl", "lctrl", "rightctrl" -> "right ctrl"
    for short, full in (("rctrl", "right ctrl"), ("lctrl", "left ctrl"),
                        ("rightctrl", "right ctrl"), ("leftctrl", "left ctrl"),
                        ("ralt", "right alt"), ("lalt", "left alt"),
                        ("rshift", "right shift"), ("lshift", "left shift")):
        if text == short:
            text = full
    if text in _VK_NAMES:
        return _VK_NAMES[text]
    if len(text) == 1 and text.isalnum():
        return ord(text.upper())
    if text.startswith("f") and text[1:].isdigit() and 1 <= int(text[1:]) <= 24:
        return 0x70 + int(text[1:]) - 1
    return None


def _windows_key_poller(key: str):
    """(poll, error): a no-argument callable that is True while `key` is down."""
    if sys.platform != "win32":
        return None, "push-to-talk polls the keyboard through user32, so it needs Windows"
    code = virtual_key(key)
    if code is None:
        return None, f"don't know a key called {key!r}"
    try:
        import ctypes

        get_state = ctypes.windll.user32.GetAsyncKeyState
        get_state.argtypes = [ctypes.c_int]
        get_state.restype = ctypes.c_short
    except Exception as exc:  # noqa: BLE001 - no user32 means no push-to-talk
        return None, f"could not reach user32 ({type(exc).__name__}: {exc})"
    # The high bit is "down right now"; the low bit ("pressed since the
    # last call") is deliberately ignored - it would fire on a tap that
    # happened before this listen began.
    return (lambda: bool(get_state(code) & 0x8000)), ""


_PTT_MODES = {
    "": "off", "0": "off", "off": "off", "false": "off", "no": "off",
    "1": "auto", "on": "auto", "true": "auto", "yes": "auto", "auto": "auto", "assist": "auto",
    "hold": "hold", "only": "hold", "ptt": "hold", "always": "hold",
}


class PushToTalk:
    """
    A key the user can hold to take over from the VAD.

    `is_down` is injectable so the logic can be tested without a
    keyboard; by default it polls the real key.
    """

    def __init__(
        self,
        mode: str = "off",
        key: str = PTT_DEFAULT_KEY,
        is_down: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.mode = mode if mode in ("off", "auto", "hold") else "off"
        self.key = key
        self.error = ""
        self._is_down = is_down
        if self.mode != "off" and self._is_down is None:
            self._is_down, self.error = _windows_key_poller(key)
            if self._is_down is None:
                print(f"[VAD] Push-to-talk disabled: {self.error}.")
                self.mode = "off"

    @classmethod
    def from_env(cls) -> "PushToTalk":
        raw = _env("JARVIS_PTT").lower()
        mode = _PTT_MODES.get(raw)
        if mode is None:
            print(f"[VAD] Unknown JARVIS_PTT={raw!r}; push-to-talk is off.")
            mode = "off"
        return cls(mode=mode, key=_env("JARVIS_PTT_KEY") or PTT_DEFAULT_KEY)

    @property
    def enabled(self) -> bool:
        return self.mode != "off" and self._is_down is not None

    def down(self) -> bool:
        if not self.enabled:
            return False
        try:
            return bool(self._is_down())
        except Exception:  # noqa: BLE001 - a flaky poll reads as "key up"
            return False


# ---------------------------------------------------------------------
# The endpointer
# ---------------------------------------------------------------------


class Endpointer:
    """
    Decides where one utterance starts and stops. A state machine over
    frames: no microphone, no clock, no model. It is fed a frame, the
    detector's probability for it and whether the push-to-talk key is
    down, and it says whether the utterance is over.

    feed() returns "" while capturing, else why capture ended:

        silence      the speaker stopped
        ptt-release  the held key came up
        max-length   MAX_PHRASE (or PTT_MAX_PHRASE) was reached
        too-short    ended on silence, but with under MIN_SPEECH of speech
                     in it; the caller discards it and carries on
    """

    def __init__(self, config: CaptureConfig) -> None:
        self.config = config
        self.reset()

    def reset(self) -> None:
        cfg = self.config
        # Long enough for the pre-roll, for the whole settle window plus
        # the frames that confirm speech after it (so a command begun the
        # instant the microphone opens loses nothing), and for the key.
        self._pre: Deque[bytes] = deque(
            maxlen=max(cfg.pre_roll_frames + cfg.start_frames,
                       cfg.settle_frames + cfg.start_frames,
                       cfg.ptt_hold_frames + cfg.ptt_pre_roll_frames)
        )
        self._frames: List[bytes] = []
        self._undelivered = 0
        self.started = False
        self.reason = ""
        self.ptt_driven = False
        self.voiced_frames = 0
        self.ptt_frames = 0
        self._voiced_run = 0        # speech windows in a row, settling or not
        self._armed_run = 0         # ...of which, the ones after the microphone settled
        self._silence_run = 0
        self._key_run = 0
        self._release_run = 0

    # -- results -------------------------------------------------------

    @property
    def pcm(self) -> bytes:
        return b"".join(self._frames)

    @property
    def voiced_seconds(self) -> float:
        """Speech in the utterance; for a held key, how long it was held."""
        frames = self.ptt_frames if self.ptt_driven else self.voiced_frames
        return frames * FRAME_MS / 1000.0

    def drain(self) -> bytes:
        """
        Audio added since the last call - the whole pre-roll the first
        time - for a streaming consumer. Never rewound: if the push-to-
        talk key trims the utterance later, what was already drained
        stays drained.
        """
        if not self.started or self._undelivered <= 0:
            return b""
        count = min(self._undelivered, len(self._frames))
        chunk = b"".join(self._frames[-count:]) if count else b""
        self._undelivered = 0
        return chunk

    # -- the machine ---------------------------------------------------

    def feed(self, frame: bytes, prob: float, key_down: bool = False, armed: bool = True) -> str:
        """
        `armed` is False while the microphone is still settling (see
        SETTLE_MS): the frame is kept for the pre-roll, but it cannot
        count towards confirming that speech has started. A held key is
        never held back by it.
        """
        cfg = self.config
        key_active = cfg.ptt_mode != "off"
        self._key_run = self._key_run + 1 if (key_active and key_down) else 0
        engaged = key_active and self._key_run >= cfg.ptt_hold_frames

        if not self.started:
            self._pre.append(frame)
            if cfg.ptt_mode != "hold":
                if prob >= cfg.start_prob:
                    self._voiced_run += 1
                    self._armed_run = self._armed_run + 1 if armed else 0
                else:
                    self._voiced_run = self._armed_run = 0
            if engaged:
                self._begin(by_key=True)
            elif cfg.ptt_mode != "hold" and self._armed_run >= cfg.start_frames:
                # Confirmed by windows AFTER the settle period, so an echo
                # that rings out just past it cannot arrive pre-confirmed.
                # The speech that led up to it (a "yes" said at the chime)
                # still counts towards how much speech the utterance holds.
                self._begin(by_key=False)
            return ""

        self._frames.append(frame)
        self._undelivered += 1

        if engaged and not self.ptt_driven:
            # The key went down after the VAD had already started on its
            # own - in a loud room, most likely a false start. The key is
            # the user's statement of where the utterance begins, so the
            # recording restarts there instead of carrying the noise
            # before it into the decoder.
            keep = cfg.ptt_hold_frames + cfg.ptt_pre_roll_frames
            self._frames = self._frames[-keep:]
            self._undelivered = min(self._undelivered, len(self._frames))
            self.ptt_driven = True
            self.ptt_frames = min(self._key_run, len(self._frames))
            self._silence_run = 0
            self._release_run = 0
            return ""

        if self.ptt_driven:
            return self._step_key(key_down)
        return self._step_vad(prob)

    def _begin(self, by_key: bool) -> None:
        cfg = self.config
        self.started = True
        if by_key:
            keep = cfg.ptt_hold_frames + cfg.ptt_pre_roll_frames
            self._frames = list(self._pre)[-keep:]
            self.ptt_driven = True
            self.ptt_frames = min(self._key_run, len(self._frames))
        else:
            # PRE_ROLL_MS of lead-in before the speech run began, however
            # long that run already was (it can pre-date the settle window).
            keep = min(len(self._pre), self._voiced_run + cfg.pre_roll_frames)
            self._frames = list(self._pre)[-keep:]
            self.voiced_frames = self._voiced_run
        self._pre.clear()
        self._undelivered = len(self._frames)

    def _step_vad(self, prob: float) -> str:
        cfg = self.config
        if prob >= cfg.end_prob:
            self.voiced_frames += 1
            self._silence_run = 0
        else:
            self._silence_run += 1
            if self._silence_run >= cfg.end_silence_frames:
                excess = max(0, cfg.end_silence_frames - cfg.tail_frames)
                if excess:
                    del self._frames[-excess:]
                enough = self.voiced_frames >= cfg.min_speech_frames
                return self._finish("silence" if enough else "too-short")
        if len(self._frames) >= cfg.max_frames:
            return self._finish("max-length")
        return ""

    def _step_key(self, key_down: bool) -> str:
        cfg = self.config
        if key_down:
            self._release_run = 0
            self.ptt_frames += 1
        else:
            self._release_run += 1
            if self._release_run >= cfg.ptt_tail_frames:
                return self._finish("ptt-release")
        if len(self._frames) >= cfg.ptt_max_frames:
            return self._finish("max-length")
        return ""

    def _finish(self, reason: str) -> str:
        self.reason = reason
        return reason


# ---------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------


@dataclass
class CaptureResult:
    """What one capture produced."""

    pcm: bytes = b""
    # silence | ptt-release | max-length | timeout | cancelled | mic-error | no-mic
    reason: str = ""
    voiced_seconds: float = 0.0
    # True when a held key, not the detector, decided this utterance. The
    # transcriber then skips its own VAD filter: the user held the key
    # because the detector's idea of "speech" was wrong.
    ptt: bool = False
    backend: str = ""

    @property
    def seconds(self) -> float:
        return len(self.pcm) / 2 / SAMPLE_RATE


def describe(vad=None, ptt: Optional[PushToTalk] = None, config: Optional[CaptureConfig] = None) -> str:
    """One line for the console: what is deciding when the user stopped talking."""
    vad = vad or load_vad()
    ptt = ptt or PushToTalk.from_env()
    cfg = config or CaptureConfig.from_env(ptt.mode)
    line = (
        f"{vad.name} VAD, end of speech after {cfg.end_silence_frames * FRAME_MS:.0f} ms of quiet, "
        f"{cfg.max_frames * FRAME_MS / 1000:.0f} s cap"
    )
    if ptt.enabled:
        how = "hold to talk, the VAD is off" if ptt.mode == "hold" else "hold to override the VAD"
        line += f"; push-to-talk on '{ptt.key}' ({how})"
    else:
        line += "; push-to-talk off (JARVIS_PTT=auto to enable)"
    return line


def capture_utterance(
    stream,
    *,
    timeout: float,
    cancel: Optional[threading.Event] = None,
    vad=None,
    ptt: Optional[PushToTalk] = None,
    config: Optional[CaptureConfig] = None,
    on_audio: Optional[Callable[[bytes], None]] = None,
    clock: Callable[[], float] = time.monotonic,
    quiet: bool = False,
) -> CaptureResult:
    """
    Reads `stream` until one utterance is complete and returns it.

    `stream` is anything with PyAudio's read(frames, exception_on_overflow=)
    that yields 16 kHz mono 16-bit PCM in FRAME_SAMPLES blocks.

    `timeout` is patience for speech to START; `cancel` is the session's
    inactivity Event. Both are honoured only before an utterance begins,
    never mid-sentence - cutting someone off loses the command they just
    spoke. A too-short utterance (a cough) is dropped without ending the
    listen, and the same timeout keeps running across it.

    `on_audio(chunk)` is the streaming seam: it is called with the
    pre-roll as soon as an utterance begins, then with every frame after
    it, while capture is still going. It must not block. Two things to
    know if a streaming recogniser is plugged in here: chunks are
    delivered before the endpoint is known, so the trailing silence the
    returned `pcm` trims has already been delivered; and a push-to-talk
    key that goes down after the VAD had begun restarts the utterance at
    the key-down for `pcm`, but cannot recall what was already delivered
    - a streaming consumer should treat `CaptureResult.ptt` as "use my
    own result from the key-down on", or simply run with JARVIS_PTT=hold.
    """
    vad = vad or load_vad()
    ptt = ptt or PushToTalk.from_env()
    cfg = config or CaptureConfig.from_env(ptt.mode)
    endpointer = Endpointer(cfg)
    detector = vad.new_stream()
    log = (lambda *a, **k: None) if quiet else print

    # The full settings are printed once, at startup (describe); a listen
    # only needs to say what the user can do right now.
    if not ptt.enabled:
        cue = "speak now"
    elif ptt.mode == "hold":
        cue = f"hold '{ptt.key}' and speak"
    else:
        cue = f"speak now, or hold '{ptt.key}'"
    log(f"[VAD] Listening... ({cue})")
    began = clock()
    announced = False
    frames_read = 0

    while True:
        if not endpointer.started:
            if cancel is not None and cancel.is_set():
                return CaptureResult(reason="cancelled", backend=vad.name)
            if clock() - began > timeout:
                return CaptureResult(reason="timeout", backend=vad.name)

        try:
            frame = stream.read(FRAME_SAMPLES, exception_on_overflow=False)
        except Exception as exc:  # noqa: BLE001 - read error mid-capture
            log(f"[VAD] Microphone read failed: {exc}")
            return CaptureResult(
                pcm=endpointer.pcm if endpointer.started else b"",
                reason="mic-error",
                voiced_seconds=endpointer.voiced_seconds,
                ptt=endpointer.ptt_driven,
                backend=vad.name,
            )

        frames_read += 1

        # The VAD is skipped in hold mode: nothing it says can matter.
        prob = 0.0 if cfg.ptt_mode == "hold" else detector.prob(frame)
        reason = endpointer.feed(frame, prob, ptt.down(), armed=frames_read > cfg.settle_frames)

        if endpointer.started and not announced:
            announced = True
            what = "Push-to-talk engaged" if endpointer.ptt_driven else "Speech detected"
            log(f"\r[VAD] {what}...", end="", flush=True)

        if on_audio is not None and endpointer.started:
            chunk = endpointer.drain()
            if chunk:
                try:
                    on_audio(chunk)
                except Exception as exc:  # noqa: BLE001 - a consumer must not break capture
                    log(f"\n[VAD] on_audio failed: {type(exc).__name__}: {exc}")

        if not reason:
            continue

        if reason == "too-short":
            # A cough or a door, not a command. Forget it and keep
            # listening - no result, so no second chime upstream.
            log(f"\r[VAD] Ignored a {endpointer.voiced_seconds:.2f}s sound.        ")
            endpointer.reset()
            detector.reset()
            announced = False
            continue

        result = CaptureResult(
            pcm=endpointer.pcm,
            reason=reason,
            voiced_seconds=endpointer.voiced_seconds,
            ptt=endpointer.ptt_driven,
            backend=vad.name,
        )
        ended = {
            "silence": "end of speech",
            "ptt-release": "key released",
            "max-length": "hit the phrase limit",
        }.get(reason, reason)
        log(
            f"\r[VAD] Captured {result.seconds:.1f}s "
            f"({result.voiced_seconds:.1f}s {'held' if result.ptt else 'voiced'}) - {ended}.        "
        )
        if reason == "max-length" and not result.ptt:
            log(
                "[VAD] The room kept the microphone open. If this happens a lot, set "
                f"JARVIS_PTT=auto and hold '{ptt.key}' while you speak."
            )
        return result


# ---------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------


def status() -> int:
    ptt = PushToTalk.from_env()
    vad = load_vad()
    cfg = CaptureConfig.from_env(ptt.mode)
    print(f"detector          {vad.name}")
    if vad.name == "silero":
        print(f"  model           {_silero_model_path()}")
    elif _silero_error:
        print(f"  silero          unavailable: {_silero_error}")
    print(f"speech prob       start {cfg.start_prob:g}, stay {cfg.end_prob:g}")
    print(f"end of speech     {cfg.end_silence_frames} frames = {cfg.end_silence_frames * FRAME_MS:.0f} ms of quiet")
    print(f"pre-roll / tail   {cfg.pre_roll_frames * FRAME_MS:.0f} ms / {cfg.tail_frames * FRAME_MS:.0f} ms")
    print(f"phrase cap        {cfg.max_frames * FRAME_MS / 1000:.1f} s  (held key: {cfg.ptt_max_frames * FRAME_MS / 1000:.0f} s)")
    print(f"push-to-talk      {ptt.mode}" + (f"  key '{ptt.key}'  (hold {PTT_HOLD_MS} ms)" if ptt.enabled else ""))
    if ptt.enabled:
        print(f"  key is down now {ptt.down()}")
    print(f"\n{describe(vad, ptt, cfg)}")
    return 0


def main() -> int:
    args = set(sys.argv[1:])
    if "--status" in args or not args:
        return status()

    if "--listen" in args:
        from voice_engine import VoiceEngine

        voice = VoiceEngine(enable_tts=False)
        stream = voice.open_input_stream(frames_per_buffer=FRAME_SAMPLES)
        if stream is None:
            print("No microphone available.")
            return 1
        try:
            result = capture_utterance(stream, timeout=10.0)
        finally:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:  # noqa: BLE001 - teardown must stay quiet
                pass
            voice.shutdown()
        print(
            f"\nreason={result.reason}  captured={result.seconds:.2f}s  "
            f"voiced={result.voiced_seconds:.2f}s  ptt={result.ptt}  detector={result.backend}"
        )
        return 0

    print("usage: python vad_capture.py [--status | --listen]")
    return 2


if __name__ == "__main__":
    sys.exit(main())
