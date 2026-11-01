/**
 * Files Jarvis made. The brain (Python) saves each one in the Jarvis project's artifacts/
 * folder (_2, _3... after the time when two land in the same second):
 *
 *   - pictures (agents/creative_agent.py): image_YYYYMMDD_HHMMSS.png (.jpg or .webp if that
 *     is what came back), shown in a web chat reply as one Markdown image line:
 *       ![Generated Image](/artifacts/image_20261029_160717.png)
 *   - presentations (agents/publishing_agent.py): presentation_YYYYMMDD_HHMMSS.pptx (.pdf when
 *     the brain downloads them from Canva as PDFs), offered as one Markdown link:
 *       [Download Presentation](/artifacts/presentation_20261101_103000.pptx)
 *
 * Those exact forms - a same-origin /artifacts/ address and one of those names - are the only
 * Markdown a reply can hold (MessageText): an image line draws the picture, any other link to
 * such a file becomes a download. Anything else stays text, so a reply can never make the page
 * load or link an outside address this way. GET /artifacts/<name>
 * (app/artifacts/[name]/route.ts) fetches the file from the brain.
 *
 * Pure: no React, no network.
 */

export const ARTIFACTS_PATH = "/artifacts";

const STAMP = String.raw`\d{8}_\d{6}(?:_\d{1,2})?`;
const IMAGE_NAME = String.raw`image_${STAMP}\.(?:png|jpe?g|webp)`;
const FILE_NAME = String.raw`presentation_${STAMP}\.(?:pptx|pdf)`;

/** A file name Jarvis gives what it makes - nothing else is ever asked of the brain. */
export const ARTIFACT_NAME_RE = new RegExp(`^(?:${IMAGE_NAME}|${FILE_NAME})$`);
const IMAGE_NAME_RE = new RegExp(`^${IMAGE_NAME}$`);

// "!" for an image, then the label: short, on one line, with no brackets of its own.
const ARTIFACT_MARKDOWN = new RegExp(
  String.raw`(!?)\[([^\[\]\n]{0,120})\]\(/artifacts/(${IMAGE_NAME}|${FILE_NAME})\)`,
  "g"
);

/** What the browser asks for: /artifacts/<name>. */
export function artifactPath(name: string): string {
  return `${ARTIFACTS_PATH}/${name}`;
}

const MIME_BY_EXTENSION: Record<string, string> = {
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  webp: "image/webp",
  pptx: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  pdf: "application/pdf",
};

/** The content type a file with this name must have, or null for a name Jarvis never gives. */
export function artifactMime(name: string): string | null {
  if (!ARTIFACT_NAME_RE.test(name)) return null;
  return MIME_BY_EXTENSION[name.slice(name.lastIndexOf(".") + 1).toLowerCase()] ?? null;
}

/** True for a picture, which the page can show; anything else Jarvis made is a download. */
export function isArtifactImage(name: string): boolean {
  return IMAGE_NAME_RE.test(name);
}

export type ReplyPart =
  | { kind: "text"; text: string }
  | { kind: "image"; name: string; alt: string }
  | { kind: "file"; name: string; label: string };

/**
 * `text` cut into its plain stretches and the files between them, in order. An image line
 * (`![...]`) naming a picture draws it; every other match - a presentation, or a plain link
 * to a picture - is offered as a download.
 */
export function splitArtifacts(text: string): ReplyPart[] {
  const parts: ReplyPart[] = [];
  let cursor = 0;
  for (const match of text.matchAll(ARTIFACT_MARKDOWN)) {
    const start = match.index ?? 0;
    if (start > cursor) parts.push({ kind: "text", text: text.slice(cursor, start) });
    const [, bang, label, name] = match;
    parts.push(
      bang && isArtifactImage(name)
        ? { kind: "image", name, alt: label.trim() }
        : { kind: "file", name, label: label.trim() }
    );
    cursor = start + match[0].length;
  }
  if (cursor < text.length) parts.push({ kind: "text", text: text.slice(cursor) });
  return parts;
}
