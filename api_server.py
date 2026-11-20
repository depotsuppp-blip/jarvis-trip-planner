"""
HTTP front for the brain, for the Trip Planner web app's Jarvis hub.

    POST /chat   { "prompt": "...", "history": [{ "role": "user"|"jarvis", "content": "...",
                                                  "attachments": [...] }],
                   "user_id": "...", "app_url": "...", "speak": false, "interactive": true,
                   "attachments": [{ "path": "uploads/...", "name": "report.pdf",
                                     "mime": "application/pdf", "size": 48213 }] }
              -> text/event-stream: server-sent events as the brain's state machine works
                 (llm_brain.py, THE STATE MACHINE), each `event: <type>` + `data: <JSON>`:

                   progress  {"type": "progress", "node": "Delegate_to_Agent",
                              "status": "Delegating to Head of Analytics..."}
                             as each step starts; "node" is left out of the finer steps a
                             specialist agent reports ("Head of Research: Searching the web...")
                   final     {"type": "final", "node": "Final_Response", "status": "Done",
                              "message": "...", "chat_text": "...",
                              "spoken_audio_text": "..." | null, "audio": null,
                              "memory_events": [{"action": "saved"|"confirmed"|"failed",
                                                 "entries": 2, "facts": 3, "merged": 0,
                                                 "summary": "Nok sister of me; ..."}],
                              "saved_memories": [{"source_file": "Spec_v2.pdf", "count": 6,
                                                  "facts": [{"subject": "Atlas", "relation":
                                                             "deadline_is", "object": "2027-03-01",
                                                             "status": "active"}, ...],
                                                  "ingested_at": "2026-11-20T09:15:02+00:00"}]}
                             once, last
                   error     {"type": "error", "status": "Failed", "message": "...",
                              "code": 502}                    instead of final, last
                   action_required  {"type": "action_required", "approval_id": "...",
                              "tool": "send_line_notification", "prompt": "Do you authorize ...?",
                              "details": [{"name": "message", "value": "..."}], "expires_in": 60}
                             a confirmation-gated tool is WAITING for the person's Allow or Deny
                             (approvals.py); answer with POST /approvals/{approval_id}
                   action_resolved  {"type": "action_resolved", "approval_id": "...",
                              "decision": "allow"|"deny"|"timeout"}   the card is closed
                 plus a ": keep-alive" comment line after 10 s without an event.

                 memory_events is what the memory tool REALLY wrote during the turn, one per
                 remember call (mcp_client.py records them; the model's words count for nothing):
                 saved (entries = new or updated entries, facts = new or changed facts), confirmed
                 (all already known) or failed. Empty when nothing was remembered. The page shows
                 it under the reply, so a "got it" without a save is visible as such.
                 message (= chat_text) is what the chat shows, in any language: a plain
                 sentence, or - when a tool showed the person a widget - a JSON string, see
                 rich_reply.py; a picture or a file Jarvis made is in it as its Markdown line
                 (see GET /artifacts). spoken_audio_text is what the voice says, ENGLISH
                 ALWAYS - the reply when it is already short English, else the brain's
                 English translation and summary of it, at most 200 characters - and null
                 unless "speak" was true. audio is that text voiced, {"mime": "audio/mpeg",
                 "base64": "..."}, when the voice worked (reply_audio.py, Edge-TTS, English
                 voice); otherwise null and the reply is just text. The voice never reads
                 chat_text itself. A provider failure is not
                 an error event: it is a final message saying what went wrong, as before.
                 Before the stream opens, the usual HTTP errors: 422 a bad body, 503 no model.

                 STOP. Closing the connection stops the turn (a watcher checks every 0.25 s):
                 the model request in flight is closed, no further tool starts, a specialist
                 agent ends after its current step, nothing is kept
                 in the brain's history, and the brain is free for the next message at once.
                 attachments are files the person attached in the web hub, which the web
                 app has already saved in this folder's uploads/ (path is relative to this
                 folder). The model is not sent the file: a line is added after the prompt
                 saying where it is and which tool reads it (attachments.py), and the model
                 reads it with the filesystem tool server. prompt may be "" when a file
                 came with no words. User turns in history carry theirs the same way.
                 save_to_memory (default false) is the hub's Save to Memory switch on an
                 attached file: the file's key facts - at most 10: projects, people, the stack,
                 deadlines, budgets, constraints - are also read into the signed-in person's
                 memory with the file's name on each (memory_ingest.py), while the message is
                 answered as usual. saved_memories says what was saved, per file; a fact a newer
                 document's value replaced is listed as "archived" with "replaced": true. It is []
                 when the switch was off, there was no user_id, or nothing was worth keeping -
                 a failure there never fails the reply. The facts are prepared beside the turn and
                 written only once the reply is ready (a failed or stopped turn writes nothing),
                 and never past SAVE_BUDGET_SECONDS (100 s) from the start of the request.
    POST /voice  multipart/form-data: audio (the file the browser recorded), and optionally
                   history (a JSON list shaped like /chat's), user_id, app_url, speak
              -> { "transcript": "...", "reply": "...", "engine": "openai"|"local",
                   "spoken_audio_text": "..." | null, "audio": null }
                 Speech-to-text (stt.py: OpenAI Whisper with OPENAI_API_KEY, otherwise
                 faster-whisper on this machine) first, then the transcript is asked
                 exactly as a typed /chat prompt would be - the same state machine,
                 collected into this one JSON answer, and stopped the same way when the
                 connection closes. 422 when nothing was heard.
    POST /approvals/{approval_id}  { "user_id": "...", "decision": "allow"|"deny" }
              -> { "ok": true, "decision": "allow" }   the person's answer to an action_required
                 card; the waiting run resumes at once. 404 when the card is gone (answered, timed
                 out, the turn stopped), 403 when it was sent to a different user_id.
    GET  /health -> { "ok": true, "provider": "...", "model": "...",
                      "stt": "openai"|"local", "stt_ready": true|false,
                      "tts": true|false, "mcp_tools": ["mcp__web_search__web_search", ...],
                      "mcp_servers": [{ "name": "web_search", "connected": true,
                                        "tools": ["web_search"], "error": "" }, ...],
                      "manager": { "provider": "gemini", "model": "gemini-flash-lite-latest",
                                   "on_standby": true,
                                   "primary": { "provider": "anthropic", "model": "claude-sonnet-5",
                                                "state": "resting", "last_error": "billing",
                                                "back_in": 512 } },
                      "agents": [{ "name": "data", "title": "Head of Data & Documents",
                                   "tool": "delegate_to_data", "active": true,
                                   "routes": [{ "role": "primary", "provider": "gemini",
                                                "model": "gemini-pro-latest", "state": "ready" }, ...],
                                   "tools": ["mcp__local_fs__read_file", ...] }, ...] }
                 stt_ready is false while the on-device model is still loading. tts says whether
                 replies can be spoken (Edge-TTS installed and not switched off). mcp_tools are
                 the tool-server tools Jarvis had at its last turn, the manager's and its agents'
                 (agents/); manager is the brain's own route (LLMBrain.manager_status: on the
                 Gemini standby or not, and why); agents is the team - a route's state is ready, resting (failed lately:
                 out of credit or quota, rate limited), failed (until restart) or not configured
                 (no key: "why" says which). heads is the rest of what Jarvis can do, none of it an LLM
                 agent: [{ "name": "creative"|"publishing"|"analytics"|"classroom", "title": "Head of
                 Creative", "tools": ["delegate_to_creative"], "state": "ready"|"not configured"|"off",
                 "why": "needs OPENAI_API_KEY", "owner_only": false }, ...] (LLMBrain.heads_status; Google
                 Classroom is owner_only). mcp_servers is each tool
                 server as it is right now (error says why one is not connected). The bundled
                 servers - local_fs, web_search, fetch, memory, data - start with the brain, with
                 nothing to configure (mcp_client.py). The web hub's sidebar lists them from here
                 (TripPlanner_Web: GET /api/chat/tools).
    GET  /artifacts/<file> -> the file itself: what Jarvis made and saved in artifacts/ - the
                 images from agents/creative_agent.py (image_YYYYMMDD_HHMMSS.png) and the
                 presentations from agents/publishing_agent.py (presentation_YYYYMMDD_HHMMSS.pptx)
                 and the spreadsheets from agents/analytics_agent.py
                 (spreadsheet_YYYYMMDD_HHMMSS.xlsx).
                 A web chat reply shows a picture as ![Generated Image](/artifacts/<file>) and
                 offers a presentation as [Download Presentation](/artifacts/<file>) (a spreadsheet
                 as [Download Spreadsheet](/artifacts/<file>)); the web app
                 answers that address for the browser by fetching it from here
                 (TripPlanner_Web/app/artifacts/[name]/route.ts), for the signed-in person
                 whose chat it is in. 404 for a file that is not there.

    The Memory page (TripPlanner_Web /jarvis/memory) - Jarvis's long-term memory of one
    person, the knowledge graph mcp_memory.py keeps, to look at and correct (memory_graph.py).
    Every request names the signed-in person in the header X-Jarvis-User-Id (TripPlanner_Web's
    User.id; JARVIS_OWNER_USER_ID's is the owner, whose memory is also the voice's - see
    mcp_client.web_scope) and only ever reaches that person's memory: someone else's entry or
    relation is a 404, exactly like one that does not exist. Errors are { "code", "detail" }.
    GET    /memory/graph -> { "nodes": [{ "id", "name", "type", "description" }],
                              "links": [{ "id", "source", "target", "relation_type", "weight",
                                          "status", "updated_at" }] }
                 type is one of person, project, technology, organization, place, other (older
                 entries may still carry a type from before that set); status is active, completed
                 or archived; updated_at is an ISO 8601 timestamp.
                 source and target are node ids. Empty when nothing has been remembered yet
                 (the tables are never created from here).
    PATCH  /memory/entities/{entity_id}  { "name"?, "type"?, "description"? }
              -> { "node": {...}, "reembedded": true }   when the name, type or description
                 changed, the entry is embedded again (text-embedding-3-small) so recall finds it
                 by its new words; nothing is saved if that fails (503 embedding_failed). 409
                 name_taken, 400 invalid / me_locked ("me" keeps its name) / reserved / refused (a
                 secret).
    DELETE /memory/entities/{entity_id} -> { "deleted": id, "links": [ids of its relations, deleted too] }
    PATCH  /memory/relations/{relation_id}  { "status": "active"|"completed"|"archived" }
              -> { "link": {...} }   the fact is marked done or no longer true by hand (its date moves).
    DELETE /memory/relations/{relation_id} -> { "deleted": id }   both entries stay.
                 401 no_user without the header; 503 not_configured / unavailable when the memory
                 database is not set up or cannot be reached.

    GET    /system/topology -> { "generated_at", "viewer": { "owner", "hidden" }, "orchestrator",
                                 "providers": [...], "telemetry": { "status": "optimal"|"degraded",
                                 "active_providers", "total_providers", "total_capabilities" } }
                 The System Topology page (TripPlanner_Web /jarvis/system): Jarvis, its agents, its
                 tool servers and its own tools, each with its live status - online, auth_required
                 (a Google sign-in lacks a scope, a key was refused) or offline - its tools and a few
                 starter prompts (system_topology.py). Same X-Jarvis-User-Id header as /memory:
                 401 no_user without it; the owner-only parts are left out for anyone but the owner.

    POST /webhooks/line  the LINE Official Account's Messaging API webhook: the owner chats with Jarvis
                 from LINE on their phone - the same brain, tools and memory as the web hub, and the
                 same transcript: LINE turns are kept in a "LINE" session of the web app (chat_sync.py),
                 which /jarvis lists and opens like any other. LINE's side is line_channel.py.
              -> 200 {"ok": true} at once, before anything is answered (LINE gives up on a slow
                 webhook and retries it); the messages are then answered as a background task, one at
                 a time, in order. 400 no X-Line-Signature (or a body that is not JSON), 401 a signature
                 that is not LINE's (HMAC-SHA256 of the raw body with LINE_CHANNEL_SECRET), 413 a body
                 over 1 MB, 503 no LINE_CHANNEL_SECRET. Events from anyone but the owner's own 1:1
                 chat (LINE_TARGET_USER_ID, or LINE_USER_ID) are ignored, still with 200.
                 A text is asked like a typed /chat prompt; a photo is fetched from LINE's content API
                 and read by a vision model, and Jarvis is asked about what it shows - a document or a
                 whiteboard, or any photo after "save this", also goes into memory as
                 line_image_<YYYYmmdd_HHMMSS>.jpg (memory_ingest.ingest_text). While a turn runs LINE
                 shows its loading animation; the answer goes back as a reply (falling back to a push
                 when the reply token has expired) in plain-text bubbles of at most 5000 characters.
                 Tools that need the owner's approval are declined, as for /voice: LINE has no
                 Approval Card. The owner's memory is "owner", whoever JARVIS_OWNER_USER_ID is.
                 EXPOSURE: LINE must reach this from the internet, through a tunnel (Cloudflare Tunnel,
                 ngrok). With LINE_CHANNEL_SECRET set, a second listener on 127.0.0.1:8001
                 (JARVIS_LINE_PORT; 0 turns it off) serves ONLY this path - point the tunnel at that
                 port, never at 8000. And here on 8000, a request that came through a proxy or tunnel
                 (X-Forwarded-For, Forwarded, CF-Connecting-IP and the like) gets 404 for every path
                 but this one (_TunnelGuard; JARVIS_ALLOW_PROXIED=1 lifts that).

Stateless by design: the web app's database is the source of truth for the
transcript, so every request carries its own history and replaces whatever
the brain remembered from the last one (LLMBrain.set_history). `history`
is earlier turns only, oldest first, NOT including `prompt`.

`user_id` (the signed-in person, TripPlanner_Web's User.id) and `app_url`
(the web app's own origin) are optional and describe the CALLER, not the
question: they are made available to the tools for the length of the request
(request_context.py) and are what lets "plan a trip to Chiang Mai" create the
trip for that person on that instance and answer with its link. A request
without them is still a web chat - links are allowed in the reply - it just
cannot plan a trip for anyone in particular.

Run:  python api_server.py      (127.0.0.1:8000, local only)

The brain, and with it the bundled MCP tool servers, is started when the server starts
(a background thread), not on the first message, so the first person to ask does not
wait for them.

Things that differ from the voice assistant, deliberately:

* One LLMBrain, one turn at a time. The brain keeps its history per turn
  and is not thread-safe, so turns queue on an asyncio lock instead of
  interleaving (a waiting turn streams "Waiting for the previous request to
  finish..."). Each turn runs as its own task on the event loop: model calls
  are awaited on the providers' async clients and tools run on worker
  threads (LLMBrain.run), so a stopped turn lets go of the lock at once.
* Tools that ask for spoken confirmation (n8n webhooks, the manager's own
  send_line_notification, starting/stopping servers, creating tools) are put
  to the person on the page. There is no microphone here, so /chat streams
  {"type": "action_required", "approval_id", "tool", "prompt", "details",
  "expires_in"} and the run WAITS inside that tool call (approvals.py) while the
  web app shows an Approval Card; the person's Allow or Deny comes back through
  POST /approvals/{approval_id} and the run resumes - the tool runs, or the model
  is told it was declined and says so. An {"type": "action_resolved", ...} event
  closes the card. A card nobody answers (JARVIS_APPROVAL_TIMEOUT, 60 s), Stop, or a
  closed page is a denial.
  A web request must still never be able to trigger an outward side effect or run
  new code on this machine by itself, and the web app serves everyone who signs in:
  so only the OWNER (the user named in JARVIS_OWNER_USER_ID) is ever asked, and only
  the same user can answer the card. For anyone else - and for /voice, which has no
  page to show a card on - these tools are DECLINED, as before. Without
  JARVIS_OWNER_USER_ID set, nobody is the owner and every one is declined.
  The one exception is the Head of Productivity's push_line_notification
  (agents/productivity_agent.py): it asks no one, because it can only
  message the owner's own LINE (LINE_TARGET_USER_ID) and sending the daily
  briefing there is its job - and it is capped per task.
* Bound to loopback with no authentication: anything on this machine can
  call it, so do not expose the port - the LINE webhook has its own listener
  for that (see POST /webhooks/line). Like /chat's user_id, the /memory
  endpoints' X-Jarvis-User-Id is taken as given - the web app sets it from the
  session. A browser page cannot send it here: CORS allows only GET and POST
  with Content-Type, so a page on another origin cannot add the header or
  PATCH / DELETE at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import json
import mimetypes
import os
import socket
import threading
from collections import deque
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any, AsyncIterator, Literal, Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

import approvals
import attachments
import chat_sync
import line_channel
import mcp_client
import memory_graph
import memory_ingest
import reply_audio
import request_context
import rich_reply
import stt
import system_topology
from agents.analytics_agent import XLSX_MIME
from agents.creative_agent import ARTIFACTS_DIR, ARTIFACTS_URL_PATH
from agents.publishing_agent import PPTX_MIME
from llm_brain import LLMBrain

HOST = "127.0.0.1"
PORT = 8000
# Next.js calls this server-to-server, where CORS does not apply; the
# middleware is for a browser calling it directly during development.
ALLOWED_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000"]
MAX_PROMPT_CHARS = 4000
MAX_HISTORY_TURNS = 100
# A minute of speech is well under 1 MB in any browser's codec; this is headroom, not a target.
MAX_AUDIO_BYTES = 10 * 1024 * 1024
# Per message. The web hub attaches one; the limit is headroom.
MAX_ATTACHMENTS = 5
# A streamed turn sends a comment line after this long without an event, so nothing in between
# takes a long tool call for a dead connection.
HEARTBEAT_SECONDS = 10.0
# Save to Memory may hold a /chat reply back at most this long from the start of the request: the web
# app gives up at 120 s (TripPlanner_Web lib/jarvisBrain.ts, BRAIN_TIMEOUT_MS), and a reply must never
# be lost to facts that are still being read. What is not done by then is cancelled and nothing is
# written (production, 2026-11-20: a 315 KB PDF took the whole request past 120 s).
SAVE_BUDGET_SECONDS = 100.0
# How often a turn checks that its client is still there (and stops it when it is not).
DISCONNECT_POLL_SECONDS = 0.25


class Attachment(BaseModel):
    """A file the web app saved in uploads/ for this message. Only `path` is acted on; see attachments.py."""

    path: str = Field(max_length=300)
    name: str = Field(default="", max_length=300)
    mime: str = Field(default="", max_length=200)
    size: int = Field(default=0, ge=0)


class HistoryTurn(BaseModel):
    role: Literal["user", "jarvis", "assistant"]
    content: str = Field(max_length=MAX_PROMPT_CHARS * 2)
    attachments: list[Attachment] = Field(default_factory=list, max_length=MAX_ATTACHMENTS)


class ChatRequest(BaseModel):
    # May be empty only when a file came with it: the person sent just the file.
    prompt: str = Field(default="", max_length=MAX_PROMPT_CHARS)
    history: list[HistoryTurn] = Field(default_factory=list, max_length=MAX_HISTORY_TURNS)
    user_id: str = Field(default="", max_length=128)
    app_url: str = Field(default="", max_length=300)
    # The person has voice replies on: also answer with audio when the reply is short enough.
    speak: bool = False
    # The caller can show an Approval Card and send the answer back (POST /approvals/{id}). False for
    # a caller that cannot: confirmation-gated tools are then declined at once instead of after the wait.
    interactive: bool = True
    attachments: list[Attachment] = Field(default_factory=list, max_length=MAX_ATTACHMENTS)
    # Save to Memory: the person switched it on for the attached file, so its key facts also go into
    # their long-term memory (memory_ingest.py). Off by default: asking about a file never fills it.
    save_to_memory: bool = False

    @model_validator(mode="after")
    def _says_something(self) -> "ChatRequest":
        if not self.prompt and not self.attachments:
            raise ValueError("prompt is required (or attach a file).")
        return self


class ReplyAudio(BaseModel):
    mime: str
    base64: str


class VoiceResponse(BaseModel):
    transcript: str
    reply: str
    # Who did the listening: "openai" (the Whisper API) or "local" (faster-whisper on this machine).
    engine: str
    # What the voice says, in English, and its audio: see POST /chat's final event.
    spoken_audio_text: Optional[str] = None
    audio: Optional[ReplyAudio] = None
    # What the memory tool really wrote during this turn: see POST /chat's final event.
    memory_events: list[dict[str, Any]] = Field(default_factory=list)


_history_turns = TypeAdapter(Annotated[list[HistoryTurn], Field(max_length=MAX_HISTORY_TURNS)])


class ApprovalDecision(BaseModel):
    """The person's answer to an Approval Card (POST /approvals/{approval_id})."""

    # TripPlanner_Web's User.id of the signed-in person answering: the card is only theirs.
    user_id: str = Field(min_length=1, max_length=128)
    decision: Literal["allow", "deny"]


