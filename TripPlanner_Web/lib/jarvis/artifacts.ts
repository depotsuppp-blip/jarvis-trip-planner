/**
 * Pictures Jarvis made. The brain (Python, agents/creative_agent.py) saves each one in
 * the Jarvis project's artifacts/ folder as image_YYYYMMDD_HHMMSS.png (_2, _3... when two
 * land in the same second; .jpg or .webp if that is what came back), and a web chat reply
 * shows it as one Markdown image line:
 *
 *   ![Generated Image](/artifacts/image_20261029_160717.png)
 *
 * That exact form - a same-origin /artifacts/ address and one of those names - is the only
 * image a reply can draw (MessageText). Nothing else in a reply is Markdown, and an image
 * from anywhere else stays text, so a reply can never make the page load an outside address.
 * GET /artifacts/<name> (app/artifacts/[name]/route.ts) fetches the file from the brain.
 *
 * Pure: no React, no network.
 */

export const ARTIFACTS_PATH = "/artifacts";

const NAME = String.raw`image_\d{8}_\d{6}(?:_\d{1,2})?\.(?:png|jpe?g|webp)`;

/** A file name Jarvis gives a picture - nothing else is ever asked of the brain. */
export const ARTIFACT_NAME_RE = new RegExp(`^${NAME}$`);

// The alt text is short, on one line, and has no brackets of its own.
const IMAGE_LINE = new RegExp(String.raw`!\[([^\[\]\n]{0,120})\]\(/artifacts/(${NAME})\)`, "g");

/** What the browser asks for: /artifacts/<name>. */
export function artifactPath(name: string): string {
  return `${ARTIFACTS_PATH}/${name}`;
}

const MIME_BY_EXTENSION: Record<string, string> = {
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  webp: "image/webp",
};

/** The content type a picture with this name must have, or null for a name that is not one. */
export function artifactMime(name: string): string | null {
  if (!ARTIFACT_NAME_RE.test(name)) return null;
  return MIME_BY_EXTENSION[name.slice(name.lastIndexOf(".") + 1).toLowerCase()] ?? null;
}

export type ReplyPart = { kind: "text"; text: string } | { kind: "image"; name: string; alt: string };

/** `text` cut into its plain stretches and the pictures between them, in order. */
export function splitArtifactImages(text: string): ReplyPart[] {
  const parts: ReplyPart[] = [];
  let cursor = 0;
  for (const match of text.matchAll(IMAGE_LINE)) {
    const start = match.index ?? 0;
    if (start > cursor) parts.push({ kind: "text", text: text.slice(cursor, start) });
    parts.push({ kind: "image", name: match[2], alt: match[1].trim() });
    cursor = start + match[0].length;
  }
  if (cursor < text.length) parts.push({ kind: "text", text: text.slice(cursor) });
  return parts;
}
