"""
Module 8: Session state machine (STANDBY <-> ACTIVE)

Owns the listening loop. Everything else in Jarvis handles one command
at a time; this module decides *when* Jarvis is listening for commands
at all, and hands each transcript to the router in main.py.

    STANDBY  microphone open, every transcript discarded unless it
             contains a wake phrase. Nothing is dispatched, nothing
             reaches the language model, nothing leaves the machine.

                 wake phrase heard
                       |
                       v  short spoken acknowledgement
    ACTIVE   microphone stays hot between commands. Speech goes
             straight to the router - no wake word needed for the
             second, third, or tenth command in a row.

                 15 s without a command
                       |
                       v  spoken sign-off
    STANDBY

WHY A SEPARATE MODULE
---------------------
The old loop was owned by the wake word engine: it fired a callback,
the callback captured exactly one utterance, dispatched it, and returned
to idle. One command per wake word, and nowhere to put a session
concept. Pulling the loop out here means the audio backends stay dumb
(they capture; they do not decide) and the state machine has one obvious
home.

THE INACTIVITY TIMER
--------------------
ACTIVE mode ends on silence, and the timer that ends it is a plain
threading.Timer that sets an Event and nothing else. It never touches
the audio stream, never calls into the recogniser, and never speaks -
those all belong to the main thread, and a timer thread reaching into a
PyAudio stream is how you get a hang instead of a timeout.

The main thread notices in one of two places, both non-blocking:

    * VoiceEngine.stream_utterances checks the Event between 0.25 s
      audio chunks, so a listen in progress ends within a quarter of a
      second - and only between phrases, never mid-sentence.
    * the ACTIVE loop checks it after each turn.

The timer is PAUSED while a command runs. A workflow confirmation, an
API call, and a spoken reply can easily take longer than the whole
timeout, and a session must not expire because Jarvis itself was busy
talking. The clock restarts when Jarvis stops speaking.

HYBRID SPEECH-TO-TEXT
---------------------
The two states use two different recognisers, for two different jobs:

    STANDBY  Vosk     tiny and always-on. Its only job is spotting the
                      wake phrase, so it must be cheap enough to run
                      for hours against an open microphone.
    ACTIVE   Whisper  loaded once, used only inside a session.
                      Multilingual, so a command can be Thai, English,
                      or the mix of both this user actually speaks.

The switch happens in _active(), and the audio spoken during it is not
lost: _build_handoff slices the PCM that followed the wake phrase out
of the standby buffer (using Vosk's word timings) and _active feeds it
to Whisper as priming audio. "Jarvis, log ค่ากาแฟ 120 บาท" spoken in one
breath therefore arrives complete, transcribed by the recogniser that
can actually read it. Whisper being unavailable is not fatal - ACTIVE
falls back to Vosk and Vosk's own transcript of the spillover.

STANDBY BACKENDS
----------------
Two ways to spot the wake word, chosen with JARVIS_WAKE_BACKEND:

    vosk         (default) continuous offline transcription, matched
                 against WAKE_PHRASES. Any phrase works - "jarvis",
                 "hey jarvis", "wake up" - and adding one is a one-line
                 edit with no model training.
    openwakeword the dedicated always-on ONNX model in
                 wake_word_engine.py. Lower CPU and far fewer false
                 triggers, but it only knows the phrase it was trained
                 on, and the model files must be downloaded first.

Both end at the same place: a wake phrase was heard, so go ACTIVE.
"""

from __future__ import annotations

import itertools
import os
import re
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

import task_dispatcher


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

# Spoken forms that wake Jarvis in STANDBY. Matched against the offline
# transcript, so include the mishearings Vosk actually produces -
# "jervis" and "service" are both common for "jarvis".
WAKE_PHRASES: tuple[str, ...] = (
    "hey jarvis",
    "hey jervis",
    "ok jarvis",
    "okay jarvis",
    "hi jarvis",
    "jarvis",
    "jervis",
    "wake up jarvis",
    "wake up",
)

