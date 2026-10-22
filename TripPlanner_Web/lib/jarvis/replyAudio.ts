import { parseMessageContent } from "./messageContent";

/**
 * Spoken replies: what the brain sends back as audio, and when the page may play it.
 *
 * The brain (Python, reply_audio.py) voices a reply with Edge-TTS and returns it
 * beside the text as `audio: { mime: "audio/mpeg", base64: "..." }`. It does so
 * only when asked (`speak: true`) and only for a reply whose plain text is at most
 * VOICE_REPLY_MAX_CHARS characters - the "conversational" size: a sentence or two,
 * not a plan or a list of search results.
 *
 * The page enforces that limit itself, too (`canSpeak`): it plays a reply only if
 * its own text passes, whatever audio arrived with it, so a brain that ignored the
 * rule still could not make the page read an essay aloud. The same number is
 * `VOICE_MAX_CHARS` in rich_reply.py.
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
 * The audio to hand the page for a stored Jarvis reply: `audio` itself when the
 * reply's text (the words, not a widget's data) passes `canSpeak`, otherwise null.
 */
export function speakableAudio(storedReply: string, audio: ReplyAudio | null): ReplyAudio | null {
  if (!audio) return null;
  return canSpeak(parseMessageContent(storedReply, "jarvis").text) ? audio : null;
}