def _warm_brain() -> None:
    """Builds the brain now, which also starts the bundled MCP tool servers (filesystem, web search)."""
    try:
        _get_brain()
    except Exception as exc:  # noqa: BLE001 - the first request will try again and report it
        print(f"[API] Starting the brain failed: {type(exc).__name__}: {exc}")


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # The on-device speech model takes seconds to load: start that now, on its own
    # thread (importing it is slow too), so the first Talk press does not wait for it
    # and the server is not held back from answering. A no-op when OPENAI_API_KEY is set.
    threading.Thread(target=stt.warm_up, name="jarvis-stt-warmup", daemon=True).start()
    # Same idea for the brain: its MCP tool servers take a few seconds to start.
    threading.Thread(target=_warm_brain, name="jarvis-brain-warmup", daemon=True).start()
    # The LINE webhook's own listener, the one port a tunnel may point at (POST /webhooks/line).
    listener = await _start_line_listener()
    try:
        yield
    finally:
        if listener is not None:
            await _stop_line_listener(*listener)
        await _chat_store.close()
        await memory_graph.close()


# Request headers a proxy or tunnel adds (Cloudflare Tunnel, ngrok, Tailscale Funnel, nginx...); a
# request on this machine's loopback from the web app or a script carries none of them.
_PROXY_HEADERS = frozenset({
    b"x-forwarded-for", b"x-forwarded-host", b"forwarded", b"x-real-ip", b"true-client-ip",
    b"cf-connecting-ip", b"cf-ray", b"tailscale-funnel-request",
})