# Spoken the moment the wake word lands. Kept to two or three words:
# this plays while the user is still drawing breath to give the command,
# and a long greeting talks over them. Cycled rather than randomised so
# the sequence is reproducible in a demo.
ACK_PHRASES: tuple[str, ...] = (
    "I'm listening.",
    "Yes, sir.",
    "Go ahead.",
)

# Spoken once, after the first command of a session, to signal that the
# mic is still hot. Not repeated after every command - a follow-up cue
# each time turns into nagging, and the state is already visible in the
# terminal.
FOLLOW_UP_PHRASE = "Is there anything else?"

# Spoken when the session times out.
CLOSING_PHRASE = "If you need further assistance, just call for me."

# Seconds of silence (or unintelligible speech) that end an ACTIVE
# session. Overridable with JARVIS_ACTIVE_TIMEOUT for demos, where 15
# seconds of dead air is a long time to stand in front of an audience.
DEFAULT_ACTIVE_TIMEOUT = 15.0

# Cap on a single utterance in STANDBY. Lower than the command limit:
# nothing said in standby is acted on, so a long transcript is pure
# latency between the wake word and the acknowledgement.
STANDBY_PHRASE_LIMIT = 6.0

# How long a session will wait for the Whisper model if the wake word
# arrives before the background load has finished. Preloading at startup
# means this is normally zero; the wait exists so an early wake word
# gets the better recogniser instead of silently dropping to Vosk.
WHISPER_LOAD_GRACE = 20.0


def _active_timeout() -> float:
    raw = os.getenv("JARVIS_ACTIVE_TIMEOUT", "").strip()
    if not raw:
        return DEFAULT_ACTIVE_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        print(f"[Session] Ignoring invalid JARVIS_ACTIVE_TIMEOUT={raw!r}.")
        return DEFAULT_ACTIVE_TIMEOUT
    return value if value > 0 else DEFAULT_ACTIVE_TIMEOUT


class SessionState(Enum):
    """The two states of the listening loop."""

    STANDBY = "STANDBY"
    ACTIVE = "ACTIVE"


@dataclass
class Handoff:
    """
    What STANDBY passes to ACTIVE when the wake phrase is heard.

    People do not pause after a wake word - "jarvis, log my coffee"
    arrives in one breath - so whatever followed the phrase has already
    been spoken and must survive the switch of recogniser.

    Two forms of it, because the two recognisers want different things:

        text   Vosk's own transcript of the remainder. English only, and
               the fallback when Whisper is not available.
        audio  the raw PCM of the remainder, sliced out of the standby
               buffer at the wake phrase's end time. This is the one
               that matters: Whisper re-transcribes it, which is the
               only way a Thai spillover command is ever understood.
    """

    phrase: str = ""
    text: str = ""
    audio: bytes = b""

    def __bool__(self) -> bool:
        return bool(self.text or self.audio)


# ---------------------------------------------------------------------
# Wake phrase matching
# ---------------------------------------------------------------------


