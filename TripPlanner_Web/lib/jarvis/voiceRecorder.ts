/**
 * The microphone, as the hub needs it: ask for it, record one clip, hand back a
 * Blob - or throw it away. Browser APIs only (getUserMedia, MediaRecorder); no
 * React and no network, so the controller (`chatController.ts`) decides what a
 * recording means and this file only knows how to make one.
 *
 * The browser decides the container: WebM/Opus in Chrome and Firefox, MP4/AAC in
 * Safari. The first type the browser can record from `PREFERRED_TYPES` is used,
 * and the Blob says which it got; the brain's speech-to-text reads all of them.
 *
 * The microphone is released the moment a recording ends, however it ends -
 * stop, discard, or the device vanishing - so the browser's recording indicator
 * never outlives the recording.
 */

/** Why a recording could not start, in the terms the person can act on. */
export type VoiceRecorderFailure = "unsupported" | "denied" | "no-device" | "busy" | "failed";

export class VoiceRecorderError extends Error {
  constructor(readonly kind: VoiceRecorderFailure) {
    super(kind);
  }
}

export interface RecordedAudio {
  blob: Blob;
  durationMs: number;
}

export interface ActiveRecording {
  /** Ends the recording and resolves with it. Rejects only if the browser lost the audio. */
  stop(): Promise<RecordedAudio>;
  /** Ends the recording and drops it. Safe to call at any time, any number of times. */
  discard(): void;
}

export interface RecordingOptions {
  /** The recording ended without anyone asking (the microphone was unplugged, or the browser stopped it). */
  onEnded?: () => void;
}

const PREFERRED_TYPES = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"];

function pickMimeType(): string | undefined {
  return PREFERRED_TYPES.find((type) => MediaRecorder.isTypeSupported(type));
}

function failureFor(error: unknown): VoiceRecorderFailure {
  const name = error instanceof DOMException ? error.name : "";
  if (name === "NotAllowedError" || name === "SecurityError") return "denied";
  if (name === "NotFoundError" || name === "OverconstrainedError") return "no-device";
  if (name === "NotReadableError" || name === "AbortError") return "busy";
  return "failed";
}

/** Whether this page can record at all (false on plain http outside localhost, and in old browsers). */
export function canRecord(): boolean {
  return typeof navigator !== "undefined" && !!navigator.mediaDevices?.getUserMedia && typeof MediaRecorder !== "undefined";
}

/**
 * Asks for the microphone (the browser shows its permission prompt the first
 * time) and starts recording. Resolves once audio is flowing; rejects with a
 * VoiceRecorderError otherwise.
 */
export async function startRecording(options: RecordingOptions = {}): Promise<ActiveRecording> {
  if (!canRecord()) throw new VoiceRecorderError("unsupported");

  let stream: MediaStream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 },
    });
  } catch (error) {
    throw new VoiceRecorderError(failureFor(error));
  }

  const release = () => stream.getTracks().forEach((track) => track.stop());

  let recorder: MediaRecorder;
  try {
    const mimeType = pickMimeType();
    recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
  } catch {
    release();
    throw new VoiceRecorderError("failed");
  }

  const chunks: Blob[] = [];
  let endedAt = 0;
  let asked = false; // stop() or discard() was called: the end is expected
  let finish: () => void = () => {};
  const ended = new Promise<void>((resolve) => {
    finish = resolve;
  });

  recorder.ondataavailable = (event) => {
    if (event.data.size > 0) chunks.push(event.data);
  };
  recorder.onstop = () => {
    endedAt = performance.now();
    release();
    finish();
    if (!asked) options.onEnded?.();
  };
  recorder.onerror = () => {
    // The browser gives up on its own; onstop follows and reports it through onEnded.
    if (recorder.state !== "inactive") recorder.stop();
  };
  // Unplugging the device ends the track without ending the recorder in some browsers.
  stream.getAudioTracks()[0]?.addEventListener("ended", () => {
    if (recorder.state !== "inactive") recorder.stop();
  });

  const startedAt = performance.now();
  recorder.start();

  const halt = () => {
    asked = true;
    if (recorder.state !== "inactive") recorder.stop();
    else release();
  };

  return {
    async stop() {
      halt();
      await ended;
      const type = recorder.mimeType || chunks[0]?.type || "audio/webm";
      return { blob: new Blob(chunks, { type }), durationMs: Math.max(0, endedAt - startedAt) };
    },
    discard() {
      chunks.length = 0;
      halt();
    },
  };
}