class _TunnelGuard:
    """
    Plain ASGI (no BaseHTTPMiddleware, which would get in the way of /chat's stream and its Stop): a
    request that came through a proxy or tunnel may reach POST /webhooks/line and nothing else. The rest
    of this server has no authentication (see the top of this file), so a tunnel pointed at port 8000
    by mistake must not hand the internet the chat, the memory or the files. JARVIS_ALLOW_PROXIED=1
    lifts it, for a setup that really does put a proxy in front of the brain.
    """

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if (
            scope.get("type") == "http"
            and scope.get("path") != line_channel.WEBHOOK_PATH
            and os.environ.get("JARVIS_ALLOW_PROXIED", "").strip() != "1"
            and any(name in _PROXY_HEADERS or name.startswith(b"ngrok-") for name, _ in scope.get("headers") or [])
        ):
            await JSONResponse(status_code=404, content={"detail": "Not Found"})(scope, receive, send)
            return
        await self.inner(scope, receive, send)


app = FastAPI(title="Jarvis brain", docs_url=None, redoc_url=None, lifespan=_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)
app.add_middleware(_TunnelGuard)

# GET /artifacts/<file>: the folder Jarvis saves what it makes in (agents/creative_agent.py,
# agents/publishing_agent.py, agents/analytics_agent.py). StaticFiles keeps requests inside it (no ../), and answers 404
# for anything missing. The folder is made here when it is not there yet - a fresh clone has
# it, a Docker volume may not - and a folder that cannot be made only costs the files, never
# the server's start.
try:
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
except OSError as exc:
    print(f"[API] The artifacts folder {ARTIFACTS_DIR} could not be made ({exc}); its files will not be served.")