def _normalise(text: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace."""
    lowered = re.sub(r"[^\w\s]", " ", text.lower())
    return re.sub(r"\s+", " ", lowered).strip()


def find_wake_phrase(transcript: str) -> Optional[tuple[str, str]]:
    """
    Looks for a wake phrase in a STANDBY transcript.

    Returns (phrase_heard, remainder) or None. The remainder is whatever
    followed the wake phrase in the same breath, so "jarvis, log my
    coffee" wakes up AND dispatches the command - the user should not
    have to say it twice just because they spoke naturally.

    Longest phrases are tried first, so "wake up jarvis" is reported as
    itself rather than as a bare "jarvis" with "wake up" left in front.
    """
    text = _normalise(transcript)
    if not text:
        return None

    for phrase in sorted(WAKE_PHRASES, key=len, reverse=True):
        match = re.search(rf"\b{re.escape(phrase)}\b", text)
        if match:
            return phrase, text[match.end():].strip()
    return None


def wake_phrase_end_time(words: list, phrase: str) -> Optional[float]:
    """
    Where the wake phrase stops, in stream seconds, taken from Vosk's
    word timings - the cut point for the audio handed to Whisper.

    `words` is Vosk's per-word list ({"word", "start", "end"}) and
    `phrase` is the matched wake phrase. Returns None when the timings
    are missing or do not line up, in which case the caller keeps the
    whole utterance rather than guessing: a little extra audio in front
    of the command costs Whisper nothing, while cutting at the wrong
    place eats the command itself.
    """
    if not words or not phrase:
        return None

    spoken = [_normalise(str(w.get("word", ""))) for w in words]
    target = phrase.split()
    span = len(target)

    for i in range(len(spoken) - span + 1):
        if spoken[i:i + span] == target:
            end = words[i + span - 1].get("end")
            return float(end) if end is not None else None
    return None


# ---------------------------------------------------------------------
# The inactivity timer
# ---------------------------------------------------------------------


class InactivityTimer:
    """
    A pausable countdown that sets an Event when it expires.

    Deliberately minimal: the timer thread's only job is to flip a flag.
    Whoever is watching the flag decides what to do about it, on the
    main thread, where the microphone and the speakers live.

    All four operations are safe to call from any thread and any number
    of times - a stray reset() after cancel() restarts nothing.
    """

    def __init__(self, seconds: float, name: str = "inactivity") -> None:
        self._seconds = seconds
        self._name = name
        self._lock = threading.RLock()
        self._timer: Optional[threading.Timer] = None
        self._cancelled = False
        self._deadline: Optional[float] = None
        self.expired = threading.Event()

    @property
    def seconds(self) -> float:
        return self._seconds

    def start(self) -> None:
        """Starts (or restarts) the countdown from the full duration."""
        with self._lock:
            self._cancelled = False
            self.expired.clear()
            self._arm()

    # Reset and start are the same operation; both names read correctly
    # at their call sites ("start the session" / "the user spoke").
    reset = start

    def pause(self) -> None:
        """
        Stops the clock without expiring. Used while a command runs, so
        a slow API call or a long spoken answer cannot time the session
        out from under the user.
        """
        with self._lock:
            self._disarm()
            self._deadline = None

    def cancel(self) -> None:
        """Permanent stop. Any later reset() is ignored."""
        with self._lock:
            self._cancelled = True
            self._disarm()
            self._deadline = None

    def remaining(self) -> float:
        """Seconds left, for logging. 0.0 when not running."""
        with self._lock:
            if self._deadline is None:
                return 0.0
            return max(0.0, self._deadline - time.monotonic())

    # -- internals -----------------------------------------------------

    def _arm(self) -> None:
        self._disarm()
        if self._cancelled:
            return
        self._deadline = time.monotonic() + self._seconds
        self._timer = threading.Timer(self._seconds, self._fire)
        self._timer.daemon = True  # never keeps the process alive
        self._timer.name = f"jarvis-{self._name}-timer"
        self._timer.start()

    def _disarm(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _fire(self) -> None:
        # Runs on the timer thread. One flag set, nothing else.
        with self._lock:
            if self._cancelled:
                return
            self._deadline = None
        self.expired.set()


# ---------------------------------------------------------------------
# The state machine
# ---------------------------------------------------------------------


class SessionManager:
    """
    Drives STANDBY <-> ACTIVE and feeds transcripts to the router.

    `dispatch` is main.py's router. It returns False to shut the
    assistant down, exactly as it did when the wake word engine called
    it, so the shutdown path is unchanged.
    """

    def __init__(
        self,
        voice,
        dispatch: Callable[[str], bool],
        wake_engine=None,
        whisper=None,
        timeout: Optional[float] = None,
    ) -> None:
        self._voice = voice
        self._dispatch = dispatch
        self._wake_engine = wake_engine
        # ACTIVE-mode recogniser (whisper_manager.WhisperTranscriber).
        # None keeps the whole session on Vosk, which still works - it
        # just cannot understand Thai.
        self._whisper = whisper
        self._timer = InactivityTimer(timeout if timeout is not None else _active_timeout())
        self._acks = itertools.cycle(ACK_PHRASES)
        self._stopping = threading.Event()
        self.state = SessionState.STANDBY

    @property
    def wake_hint(self) -> str:
        """The phrase to tell the user to say, for the startup greeting."""
        if self._wake_engine is not None:
            return self._wake_engine.wake_phrase
        return WAKE_PHRASES[0].title()

    # -- state transitions ---------------------------------------------

    def _enter(self, state: SessionState, reason: str) -> None:
        previous, self.state = self.state, state
        print(f"[Session] {previous.value} -> {state.value}  ({reason})")

    def stop(self) -> None:
        """Ends the loop after the current turn. Safe from any thread."""
        self._stopping.set()
        self._timer.cancel()
        if self._wake_engine is not None:
            self._wake_engine.stop()

    # -- the loop ------------------------------------------------------

    def run(self) -> None:
        """
        The assistant's main loop. Returns when the user asks to shut
        down, when Ctrl+C is pressed, or when stop() is called.
        """
        print(
            f"[Session] Starting in STANDBY. Wake phrases: "
            f"{', '.join(repr(p) for p in WAKE_PHRASES[:4])}... "
            f"(active timeout {self._timer.seconds:.0f}s)"
        )
        try:
            while not self._stopping.is_set():
                handoff = self._standby()
                if self._stopping.is_set():
                    break
                if not self._active(handoff):
                    break
        except KeyboardInterrupt:
            print("\n[Session] Interrupted.")
        finally:
            self._timer.cancel()
            print("[Session] Listening loop ended.")

    # -- STANDBY -------------------------------------------------------

    def _standby(self) -> Handoff:
        """
        Blocks until a wake phrase is heard, then acknowledges it.

        Returns the Handoff: whatever the user said in the same breath
        after the wake phrase, as both Vosk's text and the raw audio, for
        the ACTIVE loop to run as the session's first command.
        """
        if self.state is not SessionState.STANDBY:
            self._enter(SessionState.STANDBY, "returning to idle")

        handoff = self._wait_for_wake()
        if self._stopping.is_set():
            return Handoff()

        self._enter(SessionState.ACTIVE, f"wake phrase {handoff.phrase!r}")
        # Blocking on purpose: the mic is closed at this point, and the
        # acknowledgement must finish before ACTIVE reopens it, or the
        # recogniser transcribes Jarvis's own voice as the first command.
        self._voice.speak(next(self._acks))
        return handoff

    def _wait_for_wake(self) -> Handoff:
        """Runs whichever standby backend is configured."""
        if self._wake_engine is not None:
            print("[Session] STANDBY: listening for the wake word (openWakeWord)...")
            detected = self._wake_engine.wait_for_wake(cancel=self._stopping)
            if not detected:
                return Handoff()
            # No spillover from this backend: it scores audio frames
            # without transcribing them, so it has no idea whether
            # anything followed the wake word. ACTIVE just listens.
            return Handoff(phrase=self._wake_engine.wake_phrase.lower())

        print("[Session] STANDBY: listening for a wake phrase (Vosk, low power)...")
        # silence_timeout=None: standby never gives up on its own.
        # The stream yields plain transcripts; standby only needs text.
        stream = self._voice.stream_utterances(
            silence_timeout=None,
            phrase_limit=STANDBY_PHRASE_LIMIT,
            cancel=self._stopping,
            label="Standby",
            detailed=True,
        )
        try:
            for utterance in stream:
                if self._stopping.is_set():
                    return Handoff()
                hit = find_wake_phrase(utterance)
                if hit is None:
                    # The whole point of standby: heard, understood,
                    # discarded. Logged only, never dispatched.
                    print(f"[Session] (ignored in standby) {utterance!r}")
                    continue
                # Returning breaks the loop, which closes the generator
                # and releases the mic before the acknowledgement plays -
                # otherwise Jarvis transcribes its own voice out of the
                # backlog.
                return self._build_handoff(utterance, *hit)
            return Handoff()
        finally:
            stream.close()

    def _build_handoff(self, utterance: str, phrase: str, remainder: str) -> Handoff:
        """
        Builds the handoff from what Vosk heard after the wake phrase.

        The listener yields plain transcripts, so the only thing that can
        cross into ACTIVE is text: whatever followed the wake phrase in
        the same breath. There is no PCM to slice, so Whisper never gets
        a second look at the spillover.
        """
        if remainder:
            print(f"[Session] Spillover after {phrase!r}: Vosk heard {remainder!r}.")
        return Handoff(phrase=phrase, text=remainder)

    # -- ACTIVE --------------------------------------------------------

    def _active(self, handoff: Optional[Handoff] = None) -> bool:
        """
        Runs the conversation until silence times it out.

        Returns True to go back to STANDBY, False to shut the assistant
        down (the router asked for it).
        """
        self._timer.start()
        commands_run = 0
        pending = handoff or Handoff()
        whisper = self._whisper_for_session()

        while not self._stopping.is_set():
            # Background results are spoken HERE and nowhere else: the
            # microphone is closed at this point in the loop, so an
            # announcement cannot be transcribed as a command, and cannot
            # talk over someone mid-sentence. The dispatcher never
            # touches the speakers itself.
            self._announce_background()

            remaining = self._timer.remaining() or self._timer.seconds

            if whisper is not None:
                # Whisper's turn: the spillover PCM (if any) is prepended
                # to this listen, so nothing spoken during the switch of
                # recogniser is lost.
                priming, pending = pending.audio, Handoff()
                if not priming:
                    print(f"[Session] ACTIVE: mic hot, {remaining:.0f}s to timeout.")
                    self._voice.play_listening_cue()
                command = whisper.listen(
                    timeout=max(1.0, remaining),
                    cancel=self._timer.expired,
                    priming_audio=priming,
                    on_capture=self._speak_filler,
                )
            elif pending.text:
                # No Whisper. Fall back to what Vosk already transcribed.
                command, pending = pending.text, Handoff()
            else:
                pending = Handoff()
                print(f"[Session] ACTIVE: mic hot, {remaining:.0f}s to timeout.")
                self._voice.play_listening_cue()
                # Two independent stops: the timer's Event (authoritative,
                # survives across turns) and the listen's own silence
                # timeout (a backstop, so the mic is never held open
                # forever if the timer thread dies).
                command = self._voice.listen(
                    timeout=max(1.0, remaining),
                    cancel=self._timer.expired,
                )
                # Vosk transcribes as it listens, so there is no decode
                # gap to cover - but the brain still takes a second or
                # two, and the acknowledgement has to come from
                # somewhere on this path too. The transcript is already
                # in hand here, so the length gate reads it directly
                # rather than guessing from a duration.
                if command:
                    self._voice.play_filler(text=command)

            if self._stopping.is_set():
                return False

            if not command:
                if self._timer.expired.is_set():
                    return self._time_out()
                # Heard something unintelligible, or a short silence
                # inside the window. The timer is untouched - only a
                # real command resets it - so keep listening.
                print("[Session] Nothing usable heard; session clock still running.")
                continue

            # A real command: stop the clock so a slow answer cannot
            # expire the session, run it, then start the clock fresh.
            self._timer.pause()
            keep_running = True
            try:
                keep_running = self._dispatch(command)
            except Exception as exc:  # noqa: BLE001 - one bad command must not end the session
                print(f"[Session] Error while handling {command!r}: {type(exc).__name__}: {exc}")
                self._voice.speak("Something went wrong handling that.")

            if not keep_running:
                print("[Session] Router requested shutdown.")
                self._timer.cancel()
                return False

            commands_run += 1
            if commands_run == 1:
                self._voice.speak(FOLLOW_UP_PHRASE)

            self._timer.reset()

        return False

    def _speak_filler(self, seconds: float) -> None:
        """
        Acknowledges a captured command the instant the mic closes.

        Called from inside the recogniser, before it starts decoding,
        so it has to return immediately: the voice engine plays a
        pre-synthesised file for this and never waits on the network.

        `seconds` is how long the user spoke for. Nothing has been
        transcribed yet, so that duration is the only way to tell "log
        four hundred baht for lunch at the place downstairs" from
        "thanks" - and the second of those wants silence, not a filler.
        """
        self._voice.play_filler(seconds=seconds)


    def _whisper_for_session(self):
        """
        The ACTIVE-mode recogniser, or None to stay on Vosk.

        Called once per session rather than once per command: the answer
        cannot change mid-session, and the handoff should be announced
        once, not before every sentence.
        """
        if self._whisper is None:
            return None
        if not self._whisper.ensure_loaded(timeout=WHISPER_LOAD_GRACE):
            print("[Session] Whisper unavailable; ACTIVE stays on Vosk (English only).")
            return None
        print(f"[Session] Handing off to Whisper for ACTIVE transcription ({self._whisper.description}).")
        return self._whisper

    def _announce_background(self) -> None:
        """
        Speaks whatever finished since the last turn.

        Never raises: a dispatcher problem must not end a session. If a
        task finishes while Jarvis is in STANDBY, its announcement waits
        in the queue and is spoken at the start of the next session -
        speaking into standby would let Vosk hear the word "jarvis" in
        Jarvis's own voice and wake itself up.
        """
        try:
            for line in task_dispatcher.dispatcher().drain_announcements():
                print(f"[Session] Background result: {line}")
                self._voice.speak(line)
        except Exception as exc:  # noqa: BLE001 - reporting must not break the loop
            print(f"[Session] Could not report a background result: "
                  f"{type(exc).__name__}: {exc}")

    def _time_out(self) -> bool:
        """Graceful end of an ACTIVE session."""
        # Last chance to report anything that landed during the silence,
        # before the session closes and the queue waits for the next one.
        self._announce_background()

        self._enter(
            SessionState.STANDBY,
            f"no command for {self._timer.seconds:.0f}s",
        )
        self._timer.cancel()

        still_running = task_dispatcher.dispatcher().running_labels()
        if still_running:
            self._voice.speak(
                f"Still working on the {still_running[0]}. I'll tell you when it's done."
                if len(still_running) == 1
                else f"Still working on {len(still_running)} background tasks."
            )

        self._voice.speak(CLOSING_PHRASE)
        return True


if __name__ == "__main__":
    print("Wake phrase matching:")
    samples = [
        "jarvis",
        "hey jarvis",
        "wake up",
        "hey jervis are you there",
        "jarvis log my netflix subscription fifteen dollars",
        "wake up jarvis",
        "the weather is nice today",
        "i was talking to somebody else",
    ]
    for phrase in samples:
        print(f"  {phrase!r:55} -> {find_wake_phrase(phrase)!r}")

    print("\nInactivity timer (1.0s, paused for 1.5s mid-way):")
    timer = InactivityTimer(1.0, name="demo")
    timer.start()
    time.sleep(0.5)
    timer.pause()
    print(f"  paused, expired={timer.expired.is_set()}")
    time.sleep(1.5)
    print(f"  after 1.5s paused, expired={timer.expired.is_set()}  (must be False)")
    timer.reset()
    fired = timer.expired.wait(2.0)
    print(f"  after reset, expired={fired}  (must be True)")
    timer.cancel()
