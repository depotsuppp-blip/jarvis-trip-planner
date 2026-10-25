/**
 * Files the person attaches to a typed turn in the hub: which ones Jarvis can
 * read, how big they may be, what they are called on disk, and how a message
 * that carries one is stored. Pure - no React, no Node, never throws - so the
 * page (checks a file the moment it is picked) and the server (checks it again,
 * then saves it: lib/uploads.ts) apply the same rules.
 *
 * Jarvis reads an attachment with the filesystem tool server's read_file
 * (mcp_filesystem.py), so these are the formats that tool reads: PDF and Word
 * (.docx), whose text it extracts, and plain text of any kind. The limits are
 * the tool's too: it reads at most 1 MB of plain text, and PDF / Word files are
 * capped here at 20 MB (the tool would go to 50; a scan that big has no text to
 * extract anyway).
 *
 * THE STORED FORM. `Message.content` is one string. A message with no file is
 * stored as its text, as always. One with a file is stored as a JSON object:
 *
 *   { "text": "What does this say?", "attachments": [ { "path": "uploads/3f...-report.pdf",
 *     "name": "report.pdf", "mime": "application/pdf", "size": 48213 } ] }
 *
 * `path` is relative to the Jarvis project root - the folder the brain and its
 * filesystem tool run in - and is what the brain is told to read; `name` is
 * what the person called the file. parseStoredUserMessage reads it back, and
 * anything that is not exactly this shape stays the plain text it was.
 */

export interface Attachment {
  /** Where the brain finds it, relative to the Jarvis project root: always `uploads/<file>`. */
  path: string;
  /** The name the person's file had, cleaned up for display. */
  name: string;
  mime: string;
  /** Bytes. */
  size: number;
}

/** What the page shows for an attachment. */
export type AttachmentSummary = Pick<Attachment, "name" | "size">;

/** The folder under the Jarvis project root that uploads are saved in. */
export const UPLOADS_FOLDER = "uploads";

export const MAX_DOCUMENT_BYTES = 20 * 1024 * 1024;
export const MAX_TEXT_BYTES = 1024 * 1024;
/** Per message. The stored form is a list so that this can grow; the page attaches one. */
export const MAX_ATTACHMENTS = 1;

const DOCUMENT_TYPES: Record<string, string> = {
  pdf: "application/pdf",
  docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
};

// Read as plain text by the tool. Code and data formats included: "what does this script do?" is a fair question.
const TEXT_EXTENSIONS = [
  "txt", "md", "markdown", "rst", "csv", "tsv", "json", "jsonl", "xml", "html", "htm", "yaml", "yml",
  "toml", "ini", "cfg", "conf", "log", "srt", "vtt", "tex", "sql",
  "py", "js", "mjs", "cjs", "ts", "tsx", "jsx", "java", "kt", "c", "h", "cpp", "hpp", "cs", "go", "rs",
  "rb", "php", "swift", "sh", "ps1", "bat", "css", "scss",
] as const;
const TEXT_SET: ReadonlySet<string> = new Set(TEXT_EXTENSIONS);

/** For the file picker's `accept`: the extensions above, so the dialog offers only what Jarvis can read. */
export const ACCEPTED_FILE_TYPES = [...Object.keys(DOCUMENT_TYPES), ...TEXT_EXTENSIONS].map((ext) => `.${ext}`).join(",");

const UNSUPPORTED_MESSAGE = "Jarvis can read PDF, Word (.docx) and text files.";

export type FileKind = "pdf" | "docx" | "text";

/** The lower-case extension of a file name, without the dot ("" when it has none). */
export function extensionOf(name: string): string {
  const base = baseName(name);
  const dot = base.lastIndexOf(".");
  return dot > 0 ? base.slice(dot + 1).toLowerCase() : "";
}

/** What kind of file `name` is, as far as Jarvis's reader goes, or null when it cannot read it. */
export function fileKindOf(name: string): FileKind | null {
  const ext = extensionOf(name);
  if (ext === "pdf" || ext === "docx") return ext;
  return TEXT_SET.has(ext) ? "text" : null;
}

export function mimeFor(name: string, reported = ""): string {
  const ext = extensionOf(name);
  return DOCUMENT_TYPES[ext] ?? (reported.trim() || "text/plain");
}

/**
 * Why this file cannot be attached, in a sentence for the person - or null when it can.
 * Only the name and size are looked at here; the server also checks the bytes.
 */
