"""
Save to Memory: the few facts worth keeping in a document the person attached in the web hub, read
into Jarvis's long-term memory graph (mcp_memory.py) with where they came from.

The hub's chat dock has a "Save to Memory" switch beside an attached file (off by default, so asking
about a file never fills the graph with it). With it on, api_server.py's POST /chat answers the
message as usual and, side by side with that turn, runs ingest() on the file:

    the file's text      mcp_filesystem.extract_text - the same PDF (PyMuPDF), Word (python-docx) and
                         text readers the model's read_file tool uses - on the uploaded file only
                         (attachments.resolve: a path inside uploads/), at most MAX_DOCUMENT_CHARS
    secrets out          API keys, "password: ...", card and ID numbers are blanked before the text
                         leaves the machine (mcp_memory.redact_secrets); an extracted entry that still
                         looks like one is dropped with its facts (mcp_memory.drop_secrets)
    the facts            gpt-4o-mini with a strict rubric (mcp_memory.DOCUMENT_EXTRACTION_PROMPT): keep
                         projects, people and organizations, the stack, deadlines, budgets and key
                         numbers, constraints and agreements, links to "me" and to known projects;
                         ignore boilerplate, definitions, tables of contents, introductions, trivia.
                         Held to MAX_DOCUMENT_FACTS (10) in code, whatever the model returns.
    saved                mcp_memory.save_graph - the same dedup and upserts as remember - with the
                         provenance on every fact: source_file (the file's name) and ingested_at.
                         A newer document's one-value fact (a deadline, a budget, the backend...)
                         archives the old active value: Spec_v2.pdf's deadline replaces Spec_v1.pdf's.

The final event of /chat carries what was saved, per file: {source_file, count, facts: [{subject,
relation, object, status}], ingested_at} - a fact a new value replaced is listed too, archived, with
"replaced": true. Nothing that goes wrong here fails the chat: a file that cannot be read, a model or
database failure, or a document with nothing worth keeping simply adds nothing (saved_memories: []),
and the reason goes to the log - never the document's words.

Runs in api_server.py's process, on its event loop, sharing mcp_memory's connection pool and OpenAI
client with memory_graph.py (the Memory page). Recall (mcp_memory.recall) then shows each fact's
source_file, and tells the model to name the document when it uses one.
"""

from __future__ import annotations

import asyncio
import datetime
import re
from typing import Any, Optional, Protocol, Sequence

import attachments
import mcp_filesystem
import mcp_memory as M

# What the extractor reads of one document: a long one is read from its start (where a spec states
# what the project is, who is in it, and its dates and budget); the rest is left out, and the model is
# told so. ~7-8k tokens of gpt-4o-mini input.
MAX_DOCUMENT_CHARS = 30_000
# One document's reading, extraction, embedding and saving; past this it is given up (the chat waits
# for it, so it is shorter than the web app's 120 s for the whole turn).
INGEST_TIMEOUT_SECONDS = 75.0
# Files per message (the web hub attaches one).
MAX_FILES = 5

_UPLOAD_PREFIX = re.compile(r"^[0-9a-f]{16}-")  # lib/uploads.ts names a stored file <16 hex>-<name>


class AttachmentLike(Protocol):
    path: str
    name: str


class IngestError(Exception):
    """Why a document added nothing to memory: a short sentence for the log."""


def _log(message: str) -> None:
    print(f"[Memory] {message}", flush=True)


def source_name(attachment: AttachmentLike) -> str:
    """
    The document's name as memory keeps it (source_file): the name the person gave the file, made
    safe to quote (attachments.quoted_name), else the stored file's name without its upload prefix.
    """
    stored = str(attachment.path or "").rsplit("/", 1)[-1]
    fallback = _UPLOAD_PREFIX.sub("", stored) or "document"
    return attachments.quoted_name(attachment.name, fallback)[: M.MAX_SOURCE_FILE]


def document_text(attachment: AttachmentLike) -> tuple[str, bool]:
    """
    The text of an uploaded file (the read_file tool's readers) and whether it was cut to
    MAX_DOCUMENT_CHARS. IngestError when it is not a file in uploads/ on this machine, cannot be read,
    or holds no text. Blocking (a PDF is parsed): call it in a thread.
    """
    real = attachments.resolve(attachment.path)
    if real is None:
        raise IngestError("the file is not in uploads/ on this machine")
    try:
        text = mcp_filesystem.extract_text(real, source_name(attachment))
    except mcp_filesystem.FsError as exc:
        raise IngestError(str(exc)) from None
    text = text.strip()
    if not text:
        raise IngestError("the file has no text")
    if len(text) <= MAX_DOCUMENT_CHARS:
        return text, False
    return text[:MAX_DOCUMENT_CHARS] + "\n\n[The rest of the document is not included here.]", True


def _fact(source: str, verb: str, target: str, status: str, *, replaced: bool = False) -> dict[str, Any]:
    fact: dict[str, Any] = {"subject": source, "relation": verb, "object": target, "status": status}
    if replaced:
        fact["replaced"] = True
    return fact


