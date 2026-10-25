import { FileText, X } from "lucide-react";
import { formatBytes } from "@/lib/jarvis/attachments";
import { focusRing } from "./styles";

/**
 * A file, as one ruled chip: an icon, its name (cut short with an ellipsis when
 * it is long) and its size. The same chip in two places, so it is never styled
 * two ways: above the dock's field while the file waits to be sent (with a
 * remove button), and on the person's message once it has been.
 */
export function AttachmentChip({
  name,
  size,
  onRemove,
  removeDisabled = false,
  className = "",
}: {
  name: string;
  size: number;
  /** Shown only when given: the dock's chip can be taken off again. */
  onRemove?: () => void;
  removeDisabled?: boolean;
  className?: string;
}) {
  return (
    <span
      className={`inline-flex h-9 min-w-0 max-w-full items-center gap-2 rounded-md border border-slate-300 bg-white text-sm text-slate-900 ${
        onRemove ? "pl-2.5 pr-1" : "px-2.5"
      } ${className}`}
    >
      <FileText className="size-4 shrink-0 text-slate-700" aria-hidden="true" />
      <span className="sr-only">Attached file: </span>
      <span className="min-w-0 truncate font-medium" title={name}>
        {name}
      </span>
      <span className="shrink-0 text-xs tabular-nums text-slate-600">{formatBytes(size)}</span>
      {onRemove && (
        <button
          type="button"
          onClick={onRemove}
          disabled={removeDisabled}
          aria-label={`Remove ${name}`}
          className={`grid size-7 shrink-0 place-items-center rounded text-slate-700 transition-colors duration-150 hover:bg-slate-100 hover:text-slate-900 disabled:pointer-events-none disabled:opacity-40 ${focusRing}`}
        >
          <X className="size-4" aria-hidden="true" />
        </button>
      )}
    </span>
  );
}
