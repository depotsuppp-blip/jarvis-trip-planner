/**
 * Copy rules for one itinerary stop: a bold place name with ONE brief
 * sentence under it (components/trip/TimelineList.tsx). Stage 2's prompt
 * (buildFinalPrompt in app/api/trigger-jarvis/route.ts) and the live-swap
 * prompt (app/api/trip/alternative/route.ts) both ask for exactly that, but a
 * schema can't hold a model to a word count - and Haiku, which writes these,
 * can still run long - so what comes back is clamped here instead of
 * trusted. A stop must never come back as a paragraph.
 *
 * Pure functions, no I/O.
 */

/** "At most 15 words": the limit the prompts state, and the one enforced here. */
export const SHORT_DESCRIPTION_MAX_WORDS = 15;
/**
 * A backstop for the cases a word count can't see: Thai and other scripts
 * written without spaces, or one absurdly long token.
 */
export const SHORT_DESCRIPTION_MAX_CHARS = 110;

// Words a sentence must not be left ending on when it is cut short.
const DANGLING_WORDS = new Set([
  "a", "an", "and", "at", "by", "for", "from", "in", "of", "on", "or", "the", "to", "with",
]);
const MIN_WORDS_BEFORE_CLAUSE_CUT = 4;

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// Trailing punctuation that only makes sense at the end of a sentence; the
// plan view sets this as a caption under the name, with no full stop.
function stripTrailingPunctuation(value: string): string {
  return value.replace(/[\s.,;:!?\-–—]+$/u, "");
}

/**
 * Forces whatever the model wrote into one short caption: whitespace
 * collapsed, the place name dropped from the front if it was repeated (it is
 * shown on its own line), only the first sentence kept, and at most
 * SHORT_DESCRIPTION_MAX_WORDS words / SHORT_DESCRIPTION_MAX_CHARS characters.
 *
 * Too long is cut at the first clause break (comma, semicolon, colon, dash)
 * that leaves a real phrase of at least four words, so the result is the
 * main clause and still reads as a thought; only with no such break is it
 * cut at the limit and marked with "…". An empty input stays empty.
 */
export function clampShortDescription(raw: string, placeName = ""): string {
  let text = raw.replace(/\s+/g, " ").trim();
  if (!text) return "";

  if (placeName) {
    text = text.replace(new RegExp(`^${escapeRegExp(placeName)}\\s*[:,\\-–—]\\s*`, "iu"), "");
  }

  // First sentence only: a terminator counts when a new sentence (or the end) follows it.
  const sentenceEnd = /[.!?。](?=\s+["'(\p{Lu}\p{N}]|\s*$)/u.exec(text);
  if (sentenceEnd) text = text.slice(0, sentenceEnd.index);

  let truncated = false;
  const words = text.split(" ");
  if (words.length > SHORT_DESCRIPTION_MAX_WORDS) {
    const kept = words.slice(0, SHORT_DESCRIPTION_MAX_WORDS);
    // The FIRST clause break with enough words before it - the main clause,
    // not the main clause plus half of its elaboration: a word that ends in
    // , ; or :, or a word that a lone dash follows.
    let cutAt = -1;
    for (let i = MIN_WORDS_BEFORE_CLAUSE_CUT - 1; i < kept.length; i++) {
      const dashFollows = i + 1 < kept.length && /^[-–—]$/.test(kept[i + 1]);
      if (/[,;:]$/.test(kept[i]) || dashFollows) {
        cutAt = i;
        break;
      }
    }
    if (cutAt >= 0) {
      text = kept.slice(0, cutAt + 1).join(" ");
    } else {
      while (kept.length > 1 && DANGLING_WORDS.has(kept[kept.length - 1].toLowerCase())) kept.pop();
      text = kept.join(" ");
      truncated = true;
    }
  }

  if (text.length > SHORT_DESCRIPTION_MAX_CHARS) {
    const room = text.slice(0, SHORT_DESCRIPTION_MAX_CHARS - 1);
    const lastSpace = room.lastIndexOf(" ");
    text = lastSpace > 0 ? room.slice(0, lastSpace) : room;
    truncated = true;
  }

  text = stripTrailingPunctuation(text);
  return truncated ? `${text}…` : text;
}

/**
 * The one plain string kept in `text` alongside the structured fields - for
 * plans' older readers and for prompts that quote a stop ("the original
 * stop was ..."). "Name: caption", or whichever half exists.
 */
export function composeStopText(placeName: string, shortDescription: string): string {
  return [placeName, shortDescription].filter(Boolean).join(": ");
}

function comparable(name: string): string {
  return name.toLowerCase().normalize("NFKD").replace(/[^\p{L}\p{N}]+/gu, "");
}

/**
 * Whether two renderings of a venue name are plausibly the same place - one
 * contains the other once case, accents and punctuation are ignored. Used
 * only to notice when the model's own `placeName` disagrees with the
 * candidate its `placeIndex` points at; both empty counts as no information,
 * not agreement.
 */
export function namesAgree(a: string, b: string): boolean {
  const x = comparable(a);
  const y = comparable(b);
  return x !== "" && y !== "" && (x.includes(y) || y.includes(x));
}
