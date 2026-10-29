"use client";

import { useState } from "react";
import { ImageOff } from "lucide-react";
import { artifactPath } from "@/lib/jarvis/artifacts";

/**
 * A picture Jarvis made, inside a reply: the image, which opens full size in a new tab.
 *
 * It is fetched through this app (GET /artifacts/<name>), which only hands it to the
 * signed-in person whose chat shows it. When it cannot be loaded - the brain is off, the
 * file was deleted, this deployment cannot reach the brain - a quiet line says so in its
 * place instead of a broken-image icon.
 *
 * Rendered inside the reply's <p>, so everything here is phrasing content (a, img, span).
 */
export function ArtifactImage({ name, alt }: { name: string; alt: string }) {
  const [failed, setFailed] = useState(false);
  const src = artifactPath(name);
  const label = alt || "Generated image";

  if (failed) {
    return (
      <span className="my-1 flex max-w-sm items-center gap-2 rounded-md border border-dashed border-slate-300 px-3 py-2 text-sm text-slate-600">
        <ImageOff className="size-4 shrink-0" aria-hidden="true" />
        <span>{label} could not be loaded.</span>
      </span>
    );
  }

  return (
    <a
      href={src}
      target="_blank"
      rel="noopener noreferrer"
      title="Open full size"
      className="my-1 block w-full max-w-sm overflow-hidden rounded-md border border-slate-300 bg-slate-100 outline-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-rose-600"
    >
      {/* A plain <img>: the source is this app's own session-checked route, which next/image's optimizer would fetch without the person's cookie. */}
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={src}
        alt={label}
        loading="lazy"
        decoding="async"
        onError={() => setFailed(true)}
        className="block aspect-square h-auto w-full object-contain"
      />
    </a>
  );
}
