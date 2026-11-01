import { Download, ImageIcon, Presentation } from "lucide-react";
import { artifactPath, isArtifactImage } from "@/lib/jarvis/artifacts";
import { focusRing } from "./styles";

/**
 * A file Jarvis made, offered for download inside a reply: a PowerPoint presentation (or a
 * picture, when a reply links one instead of showing it). One ruled chip in the family of
 * the person's own attachments (AttachmentChip): what it is, the file's name, a download mark.
 *
 * A plain link with `download`: this app's GET /artifacts/<name> hands the file only to the
 * signed-in person whose chat shows it, as an attachment under its own name, so the browser
 * saves it and the chat stays where it is. When it cannot be fetched - the brain is off, the
 * file was deleted - the browser's download list says so.
 *
 * Rendered inside the reply's <p>, so everything here is phrasing content (a, span, svg).
 */
export function ArtifactFile({ name, label }: { name: string; label: string }) {
  const Icon = isArtifactImage(name) ? ImageIcon : Presentation;
  return (
    <a
      href={artifactPath(name)}
      download={name}
      className={`my-1 flex w-full max-w-sm items-center gap-3 rounded-md border border-slate-300 bg-white px-3 py-2 transition-colors duration-150 hover:border-slate-400 hover:bg-slate-50 ${focusRing}`}
    >
      <Icon className="size-5 shrink-0 text-slate-700" aria-hidden="true" />
      <span className="min-w-0 flex-1">
        <span className="block text-sm font-medium text-slate-900">{label || "Download file"}</span>
        <span className="block truncate text-xs text-slate-600" title={name}>
          {name}
        </span>
      </span>
      <Download className="size-4 shrink-0 text-slate-600" aria-hidden="true" />
    </a>
  );
}