# StaticFiles names a file's type from Python's mimetypes table, which knows .pptx and .xlsx
# only where the system does (Windows' registry): the Linux image would send presentations and
# spreadsheets as application/octet-stream, and the web app refuses a file whose type is not the
# one its name says. With the right type the browser saves the file instead of failing on it.
mimetypes.add_type(PPTX_MIME, ".pptx")
mimetypes.add_type(XLSX_MIME, ".xlsx")
app.mount(ARTIFACTS_URL_PATH, StaticFiles(directory=ARTIFACTS_DIR, check_dir=False), name="artifacts")

_brain: LLMBrain | None = None
# One turn at a time: the brain keeps per-turn state (its history) and is not thread-safe.
# An asyncio lock, held by the turn's task on the event loop - a stopped turn lets go of it
# the moment it is cancelled.
_turn_lock = asyncio.Lock()
# Held while the brain is being built, so the start-up thread and a request that arrives
# meanwhile share one brain (two would start the MCP tool servers twice).
_build_lock = threading.Lock()


def _get_brain() -> LLMBrain:
    """Built once - at start-up on a background thread, or on first use. Importing this module stays cheap."""
    global _brain
    with _build_lock:
        if _brain is None:
            _brain = LLMBrain(confirm=approvals.confirm)
        return _brain


@app.get("/health")
def health() -> dict:
    brain = _get_brain()
    return {
        "ok": brain.available,
        "provider": brain.provider_name,
        "model": brain.model,
        "stt": stt.engine(),
        "stt_ready": stt.is_ready(),
        "tts": reply_audio.enabled(),
        # Every tool-server tool Jarvis has, whether the manager or one of its agents holds it.
        "mcp_tools": [name for name in brain.all_tool_names if name.startswith("mcp__")],
        "mcp_servers": brain.mcp_servers(),
        "manager": brain.manager_status(),
        "agents": brain.team_status(),
        # The capabilities that are plain Python behind a delegate_to_* tool, not LLM agents.
        "heads": brain.heads_status(),
    }


def _prepare(
    brain: LLMBrain,
    prompt: str,
    history: list[HistoryTurn],
    files: list[Attachment] | None,
    saving: bool = False,
) -> tuple[str, list[tuple[str, str]]]:
    """
    The prompt as the brain is asked it, and the history as (role, text) pairs. Attached
    files are pointed at, not pasted in: a line after the words says where each file is and
    which tool reads it (attachments.py). Built while the turn holds the brain, because it
    asks the brain which tools it has right now. A .csv/.tsv/.json/.jsonl file is pointed at
    the data tool (load it into a table, answer with SQL) when that server is connected.
    `saving`: Save to Memory is reading the files into memory on the side (memory_ingest.py), and
    the model is told not to remember them itself.
    """
    attached = bool(files) or any(t.attachments for t in history)
    tool = attachments.read_tool(brain.tool_names) if attached else None
    data = attachments.data_tool(brain.tool_names) if attached else None
    if files:
        print(f"[API] Attachments: {', '.join(f.path for f in files)} (reader: {tool or 'none'}, data: {data or 'none'}).")
        prompt = attachments.with_context(prompt, files, tool, current=True, data=data)
        if saving:
            prompt = f"{prompt}\n\n{attachments.SAVING_NOTE}"
    # A reply that carried widgets is stored as JSON; the model remembers it as the sentence it said.
    turns = [
        (
            "user",
            attachments.with_context(t.content, t.attachments, tool, data=data) if t.attachments else t.content,
        )
        if t.role == "user"
        else ("assistant", rich_reply.history_text(t.content))
        for t in history
    ]
    return prompt, turns


