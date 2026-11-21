// Tests for Save to Memory's background job as the hub reads it (lib/jarvis/documentMemory.ts):
// the brain answers first and reads the file afterwards (its ingest_jobs.py), so a reply carries the
// job ({ jobId, sourceFiles }) and the page asks how it is going until the facts are saved. Pure: no
// browser, no server.
//
//     npm test

import assert from "node:assert/strict";
import { test } from "node:test";

const {
  documentIngestText,
  INGEST_JOB_ID,
  readDocumentIngest,
  readIngestStart,
  readIngestStatus,
} = await import("../lib/jarvis/documentMemory.ts");

const JOB = "q3JkV0x9_Ab-12cdEf34gh";

test("a job from the brain's final event starts as saving, with the files' names", () => {
  assert.deepEqual(readIngestStart(JOB, ["CV.pdf"]), { jobId: JOB, sourceFiles: ["CV.pdf"], state: "saving" });
  assert.deepEqual(readIngestStart(JOB, "CV.pdf"), { jobId: JOB, sourceFiles: [], state: "saving" }, "names must be a list");
});

test("a job id that is not the brain's shape is no job at all", () => {
  for (const bad of [undefined, null, 42, "", "short", "has space in it", "../../etc/passwd", "x".repeat(65), { id: JOB }]) {
    assert.equal(readIngestStart(bad, ["CV.pdf"]), null, String(bad));
  }
  assert.ok(INGEST_JOB_ID.test(JOB));
});

test("file names are one line and capped", () => {
  const [name] = readIngestStart(JOB, ["CV\n\u0007.pdf", 5, ""]).sourceFiles;
  assert.equal(name, "CV .pdf");
  assert.equal(readIngestStart(JOB, ["a".repeat(500)]).sourceFiles[0].length, 200);
  assert.equal(readIngestStart(JOB, Array.from({ length: 9 }, (_, i) => `f${i}.pdf`)).sourceFiles.length, 5);
});

test("the page re-checks what its server sent", () => {
  assert.deepEqual(readDocumentIngest({ jobId: JOB, sourceFiles: ["CV.pdf"], state: "failed" }).state, "failed");
  assert.deepEqual(readDocumentIngest({ jobId: JOB, sourceFiles: ["CV.pdf"], state: "weird" }).state, "saving");
  assert.equal(readDocumentIngest({ jobId: "nope", sourceFiles: [] }), null);
  assert.equal(readDocumentIngest(null), null);
});

test("the brain's status: saved with facts, saved without drawable facts, the rest", () => {
  const saved = readIngestStatus({
    job_id: JOB,
    state: "saved",
    source_files: ["CV.pdf"],
    saved_memories: [
      { source_file: "CV.pdf", count: 2, facts: [{ subject: "me", relation: "building", object: "Atlas", status: "active" }] },
    ],
  });
  assert.equal(saved.state, "saved");
  assert.deepEqual(saved.savedMemories, [
    { sourceFile: "CV.pdf", count: 2, facts: [{ subject: "me", relation: "building", object: "Atlas", status: "active" }] },
  ]);
  assert.equal(readIngestStatus({ state: "saved", saved_memories: [] }).state, "nothing", "nothing to draw is nothing");
  assert.equal(readIngestStatus({ state: "saving" }).state, "saving");
  assert.equal(readIngestStatus({ state: "nothing" }).state, "nothing");
  assert.equal(readIngestStatus({ state: "cancelled" }).state, "failed");
  assert.equal(readIngestStatus(null).state, "failed");
});

test("the pill's words", () => {
  const job = (state, sourceFiles = ["CV.pdf"]) => ({ jobId: JOB, sourceFiles, state });
  assert.equal(documentIngestText(job("saving")), "Saving key facts from CV.pdf…");
  assert.equal(documentIngestText(job("nothing")), "No new facts to save from CV.pdf");
  assert.equal(documentIngestText(job("failed")), "Couldn't save CV.pdf to memory");
  assert.equal(documentIngestText(job("unknown")), "Couldn't confirm what was saved from CV.pdf");
  assert.equal(documentIngestText(job("saving", ["a.pdf", "b.pdf"])), "Saving key facts from 2 files…");
  assert.equal(documentIngestText(job("saving", [])), "Saving key facts from the file…");
});
