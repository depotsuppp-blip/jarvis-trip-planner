"""
Save to Memory as a background job: an attached document's key facts are read into the person's
memory (memory_ingest.py) AFTER the chat has answered, never while it waits.

Production, 2026-11-21: an 11-page PDF résumé held POST /chat for 47 s while its facts were read and
written, because the reply was only sent once they were saved. Now api_server.py's /chat makes a job
here, hands it to FastAPI's BackgroundTasks - which run once the response has gone out - and answers
at once with the job's id (`ingest_job_id` on the final event). The web hub shows "Saving key facts
from X..." under the reply and asks GET /memory/ingest-status/{job_id} until the job is done, then
shows what was saved ("Saved 6 facts from X").

    create(scope, files, person_names)   a job for one message's files, state "saving"
    run(job_id)                          the work (memory_ingest.ingest): BackgroundTasks calls it
    cancel(job_id)                       the turn failed or was stopped before it answered: nothing
                                         is written (a running save is cancelled and rolls back)
    status(job_id, scope)                what the page shows - only for the scope that made it

States: "saving" (queued or running), "saved" (at least one fact written), "nothing" (read, but
nothing new worth keeping), "failed" (a file could not be read, the model or the database failed, or
JOB_BUDGET_SECONDS passed), "cancelled" (see cancel). The reason for a failure goes to the log only -
never the document's words, and never to the page, which says one fixed sentence per state.

Jobs live in this process's memory: a restarted brain forgets them (the page then says it could not
confirm the save; what was written stays written). A finished job is kept KEEP_SECONDS for the page to
read, and at most MAX_JOBS jobs are kept at all.
"""

from __future__ import annotations

import asyncio
import datetime
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

import memory_ingest

SAVING = "saving"
SAVED = "saved"
NOTHING = "nothing"
FAILED = "failed"
CANCELLED = "cancelled"
STATES = (SAVING, SAVED, NOTHING, FAILED, CANCELLED)

# One job, every file of the message: reading and extracting is at most 40 s a file and writing 15 s
# (memory_ingest), and the web hub attaches one file - this is the ceiling, not the expected time.
JOB_BUDGET_SECONDS = 180.0
# A finished job's answer stays this long for the page to read (it asks every couple of seconds).
KEEP_SECONDS = 3600.0
MAX_JOBS = 200


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


@dataclass
class Job:
    id: str
    scope: str
    files: list[Any]
    # The names memory cites (memory_ingest.source_name), for the page's "Saving key facts from X".
    source_files: list[str]
    # Who "me" is in the documents: the signed-in person's own name (memory_ingest.person_names).
    person_names: list[str]
    state: str = SAVING
    saved: list[dict[str, Any]] = field(default_factory=list)
    created: float = field(default_factory=time.monotonic)
    created_at: str = field(default_factory=_now_iso)
    finished: Optional[float] = None
    finished_at: Optional[str] = None
    task: Optional["asyncio.Task[Any]"] = None


_jobs: dict[str, Job] = {}


def _log(message: str) -> None:
    print(f"[Memory] {message}", flush=True)


def _prune(now: Optional[float] = None) -> None:
    """Forgets finished jobs past KEEP_SECONDS, jobs that never finished past their budget, and the
    oldest beyond MAX_JOBS."""
    clock = time.monotonic() if now is None else now
    for job_id, job in list(_jobs.items()):
        if job.finished is not None and clock - job.finished > KEEP_SECONDS:
            del _jobs[job_id]
        elif job.finished is None and clock - job.created > JOB_BUDGET_SECONDS + KEEP_SECONDS:
            del _jobs[job_id]
    while len(_jobs) > MAX_JOBS:
        oldest = min(_jobs.values(), key=lambda job: job.created)
        del _jobs[oldest.id]


def create(scope: str, files: Sequence[Any], person_names: Sequence[str] = ()) -> Job:
    """A new job for `files` (attachments with .path and .name) into `scope`'s memory, state "saving".
    Nothing runs until run(job.id)."""
    listed = list(files)[: memory_ingest.MAX_FILES]
    job = Job(
        id=secrets.token_urlsafe(16),
        scope=scope,
        files=listed,
        source_files=[memory_ingest.source_name(item) for item in listed],
        person_names=[name for name in person_names if isinstance(name, str) and name.strip()],
    )
    _jobs[job.id] = job
    # After adding: the new job is the newest, so it is never the one MAX_JOBS lets go.
    _prune()
    return job


def _finish(job: Job, state: str, saved: Optional[list[dict[str, Any]]] = None) -> None:
    job.state = state
    job.saved = list(saved or [])
    job.finished = time.monotonic()
    job.finished_at = _now_iso()
    job.task = None


async def run(job_id: str) -> None:
    """
    Does the job (memory_ingest.ingest: read, extract, check, write), at most JOB_BUDGET_SECONDS. Never
    raises but for a cancellation (the server shutting down), which is recorded first. A job that was
    cancelled before it started does nothing.
    """
    job = _jobs.get(job_id)
    if job is None or job.state != SAVING or job.task is not None:
        return
    job.task = asyncio.current_task()
    names = ", ".join(job.source_files) or "the file"
    problems: list[str] = []
    started = time.monotonic()
    try:
        saved = await asyncio.wait_for(
            memory_ingest.ingest(job.scope, job.files, person_names=job.person_names, problems=problems),
            JOB_BUDGET_SECONDS,
        )
    except asyncio.CancelledError:
        _finish(job, CANCELLED)
        _log(f"Save to Memory: {names} was cancelled; nothing more is written.")
        raise
    except asyncio.TimeoutError:
        _finish(job, FAILED)
        _log(f"Save to Memory: {names} gave up after {JOB_BUDGET_SECONDS:g} s; nothing more was saved.")
        return
    except Exception as exc:  # noqa: BLE001 - memory_ingest never raises; a bug here must not leave the job "saving"
        _finish(job, FAILED)
        _log(f"Save to Memory: {names} failed: {type(exc).__name__}: {exc}")
        return
    state = SAVED if saved else FAILED if problems else NOTHING
    _finish(job, state, saved)
    count = sum(int(item.get("count") or 0) for item in saved)
    _log(f"Save to Memory: {names} -> {state} ({count} fact{'s' if count != 1 else ''}) in {time.monotonic() - started:.1f} s.")


def cancel(job_id: str) -> None:
    """The reply never went out (the turn failed or was stopped): the job writes nothing. A job that
    is already running is cancelled - its write, if it had started, rolls back."""
    job = _jobs.get(job_id)
    if job is None or job.state != SAVING:
        return
    if job.task is not None and not job.task.done():
        job.task.cancel()
    _finish(job, CANCELLED)


def view(job: Job) -> dict[str, Any]:
    """The job as GET /memory/ingest-status/{job_id} answers it."""
    return {
        "job_id": job.id,
        "state": job.state,
        "source_files": list(job.source_files),
        # memory_ingest's shape: [{source_file, count, facts: [{subject, relation, object, status, replaced?}], ingested_at}]
        "saved_memories": list(job.saved),
        "count": sum(int(item.get("count") or 0) for item in job.saved),
        "created_at": job.created_at,
        "finished_at": job.finished_at,
    }


def status(job_id: str, scope: str) -> Optional[dict[str, Any]]:
    """The job's view, or None when there is no such job FOR THIS SCOPE (someone else's job is
    exactly as unknown as one that never existed)."""
    _prune()
    job = _jobs.get(str(job_id or ""))
    if job is None or not scope or job.scope != scope:
        return None
    return view(job)
