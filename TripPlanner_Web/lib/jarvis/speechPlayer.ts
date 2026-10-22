import type { ReplyAudio } from "./replyAudio";

/**
 * Plays one spoken reply at a time, and reports when it starts and when it is
 * over - however it ends. Browser only (it makes an HTMLAudioElement); nothing
 * here runs until `play` is called, so creating a player on the server is safe.
 *
 * `onEnd` is called exactly once per `play`: when the clip finishes, when it
 * fails to decode, when the browser refuses to start it (autoplay blocked), or
 * when `stop` (or the next `play`) cuts it off. `onStart` is called only if
 * playback really began - so the orb never claims to be speaking over silence.
 */

export interface SpeechHandlers {
  /** Playback began. */
  onStart?: () => void;
  /** Playback is over, for whatever reason. */
  onEnd: () => void;
}

export interface SpeechPlayer {
  play(audio: ReplyAudio, handlers: SpeechHandlers): void;
  /** Cuts off the clip that is playing, if any (its `onEnd` is called). */
  stop(): void;
}

export function createSpeechPlayer(): SpeechPlayer {
  let current: { element: HTMLAudioElement; finish: () => void } | null = null;

  function stop() {
    current?.finish();
  }

  return {
    stop,

    play(audio, { onStart, onEnd }) {
      stop();

      const element = new Audio(`data:${audio.mime};base64,${audio.base64}`);
      let finished = false;

      const finish = () => {
        if (finished) return;
        finished = true;
        element.removeEventListener("ended", finish);
        element.removeEventListener("error", finish);
        element.pause();
        if (current?.element === element) current = null;
        onEnd();
      };

      element.addEventListener("ended", finish);
      element.addEventListener("error", finish);
      current = { element, finish };

      // Rejects when the browser will not play without a gesture, or the data cannot be decoded.
      element.play().then(
        () => {
          if (!finished) onStart?.();
        },
        finish
      );
    },
  };
}