export function attachmentProblem(name: string, size: number): string | null {
  const kind = fileKindOf(name);
  if (!kind) return UNSUPPORTED_MESSAGE;
  if (!Number.isFinite(size) || size <= 0) return "That file is empty.";
  if (kind === "text" && size > MAX_TEXT_BYTES) return `Text files can be up to ${formatBytes(MAX_TEXT_BYTES)}.`;
  if (size > MAX_DOCUMENT_BYTES) return `Files can be up to ${formatBytes(MAX_DOCUMENT_BYTES)}.`;
  return null;
}

/** 820 B, 12 KB, 1.4 MB. */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 1024) return `${Math.max(0, Math.round(bytes || 0))} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  const mb = bytes / (1024 * 1024);
  return `${mb < 10 ? mb.toFixed(1).replace(/\.0$/, "") : Math.round(mb)} MB`;
}

function baseName(name: string): string {
  // A browser sends the bare name, but a hand-made request might send a path.
  const parts = String(name).split(/[\\/]/);
  return parts[parts.length - 1] ?? "";
}

function codePoints(text: string, max: number): string {
  return Array.from(text).slice(0, max).join("");
}

/** The person's file name, fit to show and to quote to the brain: no path, no control characters, at most 120 characters. */
export function displayName(name: string): string {
  const clean = baseName(name).normalize("NFC").replace(/[\u0000-\u001f\u007f]+/g, " ").replace(/\s+/g, " ").trim();
  return codePoints(clean, 120) || "file";
}

/**
 * The name a file is saved under: `<id>-<cleaned name>.<ext>`. Letters of any
 * script (Thai included, with its vowel and tone marks), digits, dots, dashes and
 * underscores survive; everything else - spaces, quotes, brackets, path
 * characters - becomes "_", so the path can be quoted to the brain and typed back
 * by it exactly. `id` makes the name unique and unguessable.
 */
export function storedFileName(original: string, id: string): string {
  const base = baseName(original).normalize("NFC");
  const ext = extensionOf(base);
  const stem = ext ? base.slice(0, base.length - ext.length - 1) : base;
  const cleaned = codePoints(
    stem
      .replace(/[^\p{L}\p{M}\p{N}._-]+/gu, "_")
      // A mark belongs on a letter (Thai vowels and tone marks do); one left on its own - an emoji's variation selector - goes.
      .replace(/(?<![\p{L}\p{N}\p{M}])\p{M}+/gu, "")
      .replace(/_{2,}/g, "_")
      .replace(/^[._-]+|[._-]+$/g, ""),
    60
  ).replace(/[._-]+$/, "");
  return `${id}-${cleaned || "file"}${ext ? `.${ext}` : ""}`;
}

// uploads/<one file name>: no folders below, no drive letters or streams, no control characters,
// no quotes or brackets (the brain quotes the path in a bracketed line). The brain checks the same.
const STORED_PATH = /^uploads\/[^/\\:\u0000-\u001f'"[\]]{1,200}$/;

export function isStoredPath(path: unknown): path is string {
  return typeof path === "string" && STORED_PATH.test(path) && !path.includes("..");
}

function readAttachment(value: unknown): Attachment | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const item = value as Record<string, unknown>;
  if (!isStoredPath(item.path) || typeof item.name !== "string" || !item.name.trim()) return null;
  const size = typeof item.size === "number" && Number.isFinite(item.size) && item.size >= 0 ? item.size : 0;
  return { path: item.path, name: item.name, mime: typeof item.mime === "string" ? item.mime : "", size };
}

/** What a message with these attachments is stored as (see the top of the file). No attachments: just the text. */
export function storedUserMessage(text: string, attachments: Attachment[]): string {
  if (attachments.length === 0) return text;
  return JSON.stringify({ text, attachments });
}

/** A stored user message back as its text and attachments. Anything that is not the stored form is all text. */
export function parseStoredUserMessage(raw: string): { text: string; attachments: Attachment[] } {
  const plain = { text: raw, attachments: [] };
  const trimmed = raw.trim();
  if (!trimmed.startsWith("{") || !trimmed.endsWith("}") || trimmed.length > 20_000) return plain;

  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    return plain;
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return plain;
  const body = parsed as Record<string, unknown>;
  if (Object.keys(body).some((key) => key !== "text" && key !== "attachments")) return plain;
  if (typeof body.text !== "string" || !Array.isArray(body.attachments) || body.attachments.length === 0) return plain;

  const attachments = body.attachments.map(readAttachment);
  if (attachments.some((item) => item === null)) return plain;
  return { text: body.text, attachments: attachments as Attachment[] };
}
