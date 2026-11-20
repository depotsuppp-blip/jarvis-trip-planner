import { BrainCircuit } from "lucide-react";
import { focusRing } from "./styles";

/**
 * "Save to Memory", beside the file waiting in the dock: a pressed-or-not toggle, OFF for every
 * new file. On, the file's key facts - projects, people, the stack, deadlines, budgets (at most 10) -
 * are also read into Jarvis's long-term memory, citing the file, when the message is sent; off,
 * Jarvis only reads the file to answer and remembers nothing from it.
 *
 * The same 36px ruled chip as the attachment chip beside it (Paper, Perforation edge, 6px), with a
 * Muted brain; pressed it takes Talk's pressed look - an Ink fill and a white label - so "on" reads
 * the same way across the dock.
 */
export function SaveToMemoryToggle({
  pressed,
  onToggle,
  disabled = false,
}: {
  pressed: boolean;
  onToggle: () => void;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onToggle}
      disabled={disabled}
      aria-pressed={pressed}
      title="Extract key facts into long-term Memory Graph"
      className={`group inline-flex h-9 shrink-0 items-center gap-1.5 rounded-md border border-slate-300 bg-white px-2.5 text-sm font-medium text-slate-700 transition-[background-color,border-color,color,scale] duration-150 hover:border-slate-900 hover:text-slate-900 active:scale-[0.97] disabled:cursor-not-allowed disabled:opacity-60 disabled:hover:border-slate-300 aria-pressed:border-slate-900 aria-pressed:bg-slate-900 aria-pressed:text-white ${focusRing}`}
    >
      <BrainCircuit className="size-4 shrink-0 text-slate-500 group-aria-pressed:text-white" aria-hidden="true" />
      Save to Memory
    </button>
  );
}
