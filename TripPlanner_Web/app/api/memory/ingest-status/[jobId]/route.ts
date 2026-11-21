import { NextRequest, NextResponse } from "next/server";
import { getSessionUserId } from "@/lib/auth";
import { INGEST_JOB_ID } from "@/lib/jarvis/documentMemory";
import { fetchIngestStatus, MEMORY_SIGNED_OUT, memoryFailure } from "@/lib/jarvisMemory";

// params is a Promise in this Next.js version, not a plain object.
type RouteParams = { params: Promise<{ jobId: string }> };

/**
 * GET /api/memory/ingest-status/<jobId> - how a Save to Memory job of the chat is going: the brain
 * reads an attached file into memory AFTER it has answered (its ingest_jobs.py), and the hub asks
 * this every couple of seconds under the reply until the job is done ("Saving key facts from
 * CV.pdf…" -> "Saved 6 facts from CV.pdf"). A cheap lookup in the brain's own memory - no database.
 * Only the signed-in person's own job: someone else's is a 404, exactly like one that never was.
 *
 * Responses: 200 { state: "saving" | "saved" | "nothing" | "failed", sourceFiles: string[],
 *                  savedMemories: [{ sourceFile, count, facts: [{ subject, relation, object, status, replaced? }] }] },
 *            401 signed out, 404 { code: "not_found" } not theirs, or the brain no longer knows it
 *            (it restarted), 503 { code } the brain is not there.
 */
export async function GET(_request: NextRequest, { params }: RouteParams) {
  const userId = await getSessionUserId();
  if (!userId) return NextResponse.json({ error: MEMORY_SIGNED_OUT }, { status: 401 });

  const { jobId } = await params;
  if (!INGEST_JOB_ID.test(jobId)) {
    return NextResponse.json({ error: "That save is not known.", code: "not_found" }, { status: 404 });
  }
  try {
    const status = await fetchIngestStatus(userId, jobId);
    return NextResponse.json(status, { headers: { "Cache-Control": "private, no-store" } });
  } catch (error) {
    return memoryFailure(error, "GET /api/memory/ingest-status");
  }
}
