"""
Module 1: Privacy-First Wake Word Engine (openWakeWord)

100% offline / local — no API keys, no accounts, no network calls during
idle listening. openWakeWord runs small ONNX (or tflite) models entirely
on-device. It ships several pretrained models out of the box (see
WAKE_WORD_MODEL below); this engine defaults to "hey_mycroft" because it
is guaranteed to exist in a standard installation. Swap WAKE_WORD_MODEL to
"hey_jarvis" (or a path to a custom-trained model) once a dedicated "Hey
Jarvis" model has been trained.

Requires:
    pip install openwakeword pyaudio numpy onnxruntime

One-time setup (needs internet ONCE, to fetch the local model files —
after this, detection never touches the network again). This is required
regardless of which model name is selected below:
    python -c "import openwakeword; openwakeword.utils.download_models()"
"""

from __future__ import annotations

import os
import re
import threading
from typing import Callable, Optional

import numpy as np
import pyaudio
from openwakeword.model import Model

# Active wake word model. Either the name of one of openWakeWord's
# pre-packaged models ("alexa", "hey_mycroft", "hey_jarvis", "hey_rhasspy",
# "timer", "weather") or a filesystem path to a custom-trained .onnx/.tflite
# model. Change this single value to switch wake words.
#
# "hey_jarvis" is an official pretrained openWakeWord model - it just isn't
# downloaded by a bare `pip install openwakeword`. If it errors with
# NO_SUCHFILE, run:  python train_jarvis.py
WAKE_WORD_MODEL = "hey_jarvis"


def start_active_listening() -> None:
    """
    Mock callback fired when the wake word is detected.
    Replace this with real logic (e.g. start recording a command,
    stream audio to an ASR pipeline, wake the assistant UI, etc.).
    """
    print("[Jarvis] Wake word detected -> entering active listening mode.")


class WakeWordEngine:
    """
    Wraps openWakeWord + PyAudio into a simple always-on, local-only
    listener. Audio is captured from the local mic and scored by an
    on-device ONNX model — nothing is transmitted off the machine
    during this idle phase.
    """

    def __init__(
        self,
        wakeword_model: str = WAKE_WORD_MODEL,
        threshold: float = 0.5,
        on_wake: Callable[[], None] = start_active_listening,
        inference_framework: str = "onnx",
        chunk_size: int = 1280,  # 80ms @ 16kHz - openWakeWord's expected frame size
        sample_rate: int = 16000,
    ) -> None:
        self._on_wake = on_wake
        self._threshold = threshold
        self._chunk_size = chunk_size
        self._sample_rate = sample_rate

        # Loads local ONNX/tflite model files - all inference is on-device.
        try:
            self._model = Model(
                wakeword_models=[wakeword_model],
                inference_framework=inference_framework,
            )
        except Exception as exc:
            raise RuntimeError(
                f"Could not load wake word model '{wakeword_model}' "
                f"(inference_framework='{inference_framework}'). openWakeWord "
                "ships model *definitions* but not the model *files* - they "
                "must be downloaded once before first use:\n\n"
                '    python -c "import openwakeword; openwakeword.utils.download_models()"\n\n'
                f"Original error: {exc}"
            ) from exc

        self._audio = pyaudio.PyAudio()
        self._stream: Optional[pyaudio.Stream] = None
        self._running = False

        self.wake_phrase = self._friendly_name(wakeword_model)
        print(f"[Jarvis] Ready! Say '{self.wake_phrase}' to activate.")

    @staticmethod
    def _friendly_name(model: str) -> str:
        """
        Turns a model name or path into the phrase the user should say:
        'hey_jarvis' -> 'Hey Jarvis', './hey_jarvis_v0.1.onnx' -> 'Hey Jarvis'.
        """
        base = os.path.basename(model)
        for suffix in (".onnx", ".tflite"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
        # Drop a trailing version tag like '_v0.1'.
        base = re.sub(r"_v\d+(\.\d+)*$", "", base)
        return base.replace("_", " ").strip().title()

    def _open_stream(self) -> None:
        self._stream = self._audio.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=self._sample_rate,
            input=True,
            frames_per_buffer=self._chunk_size,
        )

    def _close_stream(self) -> None:
        """Releases the mic without tearing down PyAudio itself."""
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:  # noqa: BLE001 - teardown must stay quiet
                pass
            self._stream = None

    def wait_for_wake(self, cancel: Optional["threading.Event"] = None) -> bool:
        """
        Blocks until the wake word is heard, then releases the mic and
        returns True. Returns False if `cancel` was set or stop() was
        called first.

        This is the state-machine entry point: session_manager.py drives
        STANDBY with it and owns what happens next, instead of this class
        firing a callback into an assistant it knows nothing about. The
        mic is closed before returning precisely because the caller wants
        it - two input streams on one device is unreliable on Windows.
        """
        self._running = True
        self._open_stream()
        try:
            while self._running:
                if cancel is not None and cancel.is_set():
                    return False
                raw = self._stream.read(self._chunk_size, exception_on_overflow=False)
                audio_frame = np.frombuffer(raw, dtype=np.int16)
                predictions = self._model.predict(audio_frame)
                if any(score >= self._threshold for score in predictions.values()):
                    return True
            return False
        finally:
            self._close_stream()
            # Clear internal prediction buffers so the same utterance
            # cannot immediately re-trigger on the next call.
            self._model.reset()

    def listen_forever(self) -> None:
        """
        Blocking loop: pulls raw PCM frames from the local microphone,
        scores them with the on-device openWakeWord model, and fires
        on_wake() when the wake word crosses `threshold`. A misbehaving
        on_wake handler is caught and logged rather than allowed to
        kill the listening loop.

        The capture stream is released for the duration of the callback.
        That matters as soon as on_wake() wants the microphone itself
        (speech-to-text does): two open input streams on one device is
        unreliable on Windows, and the idle stream would otherwise pile
        up a large backlog of stale frames while the user is talking.
        """
        self._running = True
        self._open_stream()
        print("[Jarvis] Listening locally for the wake word... (Ctrl+C to stop)")

        try:
            while self._running:
                raw = self._stream.read(self._chunk_size, exception_on_overflow=False)
                audio_frame = np.frombuffer(raw, dtype=np.int16)
                predictions = self._model.predict(audio_frame)

                if any(score >= self._threshold for score in predictions.values()):
                    # Hand the microphone over to the callback.
                    self._close_stream()
                    try:
                        self._on_wake()
                    except Exception as exc:  # noqa: BLE001 - must never kill the idle loop
                        print(f"[Jarvis] on_wake callback raised an error: {exc}")
                    finally:
                        # Clear internal prediction buffers so the same
                        # utterance doesn't immediately re-trigger.
                        self._model.reset()
                        if self._running:
                            self._open_stream()
                            print("[Jarvis] Listening for the wake word again...")
        except KeyboardInterrupt:
            print("[Jarvis] Stopping wake word engine.")
        finally:
            self.stop()

    def stop(self) -> None:
        """Full teardown: releases the stream and PyAudio. Idempotent."""
        self._running = False
        self._close_stream()
        if self._audio is not None:
            try:
                self._audio.terminate()
            except Exception:  # noqa: BLE001 - teardown must stay quiet
                pass
            self._audio = None


if __name__ == "__main__":
    engine = WakeWordEngine()
    engine.listen_forever()