async def ingest_document(
    scope: str,
    attachment: AttachmentLike,
    *,
    now: Optional[datetime.datetime] = None,
) -> Optional[dict[str, Any]]:
    """
    One document into one person's memory (see the module comment): what was saved, or None when the
    document held nothing worth keeping. Raises IngestError / mcp_memory.ToolError on a failure.
    """
    name = source_name(attachment)
    when = now or datetime.datetime.now(datetime.timezone.utc)
    # Not set up (no memory database, no OPENAI_API_KEY): said before the file is even opened.
    M.db_settings()
    M._openai_client()
    text, cut = await asyncio.to_thread(document_text, attachment)
    return await _save_text(scope, name, text, cut, when)


async def _save_text(scope: str, name: str, text: str, cut: bool, when: datetime.datetime) -> Optional[dict[str, Any]]:
    """A document's text (already read) into `scope`'s memory as `name`: the part ingest_document and
    ingest_text share - secrets out, the facts extracted and checked, saved with their provenance."""
    text = M.redact_secrets(text)
    known, facts = await M._known_memory(scope)
    graph = await M.extract_document(text, name, known, facts)
    graph, misshapen = M.drop_misshapen(graph)
    if misshapen:
        _log(f"'{name}': {misshapen} malformed fact{'s' if misshapen != 1 else ''} from the extractor left out")
    graph, dropped = M.drop_secrets(graph)
    graph = M.cap_graph(graph, M.MAX_DOCUMENT_FACTS)
    if not graph.edges:
        _log(f"'{name}': nothing worth keeping was found" + (" (secret-looking facts dropped)" if dropped else ""))
        return None
    saved = await M.save_graph(scope, graph, source_file=name, ingested_at=when)
    stated = [_fact(source, verb, target, status) for source, verb, target, _new, _weight, status, _before in saved.facts]
    replaced = [_fact(source, verb, target, "archived", replaced=True) for source, verb, target in saved.superseded]
    _log(
        f"'{name}': saved {len(stated)} fact{'s' if len(stated) != 1 else ''}"
        + (f", {len(replaced)} older value{'s' if len(replaced) != 1 else ''} archived" if replaced else "")
        + (" (read from the start of a long document)" if cut else "")
    )
    return {
        "source_file": name,
        "count": len(stated),
        "facts": stated + replaced,
        "ingested_at": when.isoformat(),
    }


async def ingest_text(
    scope: str,
    source_file: str,
    text: str,
    *,
    now: Optional[datetime.datetime] = None,
) -> Optional[dict[str, Any]]:
    """
    Text that is not a file in uploads/ - what the vision model read off a photo the owner sent from
    LINE (api_server.py, line_channel.py) - into `scope`'s memory exactly like a document, with
    `source_file` (e.g. "line_image_20261120_093015.jpg") as its provenance. What was saved (the shape
    of one ingest() entry), or None when nothing was: nothing worth keeping, not set up, a failure, or
    INGEST_TIMEOUT_SECONDS passed. Never raises (a cancelled request still cancels it).
    """
    name = attachments.quoted_name(source_file, "image")[: M.MAX_SOURCE_FILE]
    when = now or datetime.datetime.now(datetime.timezone.utc)
    body = str(text or "").strip()
    if not scope or not body:
        return None
    cut = len(body) > MAX_DOCUMENT_CHARS
    if cut:
        body = body[:MAX_DOCUMENT_CHARS] + "\n\n[The rest of the document is not included here.]"
    try:
        M.db_settings()
        M._openai_client()
        result = await asyncio.wait_for(_save_text(scope, name, body, cut, when), INGEST_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        _log(f"'{name}': gave up after {INGEST_TIMEOUT_SECONDS:g} s; nothing more was saved")
        return None
    except M.ToolError as exc:
        _log(f"'{name}': not saved to memory: {exc}")
        return None
    except Exception as exc:  # noqa: BLE001 - a bug here must not cost the person their answer
        _log(f"'{name}': not saved to memory: {type(exc).__name__}: {exc}")
        return None
    return result if result and result["count"] else None


async def ingest(scope: str, files: Sequence[AttachmentLike]) -> list[dict[str, Any]]:
    """
    Every attached file of one message into `scope`'s memory, one at a time: what each saved, files
    that saved nothing left out. Never raises (a cancelled request still cancels it): the chat's answer
    must not depend on this.
    """
    if not scope or not files:
        return []
    saved: list[dict[str, Any]] = []
    for attachment in list(files)[:MAX_FILES]:
        name = source_name(attachment)
        try:
            result = await asyncio.wait_for(ingest_document(scope, attachment), INGEST_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            _log(f"'{name}': gave up after {INGEST_TIMEOUT_SECONDS:g} s; nothing more was saved")
            continue
        except (IngestError, M.ToolError) as exc:
            _log(f"'{name}': not saved to memory: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - a bug here must not cost the person their answer
            _log(f"'{name}': not saved to memory: {type(exc).__name__}: {exc}")
            continue
        if result and result["count"]:
            saved.append(result)
    return saved
