import { randomBytes } from "node:crypto";
import { existsSync } from "node:fs";
import { mkdir, unlink, writeFile } from "node:fs/promises";
import path from "node:path";
import {
  UPLOADS_FOLDER,
  attachmentProblem,
  displayName,
  fileKindOf,
  mimeFor,
  storedFileName,
  type Attachment,
  type FileKind,
} from "@/lib/jarvis/attachments";

/**
 * Saving a file the person attached in the hub, so the Jarvis brain can read it.
 *
 * The brain reads attachments with its filesystem tool (mcp_filesystem.py), which
 * only sees the Jarvis project folder - the one api_server.py runs in, the parent
 * of this web app's folder. So a file is saved to `<Jarvis project>/uploads/` and
 * the brain is handed `uploads/<file>`. That only works when this web app and the
 * brain share a disk, which is how the hub runs on the owner's PC (`next dev` in
 * TripPlanner_Web, `python api_server.py` beside it). Anywhere else - Vercel, or
 * the Docker stack, where the two are separate containers - there is no such
 * folder, and attaching answers 503 with a sentence that says so.
 *
 * JARVIS_UPLOADS_DIR overrides the folder (an absolute path). It must be the
 * folder the brain sees as `uploads/` under its own root - for example one volume
 * mounted in both containers - or the brain will not find what was saved.
 *
 * The rules on names, types and sizes are shared with the page:
 * lib/jarvis/attachments.ts.
 */

export class UploadError extends Error {
  constructor(
    message: string,
    /** The HTTP status the route answers with. */
    readonly status: number
  ) {
    super(message);
  }
}

/** A checked file, ready to be written. */
export interface PreparedUpload {
  bytes: Buffer;
  kind: FileKind;
  originalName: string;
  mime: string;
}

const NOT_HERE =
  "Attachments work when the web app runs on the same computer as Jarvis's brain. This one can't save files for it.";

/** Where uploads are saved, or null when this server has no Jarvis project beside it. */
export function uploadsDirectory(): string | null {
  const configured = process.env.JARVIS_UPLOADS_DIR?.trim();
  if (configured) return path.resolve(configured);
  // The web app runs from TripPlanner_Web; the Jarvis project (api_server.py) is its parent folder.
  const project = path.resolve(/* turbopackIgnore: true */ process.cwd(), "..");
  return existsSync(path.join(project, "api_server.py")) ? path.join(project, UPLOADS_FOLDER) : null;
}

function startsWith(bytes: Buffer, signature: string, within = 0): boolean {
  const marker = Buffer.from(signature, "latin1");
  if (within === 0) return bytes.subarray(0, marker.length).equals(marker);
  return bytes.subarray(0, within + marker.length).includes(marker);
}

/** Checks a file's name, size and bytes. Throws UploadError (400 / 413 / 415) with a sentence for the person. */
export async function prepareUpload(file: File): Promise<PreparedUpload> {
  const problem = attachmentProblem(file.name, file.size);
  if (problem) {
    const status = !fileKindOf(file.name) ? 415 : file.size > 0 ? 413 : 400;
    throw new UploadError(problem, status);
  }
  const kind = fileKindOf(file.name) as FileKind;
  const bytes = Buffer.from(await file.arrayBuffer());

  // The extension says what the file claims to be; the first bytes say what it is.
  // A PDF may have a little junk before its header, which readers allow.
  if (kind === "pdf" && !startsWith(bytes, "%PDF-", 1024)) {
    throw new UploadError("That file is named .pdf but isn't a PDF.", 415);
  }
  if (kind === "docx" && !startsWith(bytes, "PK\u0003\u0004")) {
    throw new UploadError("That file is named .docx but isn't a Word document.", 415);
  }
  // The reader refuses text with NUL bytes in its first 8 KB as binary; refuse it here instead, with a reason.
  if (kind === "text" && bytes.subarray(0, 8192).includes(0)) {
    throw new UploadError("That file isn't plain text (UTF-16 or binary files can't be read).", 415);
  }

  return { bytes, kind, originalName: file.name, mime: mimeFor(file.name, file.type) };
}

/** Writes a prepared file under a new unguessable name and returns what the message stores. Never overwrites. */
export async function saveUpload(upload: PreparedUpload): Promise<Attachment> {
  const directory = uploadsDirectory();
  if (!directory) throw new UploadError(NOT_HERE, 503);

  const fileName = storedFileName(upload.originalName, randomBytes(8).toString("hex"));
  try {
    await mkdir(directory, { recursive: true });
    await writeFile(path.join(directory, fileName), upload.bytes, { flag: "wx" });
  } catch (error) {
    console.error("Saving an attachment failed:", error);
    throw new UploadError("Couldn't save the file. Please try again.", 500);
  }

  return {
    path: `${UPLOADS_FOLDER}/${fileName}`,
    name: displayName(upload.originalName),
    mime: upload.mime,
    size: upload.bytes.length,
  };
}

/** Deletes a saved upload again (its message could not be saved). Best effort: never throws. */
export async function discardUpload(attachment: Attachment): Promise<void> {
  const directory = uploadsDirectory();
  if (!directory) return;
  await unlink(path.join(directory, path.basename(attachment.path))).catch(() => undefined);
}