async def _turn(
    request: Any,
    brain: LLMBrain,
    prompt: str,
    history: list[HistoryTurn],
    user_id: str,
    app_url: str,
    speak: bool = False,
    files: list[Attachment] | None = None,
    interactive: bool = False,
    saving: bool = False,
    channel: str = request_context.CHANNEL_WEB,
) -> AsyncIterator[dict[str, Any]]:
    """
    One turn through the brain's state machine (LLMBrain.run), as /chat and /voice both
    ask it: its events as they happen - progress, then one final (or error) - with a
    {"type": "heartbeat"} after every HEARTBEAT_SECONDS of quiet.

    The turn runs as a task of its own, holding the brain (one turn at a time); a turn
    that has to wait for another says so. A watcher asks the server every
    DISCONNECT_POLL_SECONDS whether the client is still there, and when it is not - the
    person pressed Stop, the web app gave up - the turn is cancelled on the spot: the
    model request in flight is closed, the run's worker threads stop at their next step,
    nothing is added to the history, and the brain is free for the next message at once.
    Closing this generator early (a streamed response the server cancelled) does the same.

    `interactive`: the client is a page that can answer the run (/chat). Its tools may then
    stream an {"type": "action_required"} event and wait for the person's Allow or Deny
    (approvals.py); /voice, which collects one JSON answer, is not, and those tools are declined.
    `saving`: the attached files are being saved to memory on the side (see _prepare).
    `request` is the client's Request, or anything with an async is_disconnected() (a LINE turn has
    no connection to watch); `channel` says who asks - the web hub, or the owner on LINE.
    """
    caller = request_context.RequestContext(
        channel=channel,
        user_id=user_id.strip(),
        app_url=app_url.strip(),
        # Only asked to keep answers short when they can actually be spoken.
        voice_reply=speak and reply_audio.enabled(),
    )
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Any] = asyncio.Queue()
    cancel = threading.Event()
    end = object()

    def push(event: Any) -> None:
        # From the loop and from the run's worker threads alike, in the order they happen.
        try:
            loop.call_soon_threadsafe(queue.put_nowait, event)
        except RuntimeError:  # the loop is closing: nobody is left to read it
            pass

    def progress(text: str) -> None:
        push({"type": "progress", "status": text})

    async def drive() -> None:
        try:
            if _turn_lock.locked():
                progress("Waiting for the previous request to finish...")
            async with _turn_lock:
                question, turns = _prepare(brain, prompt, history, files, saving)
                brain.set_history(turns)
                try:
                    # The context is current only while this request is being answered, on
                    # this task and the worker threads it starts - see request_context.py.
                    with request_context.use(caller):
                        async for event in brain.run(
                            question,
                            cancel=cancel,
                            progress=progress,
                            emit=push if interactive else None,
                            # The English spoken version is made only when it will be voiced.
                            speak=caller.voice_reply,
                        ):
                            if event.get("type") == "final":
                                # What the memory tool really wrote this turn (mcp_client.py), not what the model says.
                                event = {**event, "memory_events": list(caller.memory_events)}
                            push(event)
                finally:
                    # Nothing is carried to the next request; the caller owns the transcript.
                    brain.set_history([])
        except Exception as exc:  # noqa: BLE001 - a fault in the brain itself; provider and tool failures are final messages
            print(f"[API] The turn failed: {type(exc).__name__}: {exc}")
            push({"type": "error", "status": "Failed", "message": "Jarvis hit an internal error.", "code": 500})
        finally:
            push(end)

    task = asyncio.create_task(drive(), name="jarvis-turn")

    async def watch() -> None:
        while not task.done():
            if await request.is_disconnected():
                print("[API] The client went away; stopping its turn.")
                cancel.set()
                task.cancel()
                return
            await asyncio.sleep(DISCONNECT_POLL_SECONDS)

    watcher = asyncio.create_task(watch(), name="jarvis-turn-watch")
    getter: Optional[asyncio.Future[Any]] = None
    answered = False
    try:
        while True:
            if getter is None:
                getter = asyncio.ensure_future(queue.get())
            done, _ = await asyncio.wait({getter}, timeout=HEARTBEAT_SECONDS)
            if not done:
                yield {"type": "heartbeat"}
                continue
            event, getter = getter.result(), None
            if event is end:
                return
            answered = answered or event.get("type") == "final"
            yield event
    finally:
        if getter is not None:
            getter.cancel()
        watcher.cancel()
        # A turn that has answered is only letting go of the brain: it is left to finish that.
        if not task.done() and not answered:
            cancel.set()
            task.cancel()


def _sse(event: dict[str, Any]) -> str:
    """One server-sent event: `event: <type>` and its JSON as `data:`; a heartbeat is a comment line."""
    if event.get("type") == "heartbeat":
        return ": keep-alive\n\n"
    return f"event: {event.get('type', 'message')}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


def _audio_for(spoken: str, speak: bool) -> Optional[ReplyAudio]:
    """
    `spoken` - the brain's English spoken text, never the chat reply - voiced, or None: not
    asked for, nothing to say, or the voice is unavailable. Called once the reply is out of the
    brain - the network round trip to the voice must not hold up anyone else's message - and it
    never raises, so a voice failure leaves the person with the text.
    """
    if not speak or not spoken:
        return None
    try:
        payload = reply_audio.for_speech(spoken)
        return ReplyAudio(**payload) if payload else None
    except Exception as exc:  # noqa: BLE001 - audio is an extra; a bug in it must not cost the person their answer
        print(f"[API] Spoken reply failed: {type(exc).__name__}: {exc}")
        return None


async def _ready_brain() -> LLMBrain:
    """The brain, built if it is not yet - off the event loop: the start-up build may still be running."""
    brain = await asyncio.to_thread(_get_brain)
    if not brain.available:
        raise HTTPException(status_code=503, detail="No language model is configured (see .env).")
    return brain


@app.post("/chat")
async def chat(body: ChatRequest, request: Request) -> StreamingResponse:
    """
    One typed turn, answered as server-sent events (see the top of this file). Errors found
    before the turn starts are plain HTTP errors (422, 503); once the stream is open, a turn
    that fails says so in an `error` event.

    With save_to_memory and a file, the file's key facts are read into the person's memory
    (memory_ingest.py) in two steps: prepared (read, extracted, checked) side by side with the
    turn, then written only once the reply is ready - a turn that fails, is stopped or runs out of
    time writes nothing. The final event says what was saved (saved_memories). That never fails or
    holds up the turn past SAVE_BUDGET_SECONDS from the start of the request: what is not done by
    then is cancelled, and the reply goes out with saved_memories [].
    """
    brain = await _ready_brain()
    # Whose memory: the signed-in person's (mcp_client.web_scope); nobody's without a user_id.
    scope = mcp_client.web_scope(body.user_id) if body.save_to_memory and body.attachments else ""

    async def stream() -> AsyncIterator[str]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SAVE_BUDGET_SECONDS
        preparing: Optional[asyncio.Task[list[memory_ingest.Prepared]]] = None
        saving: Optional[asyncio.Task[list[dict[str, Any]]]] = None
        if scope:
            preparing = asyncio.create_task(memory_ingest.prepare(scope, body.attachments), name="jarvis-save-to-memory")
        try:
            async with aclosing(
                _turn(
                    request, brain, body.prompt, body.history, body.user_id, body.app_url, body.speak, body.attachments,
                    interactive=body.interactive, saving=preparing is not None,
                )
            ) as events:
                async for event in events:
                    kind = event.get("type")
                    if kind == "cancelled":
                        return
                    if kind != "final":
                        yield _sse(event)
                        if kind == "error":
                            return
                        continue
                    reply = str(event.get("message") or "").strip()
                    if not reply:
                        yield _sse({"type": "error", "status": "Failed", "message": "The brain returned an empty reply.", "code": 502})
                        return
                    # Only the English spoken text goes to the voice; the chat gets the reply as it is.
                    spoken = str(event.get("spoken") or "").strip() if body.speak else ""
                    if spoken and reply_audio.enabled():
                        yield _sse({"type": "progress", "status": "Recording the spoken reply..."})
                    audio = await asyncio.to_thread(_audio_for, spoken, body.speak)
                    saved: list[dict[str, Any]] = []
                    if preparing is not None:
                        # The reply is ready: the facts (prepared meanwhile) are written now, inside the budget.
                        name = memory_ingest.source_name(body.attachments[0])
                        if not preparing.done() or _saved_memories(preparing):
                            yield _sse({"type": "progress", "status": f"Saving key facts from {name} to memory..."})
                        async for beat in _heartbeats_until(preparing, deadline):
                            yield beat
                        prepared = _saved_memories(preparing) if preparing.done() else []
                        if not preparing.done():
                            print(f"[API] Save to Memory: '{name}' was not ready in time; nothing was saved.")
                        elif prepared and loop.time() < deadline:
                            saving = asyncio.create_task(memory_ingest.save(scope, prepared), name="jarvis-save-to-memory-write")
                            async for beat in _heartbeats_until(saving, deadline):
                                yield beat
                            if saving.done():
                                saved = _saved_memories(saving)
                            else:
                                print(f"[API] Save to Memory: writing '{name}' ran out of time; nothing was saved.")
                    yield _sse({
                        "type": "final",
                        "node": event.get("node"),
                        "status": "Done",
                        "message": reply,
                        "chat_text": reply,
                        "spoken_audio_text": spoken or None,
                        "audio": audio.model_dump() if audio else None,
                        "memory_events": event.get("memory_events") or [],
                        # What Save to Memory wrote from the attached files: [] when it was off or saved nothing.
                        "saved_memories": saved,
                    })
                    return
        finally:
            # Not done when the reply went out, or the turn failed or was stopped: nothing more is written
            # (a cancelled write rolls back).
            for task in (preparing, saving):
                if task is not None and not task.done():
                    task.cancel()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        # No caching, and no buffering by a proxy in between: each event is meant to arrive as it happens.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _heartbeats_until(task: "asyncio.Task[Any]", deadline: float) -> AsyncIterator[str]:
    """
    Waits for `task` until the loop's clock reaches `deadline`, with a keep-alive comment line after
    every HEARTBEAT_SECONDS of waiting so nothing in between takes the quiet for a dead connection.
    The task is left as it is: the caller reads it, or cancels it when it is not done.
    """
    loop = asyncio.get_running_loop()
    while not task.done():
        left = deadline - loop.time()
        if left <= 0:
            return
        await asyncio.wait({task}, timeout=min(HEARTBEAT_SECONDS, left))
        if not task.done() and deadline - loop.time() > 0:
            yield _sse({"type": "heartbeat"})


