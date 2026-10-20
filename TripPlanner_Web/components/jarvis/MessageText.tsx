import type { ReactNode } from "react";

/**
 * A reply's text, with web addresses drawn as real links - the trip Jarvis has
 * just planned arrives as one. The text is plain; addresses are found with a
 * pattern and nothing else in it is ever treated as markup. Only http(s)
 * matches, so a "javascript:" address can never become a link.
 */
const LINK_PATTERN = /https?:\/\/[^\s<>"'`]+/g;
// Sentence punctuation stuck to the end of an address belongs to the sentence.
const TRAILING_PUNCTUATION = /[.,;:!?)\]}]+$/;

export function MessageText({ text }: { text: string }): ReactNode {
  const parts: ReactNode[] = [];
  let cursor = 0;

  for (const match of text.matchAll(LINK_PATTERN)) {
    const url = match[0].replace(TRAILING_PUNCTUATION, "");
    if (!url) continue;
    parts.push(text.slice(cursor, match.index));
    parts.push(
      <a
        key={match.index}
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
  return <>{parts}</>;
}
