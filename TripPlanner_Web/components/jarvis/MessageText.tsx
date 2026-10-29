import { Fragment, type ReactNode } from "react";
import { splitArtifactImages } from "@/lib/jarvis/artifacts";
import { ArtifactImage } from "./ArtifactImage";

/**
 * A reply's text, with web addresses drawn as real links - the trip Jarvis has
 * just planned arrives as one - and a picture Jarvis made drawn as the picture.
 *
 * The text is plain; nothing in it is treated as markup except two things found
 * with a pattern:
 *   - http(s) addresses become links (so a "javascript:" address can never become one);
 *   - the one Markdown image form Jarvis writes for a picture it made,
 *     `![Generated Image](/artifacts/image_YYYYMMDD_HHMMSS.png)`, becomes that image
 *     (lib/jarvis/artifacts.ts: a same-origin address and a name Jarvis gives, nothing else).
 */
const LINK_PATTERN = /https?:\/\/[^\s<>"'`]+/g;
// Sentence punctuation stuck to the end of an address belongs to the sentence.
const TRAILING_PUNCTUATION = /[.,;:!?)\]}]+$/;

function withLinks(text: string, keyPrefix: string): ReactNode[] {
  const parts: ReactNode[] = [];
  let cursor = 0;

  for (const match of text.matchAll(LINK_PATTERN)) {
    const url = match[0].replace(TRAILING_PUNCTUATION, "");
    if (!url) continue;
    parts.push(text.slice(cursor, match.index));
    parts.push(
      <a
        key={`${keyPrefix}:${match.index}`}
        href={url}
        target="_blank"
        rel="noopener noreferrer"
        className="font-medium text-slate-900 underline underline-offset-2 [overflow-wrap:anywhere] outline-none hover:text-rose-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-rose-600"
      >
        {url}
      </a>
    );
    cursor = match.index + url.length;
  }

  parts.push(text.slice(cursor));
  return parts;
}

export function MessageText({ text }: { text: string }): ReactNode {
  const parts = splitArtifactImages(text);
  return (
    <>
      {parts.map((part, index) =>
        part.kind === "image" ? (
          <ArtifactImage key={`image:${index}`} name={part.name} alt={part.alt} />
        ) : (
          // The blank lines around a picture are the reply's paragraph breaks, and the
          // picture is a block of its own: trimmed there, they would only add empty space.
          <Fragment key={`text:${index}`}>
            {withLinks(trimAroundImages(part.text, index > 0, index < parts.length - 1), `text:${index}`)}
          </Fragment>
        )
      )}
    </>
  );
}

/** A text stretch with the line breaks next to a picture taken off. */
function trimAroundImages(text: string, afterImage: boolean, beforeImage: boolean): string {
  let result = text;
  if (afterImage) result = result.replace(/^\s*\n/, "");
  if (beforeImage) result = result.replace(/\n\s*$/, "");
  return result;
}