def _saved_memories(task: "asyncio.Task[list[Any]]") -> list[Any]:
    """What a finished Save to Memory step gave (prepared facts, or what was saved); [] for one that
    failed or was cancelled (memory_ingest's steps never should fail)."""
    if task.cancelled():
        return []
    failure = task.exception()
    if failure is not None:
        print(f"[API] Save to Memory failed: {type(failure).__name__}: {failure}")
        return []
    return task.result()


@app.post("/approvals/{approval_id}")
def decide_approval(approval_id: str, body: ApprovalDecision) -> dict:
    """
    The person's Allow or Deny for an Approval Card (an `action_required` event of /chat).
    The run that is waiting on it resumes at once: Allow runs the tool, Deny reports it as
    declined. 404 when the card is gone (answered, timed out, the turn stopped), 403 when
    it was sent to someone else. A plain (sync) endpoint: it only wakes a waiting thread.
    """
    outcome = approvals.resolve(approval_id, body.user_id, body.decision == "allow")
    if outcome == approvals.NOT_FOUND:
        raise HTTPException(status_code=404, detail="That request is no longer waiting for an answer.")
    if outcome == approvals.FORBIDDEN:
        raise HTTPException(status_code=403, detail="That request was not sent to you.")
    return {"ok": True, "decision": body.decision}


# ---- The Memory page (memory_graph.py) ---------------------------------------
# Plain async endpoints on the server's own event loop, where memory_graph.py keeps its
# connection pool; they never wait for the brain, so a long turn does not hold them up.


