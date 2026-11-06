import { parseMessageContent } from "./messageContent";

/**
 * Spoken replies: what the brain sends back as audio, and when the page may play it.
 *
 * The voice speaks ENGLISH, always. When asked (`speak: true`) the brain (Python)
 * sends what the voice says - `spoken_audio_text`: the reply itself when it is
 * already short English, else its English translation and summary, at most
 * VOICE_REPLY_MAX_CHARS characters - and that text voiced with Edge-TTS's English
 * voice as `audio: { mime: "audio/mpeg", base64: "..." }`. The reply shown in the
 * chat may be Thai; it is never what is played.
 *
 * The page enforces the limit itself, too (`canSpeak`): it plays audio only when the
 * text it says passes - the spoken text, or for a brain from before it the reply's
 * own words - so a brain that ignored the rule still could not make the page read
 * an essay aloud. The same number is `VOICE_MAX_CHARS` in rich_reply.py.
 *
 * Pure: no React, no browser APIs - the API routes use it as well as the page.
 */

export const VOICE_REPLY_MAX_CHARS = 200;

export interface ReplyAudio {
  mime: string;
  base64: string;
}

// 200 characters of speech is under 200 KB of MP3 (about 270 K base64 characters).
// This only stops a misbehaving brain from filling a JSON response and a page's memory.
const MAX_BASE64_CHARS = 2_000_000;
const AUDIO_MIME = /^audio\/(mpeg|mp3|ogg|webm|wav|x-wav|aac|mp4)$/i;
const BASE64 = /^[A-Za-z0-9+/]+={0,2}$/;

/** The brain's `audio` field as a ReplyAudio, or null when it is missing or not what it should be. */
export function parseReplyAudio(value: unknown): ReplyAudio | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const { mime, base64 } = value as Record<string, unknown>;
  if (typeof mime !== "string" || !AUDIO_MIME.test(mime)) return null;
  if (typeof base64 !== "string" || base64.length === 0 || base64.length > MAX_BASE64_CHARS) return null;
  if (!BASE64.test(base64)) return null;
  return { mime: mime.toLowerCase(), base64 };
}

/**
 * True when `text` is short enough to be read aloud: not blank, and at most
 * VOICE_REPLY_MAX_CHARS characters once trimmed (counted as characters, not UTF-16
 * units, so an emoji or a Thai letter counts once - the way Python counts).
 */
export function canSpeak(text: string | null | undefined): text is string {
  if (typeof text !== "string") return false;
  const trimmed = text.trim();
  if (trimmed === "") return false;
  // UTF-16 units are never fewer than characters: this skips counting a runaway string.
  if (trimmed.length > VOICE_REPLY_MAX_CHARS * 2) return false;
  return [...trimmed].length <= VOICE_REPLY_MAX_CHARS;
}

/**
 * The audio to hand the page for a stored Jarvis reply: `audio` itself when the text it says
 * passes `canSpeak` - the brain's English `spokenText` when there is one, else (a brain from
 * before it) the reply's own words, not a widget's data - otherwise null.
 */
export function speakableAudio(storedReply: string, audio: ReplyAudio | null, spokenText: string | null = null): ReplyAudio | null {
  if (!audio) return null;
  const says = spokenText ?? parseMessageContent(storedReply, "jarvis").text;
  return canSpeak(says) ? audio : null;
}