class RelationUpdate(BaseModel):
    """PATCH /memory/relations/{id}: the fact's status."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["active", "completed", "archived"]


class EntityUpdate(BaseModel):
    """PATCH /memory/entities/{id}: only the fields sent change. memory_graph.py checks the real limits."""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, max_length=400)
    type: Optional[str] = Field(default=None, max_length=200)
    description: Optional[str] = Field(default=None, max_length=1000)


# The signed-in person, as the web app names them (see the top of this file).
MemoryUser = Annotated[str, Header(alias="X-Jarvis-User-Id", max_length=128)]


def _memory_failure(exc: memory_graph.GraphError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"code": exc.code, "detail": exc.message})


async def _memory(call: Any, user_id: str) -> Any:
    """Runs a memory_graph call for the person named by the header; its refusals as { code, detail }."""
    scope = mcp_client.web_scope(user_id)
    if not scope:
        return JSONResponse(status_code=401, content={"code": "no_user", "detail": "Say whose memory this is (X-Jarvis-User-Id)."})
    try:
        return await call(scope)
    except memory_graph.GraphError as exc:
        return _memory_failure(exc)


@app.get("/memory/graph")
async def memory_graph_view(user_id: MemoryUser = "") -> Any:
    return await _memory(memory_graph.graph, user_id)


@app.patch("/memory/entities/{entity_id}")
async def memory_entity_update(entity_id: str, body: EntityUpdate, user_id: MemoryUser = "") -> Any:
    return await _memory(
        lambda scope: memory_graph.update_entity(
            scope, entity_id, name=body.name, type=body.type, description=body.description
        ),
        user_id,
    )


@app.delete("/memory/entities/{entity_id}")
async def memory_entity_delete(entity_id: str, user_id: MemoryUser = "") -> Any:
    return await _memory(lambda scope: memory_graph.delete_entity(scope, entity_id), user_id)


@app.patch("/memory/relations/{relation_id}")
async def memory_relation_update(relation_id: str, body: RelationUpdate, user_id: MemoryUser = "") -> Any:
    return await _memory(lambda scope: memory_graph.update_relation(scope, relation_id, status=body.status), user_id)


@app.delete("/memory/relations/{relation_id}")
async def memory_relation_delete(relation_id: str, user_id: MemoryUser = "") -> Any:
    return await _memory(lambda scope: memory_graph.delete_relation(scope, relation_id), user_id)


# ---- The System Topology page (system_topology.py) ---------------------------


@app.get("/system/topology")
async def system_topology_view(user_id: MemoryUser = "") -> Any:
    """
    What Jarvis can do right now and who does it, for the person named by X-Jarvis-User-Id
    (see system_topology.py for the shape). The owner sees everything; anyone else does not see
    the owner-only parts. Read from the brain's live status - no model call, no network call.
    401 no_user without the header; 503 unavailable when the brain cannot be built.
    """
    scope = mcp_client.web_scope(user_id)
    if not scope:
        return JSONResponse(status_code=401, content={"code": "no_user", "detail": "Say who is asking (X-Jarvis-User-Id)."})
    try:
        brain = await asyncio.to_thread(_get_brain)
    except Exception as exc:  # noqa: BLE001 - reported as a code; the reason stays in this console
        print(f"[API] Topology: the brain could not start: {type(exc).__name__}: {exc}")
        return JSONResponse(status_code=503, content={"code": "unavailable", "detail": "Jarvis could not start."})
    try:
        return await asyncio.to_thread(system_topology.build, brain, scope == "owner")
    except Exception as exc:  # noqa: BLE001
        print(f"[API] Topology failed: {type(exc).__name__}: {exc}")
        return JSONResponse(status_code=500, content={"code": "server", "detail": "The topology could not be read."})


# Speech-to-text runs OUTSIDE the brain's turn (on a worker thread), so a slow upload to the STT
# provider never holds up another person's typed message. The turn itself is the same state
# machine as /chat's, collected into one JSON answer, and stopped the same way when the client
# goes away.
@app.post("/voice", response_model=VoiceResponse)
async def voice(
    request: Request,
    audio: UploadFile = File(),
    history: str = Form("[]"),
    user_id: str = Form("", max_length=128),
    app_url: str = Form("", max_length=300),
    speak: bool = Form(False),
) -> VoiceResponse | JSONResponse | Response:
    # Checked first: no point paying for a transcription nobody can answer.
    brain = await _ready_brain()

    data = await audio.read(MAX_AUDIO_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="The recording was empty.")
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="The recording is too long.")

    try:
        turns = _history_turns.validate_json(history)
    except ValidationError:
        raise HTTPException(status_code=400, detail="history must be a JSON list of {role, content} turns.")

    print(f"[API] Voice: {len(data):,} bytes of {audio.content_type or 'unknown-type'} audio.")
    try:
        heard = await asyncio.to_thread(stt.transcribe, data, audio.content_type)
    except stt.SttError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    transcript = heard.text.strip()[:MAX_PROMPT_CHARS]
    if not transcript:
        raise HTTPException(status_code=422, detail="I couldn't hear anything in that recording.")

    reply = spoken_text = ""
    memory_events: list[dict[str, Any]] = []
    async with aclosing(_turn(request, brain, transcript, turns, user_id, app_url, speak)) as events:
        async for event in events:
            if event.get("type") == "final":
                reply = str(event.get("message") or "").strip()
                spoken_text = str(event.get("spoken") or "").strip() if speak else ""
                memory_events = list(event.get("memory_events") or [])
                break
            if event.get("type") in ("cancelled", "error"):
                break
    if await request.is_disconnected():
        # Stopped: nobody is waiting for this answer (499 is "client closed request").
        return Response(status_code=499)
    if not reply:
        # The words WERE heard: the caller gets them back so the person's message is not lost.
        return JSONResponse(
            status_code=502,
            content={"detail": "The brain returned an empty reply.", "transcript": transcript, "engine": heard.engine},
        )
    voiced = await asyncio.to_thread(_audio_for, spoken_text, speak)
    return VoiceResponse(
        transcript=transcript,
        reply=reply,
        engine=heard.engine,
        spoken_audio_text=spoken_text or None,
        audio=voiced,
        memory_events=memory_events,
    )


# ---- LINE: the owner's chat with Jarvis from their phone (line_channel.py, chat_sync.py) ----------

# A LINE turn still running after this long is stopped, and the owner told so (an agent's long job
# can take minutes; the answer then arrives by push, as the reply token will have expired).
LINE_TURN_TIMEOUT_SECONDS = 300.0
# How long the answer waits, once Jarvis has replied, for a photo's facts to finish saving to memory.
LINE_SAVE_WAIT_SECONDS = 90.0
# LINE turns run one at a time, in the order they arrived: each reads the session the last one wrote.
_line_lock = asyncio.Lock()
_chat_store = chat_sync.ChatStore()
# The LINE conversation when the web app's database cannot be used (no JARVIS_OWNER_USER_ID, no
# database, a failure): its last turns, in this process only, so a follow-up still has its context.
_line_transcript: deque[tuple[str, str]] = deque(maxlen=chat_sync.HISTORY_TURNS)

line_router = APIRouter()


class _NoClient:
    """What _turn watches for a LINE turn: nobody holds a connection open, so nobody can go away."""

    async def is_disconnected(self) -> bool:
        return False


@dataclass
class _LineOutcome:
    texts: list[str]          # the LINE text messages to send
    reply: str = ""           # Jarvis's reply as the session keeps it ("" = keep nothing)
    conversation: Optional[str] = None


@line_router.post(line_channel.WEBHOOK_PATH)
async def line_webhook(
    request: Request,
    background: BackgroundTasks,
    signature: Annotated[str, Header(alias="X-Line-Signature", max_length=200)] = "",
) -> Any:
    """
    LINE's delivery of what happened in the account's chats (see the top of this file). Checked and
    acknowledged at once; the owner's messages are answered afterwards, as a background task, so LINE
    never waits on Jarvis. Never reads a message before its signature is known to be LINE's.
    """
    config = line_channel.settings()
    if not config.secret:
        return JSONResponse(status_code=503, content={"code": "not_configured", "detail": f"Set {line_channel.SECRET_ENV} to receive LINE messages."})
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > line_channel.MAX_BODY_BYTES:
        return JSONResponse(status_code=413, content={"code": "too_large", "detail": "That delivery is too large."})
    if not signature:
        return JSONResponse(status_code=400, content={"code": "no_signature", "detail": "X-Line-Signature is missing."})
    body = await request.body()
    if len(body) > line_channel.MAX_BODY_BYTES:
        return JSONResponse(status_code=413, content={"code": "too_large", "detail": "That delivery is too large."})
    if not line_channel.verify_signature(config.secret, body, signature):
        print("[LINE] Refused a delivery whose signature is not LINE's.")
        return JSONResponse(status_code=401, content={"code": "bad_signature", "detail": "X-Line-Signature does not match the body."})
    try:
        payload = json.loads(body)
    except ValueError:
        return JSONResponse(status_code=400, content={"code": "bad_body", "detail": "The body is not JSON."})

    events, others = line_channel.owner_events(payload, config.owner)
    if others:
        unset = "" if config.owner else f" ({line_channel.LEGACY_OWNER_ENV} is not set, so nobody is the owner)"
        print(f"[LINE] Ignored {others} event{'s' if others != 1 else ''} from outside the owner's own chat{unset}.")
    if events:
        if config.token:
            background.add_task(_line_events, events, config)
        else:
            print(f"[LINE] {len(events)} message(s) from the owner, but {line_channel.TOKEN_ENV} is not set: no answer can be sent.")
    return {"ok": True}


app.include_router(line_router)

# The webhook-only app served on the LINE listener's port (JARVIS_LINE_PORT): this one path and
# nothing else - no /chat, no /memory, no /artifacts, no OpenAPI page.
line_app = FastAPI(title="Jarvis LINE webhook", docs_url=None, redoc_url=None, openapi_url=None)
line_app.include_router(line_router)


async def _start_line_listener() -> Optional[tuple[Any, "asyncio.Task[None]"]]:
    """
    Serves line_app on 127.0.0.1:JARVIS_LINE_PORT (8001) on this event loop - the same process, so
    LINE turns share the brain and its one-turn-at-a-time lock with the web. Only with
    LINE_CHANNEL_SECRET set. The socket is bound here, so a port that is taken is a console line, not
    a uvicorn exit that would take the whole server down with it.
    """
    config = line_channel.settings()
    if not config.secret or not config.port:
        return None
    if config.port == PORT:
        print(f"[LINE] {line_channel.PORT_ENV} is the brain's own port; no separate webhook listener.")
        return None
    import uvicorn

    class _Listener(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self) -> Any:  # Ctrl+C belongs to the main server, which stops this one
            yield

    try:
        sock = socket.create_server((HOST, config.port))
    except OSError as exc:
        print(
            f"[LINE] The webhook listener could not use {HOST}:{config.port} ({exc.strerror or type(exc).__name__}). "
            f"Set {line_channel.PORT_ENV} to a free port. Do not point a tunnel at port {PORT} instead."
        )
        return None
    server = _Listener(uvicorn.Config(line_app, lifespan="off", log_level="info"))
    task = asyncio.create_task(server.serve(sockets=[sock]), name="jarvis-line-listener")
    print(f"[LINE] Webhook listener on http://{HOST}:{config.port}{line_channel.WEBHOOK_PATH} - point the tunnel at port {config.port} only.")
    return server, task


async def _stop_line_listener(server: Any, task: "asyncio.Task[None]") -> None:
    server.should_exit = True
    try:
        await asyncio.wait_for(task, 10)
    except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001 - shutting down regardless
        print(f"[LINE] The webhook listener did not stop cleanly ({type(exc).__name__}).")


def _line_http() -> httpx.AsyncClient:
    """The HTTP client LINE's API is called with, for one delivery (a test hands in its own)."""
    return httpx.AsyncClient()


async def _line_events(events: list[dict[str, Any]], config: line_channel.LineSettings) -> None:
    """One delivery's messages from the owner, answered one after another (LINE already has its 200)."""
    async with _line_lock:
        async with _line_http() as client:
            api = line_channel.LineApi(config.token, client)
            for event in events:
                try:
                    await _line_event(api, config, event, events)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - one message's failure never costs the next its answer
                    print(f"[LINE] A message could not be answered: {type(exc).__name__}")


async def _keep_typing(api: line_channel.LineApi, chat_id: str) -> None:
    """LINE's loading animation, started again before it runs out, until the answer is ready."""
    while True:
        await api.start_loading(chat_id)
        await asyncio.sleep(line_channel.LOADING_REFRESH_SECONDS)


async def _line_event(
    api: line_channel.LineApi,
    config: line_channel.LineSettings,
    event: dict[str, Any],
    batch: list[dict[str, Any]],
) -> None:
    message = event["message"]
    kind = message.get("type")
    reply_token = str(event.get("replyToken") or "")
    if kind not in ("text", "image"):
        if kind in ("audio", "video", "file", "location"):
            await api.deliver(reply_token, config.owner, [line_channel.UNSUPPORTED_REPLY])
        return  # a sticker needs no answer
    if kind == "text" and not str(message.get("text") or "").strip():
        return

    typing = asyncio.create_task(_keep_typing(api, config.owner), name="jarvis-line-typing")
    try:
        if kind == "text":
            text = str(message["text"]).strip()[:MAX_PROMPT_CHARS]
            line_channel.note_text(text)
            print(f"[LINE] A message from the owner ({len(text)} characters).")
            outcome = await _line_turn(text, text)
        else:
            outcome = await _line_photo(api, message, batch)
    finally:
        # Stopped before the answer goes out: a restart after it would show "typing" under the answer.
        typing.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await typing

    how = await api.deliver(reply_token, config.owner, outcome.texts)
    print(f"[LINE] Answered by {how} ({len(outcome.texts)} message{'s' if len(outcome.texts) != 1 else ''}).")
    if outcome.reply:
        await _keep_in_session(outcome.conversation, "jarvis", outcome.reply)


async def _line_photo(api: line_channel.LineApi, message: dict[str, Any], batch: list[dict[str, Any]]) -> _LineOutcome:
    """A photo from the owner: fetched from LINE, read by the vision model, then a turn about what it shows."""
    when = datetime.datetime.now()
    try:
        data, mime = await api.content(str(message.get("id") or ""))
        reading = await line_channel.read_image(data, mime)
    except line_channel.LineError as exc:
        print(f"[LINE] A photo from the owner could not be read: {exc}.")
        name = line_channel.image_name(when)
        conversation, _ = await _line_session()
        await _keep_in_session(conversation, "user", line_channel.stored_photo_message(name, None))
        reply = f"I couldn't read that photo: {exc}."
        return _LineOutcome(line_channel.line_texts(reply), reply, conversation)

    source_file = line_channel.image_name(when, mime)
    asked = " ".join(
        str(item["message"].get("text") or "") for item in batch if item["message"].get("type") == "text"
    )
    saving = reading.kind in line_channel.SAVE_KINDS or line_channel.wants_saving(f"{asked} {line_channel.recent_text()}")
    print(f"[LINE] A photo from the owner: {reading.kind}" + (", saving its key facts to memory." if saving else "."))
    task: Optional[asyncio.Task[Optional[dict[str, Any]]]] = None
    if saving:
        task = asyncio.create_task(
            memory_ingest.ingest_text("owner", source_file, reading.as_document()), name="jarvis-line-save-photo"
        )
    return await _line_turn(
        line_channel.image_prompt(source_file, reading, saving=saving),
        line_channel.stored_photo_message(source_file, reading),
        saving=task,
    )


async def _line_session() -> tuple[Optional[str], list[tuple[str, str]]]:
    """The owner's LINE session in the web app and its last turns; (None, this process's) without one."""
    owner = os.environ.get("JARVIS_OWNER_USER_ID", "").strip()
    conversation = await _chat_store.line_conversation(owner) if owner else None
    rows = await _chat_store.history(conversation) if conversation else None
    if rows is None:
        if not owner:
            print("[LINE] JARVIS_OWNER_USER_ID is not set: this chat is not kept in a /jarvis session.")
        return None, list(_line_transcript)
    return conversation, rows


async def _keep_in_session(conversation: Optional[str], role: str, content: str) -> None:
    _line_transcript.append((role, content))
    if conversation:
        await _chat_store.add(conversation, role, content)


def _line_history(rows: list[tuple[str, str]]) -> list[HistoryTurn]:
    """The session's turns as /chat's history: a web file message keeps its file (chat_sync.parse_stored_user_message)."""
    turns: list[HistoryTurn] = []
    limit = MAX_PROMPT_CHARS * 2
    for role, content in rows[-MAX_HISTORY_TURNS:]:
        if role != "user":
            turns.append(HistoryTurn(role="jarvis", content=content[:limit]))
            continue
        text, files = chat_sync.parse_stored_user_message(content)
        try:
            kept = [Attachment(**item) for item in files]
        except ValidationError:
            text, kept = content, []
        turns.append(HistoryTurn(role="user", content=text[:limit], attachments=kept))
    return turns


async def _line_final(brain: LLMBrain, prompt: str, history: list[HistoryTurn], user_id: str) -> Optional[dict[str, Any]]:
    """One turn of the state machine for LINE: its final (or error) event."""
    async with aclosing(
        _turn(_NoClient(), brain, prompt, history, user_id, "", channel=request_context.CHANNEL_LINE)
    ) as events:
        async for event in events:
            if event.get("type") in ("final", "error", "cancelled"):
                return event
    return None


async def _line_turn(
    prompt: str,
    stored: str,
    saving: Optional["asyncio.Task[Optional[dict[str, Any]]]"] = None,
) -> _LineOutcome:
    """
    One LINE message through the brain, in the owner's LINE session: the session's last turns as its
    history, the message kept in the session (`stored`, what the web shows) before the turn, and the
    answer as LINE text messages - with a line saying what was saved to memory, when anything was.
    """
    conversation, rows = await _line_session()
    history = _line_history(rows)
    await _keep_in_session(conversation, "user", stored)
    owner = os.environ.get("JARVIS_OWNER_USER_ID", "").strip()
    reply, failure = "", ""
    memory_events: list[dict[str, Any]] = []
    try:
        brain = await _ready_brain()
        final = await asyncio.wait_for(_line_final(brain, prompt, history, owner), LINE_TURN_TIMEOUT_SECONDS)
        if final and final.get("type") == "final":
            reply = str(final.get("message") or "").strip()
            memory_events = list(final.get("memory_events") or [])
        if not reply:
            failure = "Jarvis hit an internal error and has no answer. Please try again."
    except HTTPException:
        failure = "Jarvis has no language model set up right now, so it cannot answer."
    except asyncio.TimeoutError:
        failure = "That took too long, so I stopped. Please try again, or ask in /jarvis on the computer."
    except Exception as exc:  # noqa: BLE001 - the owner still hears that it failed
        print(f"[LINE] The turn failed: {type(exc).__name__}")
        failure = "Jarvis hit an internal error and has no answer. Please try again."

    saved: Optional[dict[str, Any]] = None
    if saving is not None:
        try:
            saved = await asyncio.wait_for(saving, LINE_SAVE_WAIT_SECONDS)
        except asyncio.TimeoutError:
            print("[LINE] Saving the photo to memory took too long; it was stopped.")
        except Exception as exc:  # noqa: BLE001 - ingest_text never raises; belt and braces
            print(f"[LINE] Saving the photo to memory failed: {type(exc).__name__}")
    note = line_channel.memory_note(saved, memory_events)
    return _LineOutcome(line_channel.line_texts(reply or failure, note), reply, conversation)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT)
