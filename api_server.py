"""
HTTP front for the brain, for the Trip Planner web app's Jarvis hub.

    POST /chat   { "prompt": "...", "history": [{ "role": "user"|"jarvis", "content": "...",
                                                  "attachments": [...] }],
                   "user_id": "...", "app_url": "...", "speak": false,
                   "attachments": [{ "path": "uploads/...", "name": "report.pdf",
                                     "mime": "application/pdf", "size": 48213 }] }
              -> text/event-stream: server-sent events as the brain's state machine works
                 (llm_brain.py, THE STATE MACHINE), each `event: <type>` + `data: <JSON>`:

                   progress  {"type": "progress", "node": "Delegate_to_Agent",
                              "status": "Delegating to Head of Analytics..."}
                             as each step starts; "node" is left out of the finer steps a
                             specialist agent reports ("Head of Research: Searching the web...")
                   final     {"type": "final", "node": "Final_Response", "status": "Done",
                              "message": "...", "audio": null}           once, last
                   error     {"type": "error", "status": "Failed", "message": "...",
                              "code": 502}                    instead of final, last
                 plus a ": keep-alive" comment line after 10 s without an event.

                 message is a plain sentence, or - when a tool showed the person a widget -
                 a JSON string, see rich_reply.py; a picture or a file Jarvis made is in it
                 as its Markdown line (see GET /artifacts). audio is {"mime": "audio/mpeg",
                 "base64": "..."} only when "speak" was true AND the reply's plain text is at
                 most 200 characters AND the voice worked (reply_audio.py, Edge-TTS); in every
                 other case it is null and the reply is just text. A provider failure is not
                 an error event: it is a final message saying what went wrong, as before.
                 Before the stream opens, the usual HTTP errors: 422 a bad body, 503 no model.

                 STOP. Closing the connection stops the turn (a watcher checks every 0.25 s):
                 the model request in flight is closed, no further tool starts, a specialist
                 agent ends after its current step, Canva's browser is shut, nothing is kept
                 in the brain's history, and the brain is free for the next message at once.
                 attachments are files the person attached in the web hub, which the web
                 app has already saved in this folder's uploads/ (path is relative to this
                 folder). The model is not sent the file: a line is added after the prompt
                 saying where it is and which tool reads it (attachments.py), and the model
                 reads it with the filesystem tool server. prompt may be "" when a file
                 came with no words. User turns in history carry theirs the same way.
    POST /voice  multipart/form-data: audio (the file the browser recorded), and optionally
                   history (a JSON list shaped like /chat's), user_id, app_url, speak
              -> { "transcript": "...", "reply": "...", "engine": "openai"|"local", "audio": null }
                 Speech-to-text (stt.py: OpenAI Whisper with OPENAI_API_KEY, otherwise
                 faster-whisper on this machine) first, then the transcript is asked
                 exactly as a typed /chat prompt would be - the same state machine,
                 collected into this one JSON answer, and stopped the same way when the
                 connection closes. 422 when nothing was heard.
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
                 (no key: "why" says which). mcp_servers is each tool
                 server as it is right now (error says why one is not connected). The bundled
                 servers - local_fs, web_search, fetch, memory, data - start with the brain, with
                 nothing to configure (mcp_client.py). The web hub's sidebar lists them from here
                 (TripPlanner_Web: GET /api/chat/tools).
    GET  /artifacts/<file> -> the file itself: what Jarvis made and saved in artifacts/ - the
                 images from agents/creative_agent.py (image_YYYYMMDD_HHMMSS.png) and the
                 presentations from agents/publishing_agent.py (presentation_YYYYMMDD_HHMMSS.pptx,
                 or .pdf when Canva sends PDFs) and the spreadsheets from agents/analytics_agent.py
                 (spreadsheet_YYYYMMDD_HHMMSS.xlsx).
                 A web chat reply shows a picture as ![Generated Image](/artifacts/<file>) and
                 offers a presentation as [Download Presentation](/artifacts/<file>) (a spreadsheet
                 as [Download Spreadsheet](/artifacts/<file>)); the web app
                 answers that address for the browser by fetching it from here
                 (TripPlanner_Web/app/artifacts/[name]/route.ts), for the signed-in person
                 whose chat it is in. 404 for a file that is not there.

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
  send_line_notification, starting/stopping servers, creating tools) are
  DECLINED. There is no human at a microphone here, and a web request must
  never be able to trigger an outward side effect or run new code on this
  machine by itself. The model is told they were declined and says so.
  Wiring a real confirm step through the web UI is a separate piece of work.
  The one exception is the Head of Productivity's push_line_notification
  (agents/productivity_agent.py): it asks no one, because it can only
  message the owner's own LINE (LINE_TARGET_USER_ID) and sending the daily
  briefing there is its job - and it is capped per task.
* Bound to loopback with no authentication: anything on this machine can
  call it, so do not expose the port.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import threading
from contextlib import aclosing, asynccontextmanager
from typing import Annotated, Any, AsyncIterator, Literal, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, TypeAdapter, ValidationError, model_validator

import attachments
import reply_audio
import request_context
import rich_reply
import stt
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
    attachments: list[Attachment] = Field(default_factory=list, max_length=MAX_ATTACHMENTS)

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
    audio: Optional[ReplyAudio] = None


_history_turns = TypeAdapter(Annotated[list[HistoryTurn], Field(max_length=MAX_HISTORY_TURNS)])


def _decline(question: str) -> bool:
    print(f"[API] Declined a confirmation-gated action: {question}")
    return False


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
    yield


app = FastAPI(title="Jarvis brain", docs_url=None, redoc_url=None, lifespan=_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

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
            _brain = LLMBrain(confirm=_decline)
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
    }


def _prepare(
    brain: LLMBrain,
    prompt: str,
    history: list[HistoryTurn],
    files: list[Attachment] | None,
) -> tuple[str, list[tuple[str, str]]]:
    """
    The prompt as the brain is asked it, and the history as (role, text) pairs. Attached
    files are pointed at, not pasted in: a line after the words says where each file is and
    which tool reads it (attachments.py). Built while the turn holds the brain, because it
    asks the brain which tools it has right now. A .csv/.tsv/.json/.jsonl file is pointed at
    the data tool (load it into a table, answer with SQL) when that server is connected.
    """
    attached = bool(files) or any(t.attachments for t in history)
    tool = attachments.read_tool(brain.tool_names) if attached else None
    data = attachments.data_tool(brain.tool_names) if attached else None
    if files:
        print(f"[API] Attachments: {', '.join(f.path for f in files)} (reader: {tool or 'none'}, data: {data or 'none'}).")
        prompt = attachments.with_context(prompt, files, tool, current=True, data=data)
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
    request: Request,
    brain: LLMBrain,
    prompt: str,
    history: list[HistoryTurn],
    user_id: str,
    app_url: str,
    speak: bool = False,
    files: list[Attachment] | None = None,
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
    """
    caller = request_context.RequestContext(
        channel=request_context.CHANNEL_WEB,
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
                question, turns = _prepare(brain, prompt, history, files)
                brain.set_history(turns)
                try:
                    # The context is current only while this request is being answered, on
                    # this task and the worker threads it starts - see request_context.py.
                    with request_context.use(caller):
                        async for event in brain.run(question, cancel=cancel, progress=progress):
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


def _audio_for(reply: str, speak: bool) -> Optional[ReplyAudio]:
    """
    The spoken version of `reply`, or None: not asked for, longer than the voice limit,
    or the voice is unavailable. Called once the reply is out of the brain - the network
    round trip to the voice must not hold up anyone else's message - and it never raises,
    so a voice failure leaves the person with the text.
    """
    if not speak:
        return None
    try:
        payload = reply_audio.for_reply(reply)
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
    """
    brain = await _ready_brain()

    async def stream() -> AsyncIterator[str]:
        async with aclosing(
            _turn(request, brain, body.prompt, body.history, body.user_id, body.app_url, body.speak, body.attachments)
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
                if body.speak and reply_audio.enabled():
                    yield _sse({"type": "progress", "status": "Recording the spoken reply..."})
                audio = await asyncio.to_thread(_audio_for, reply, body.speak)
                yield _sse({
                    "type": "final",
                    "node": event.get("node"),
                    "status": "Done",
                    "message": reply,
                    "audio": audio.model_dump() if audio else None,
                })
                return

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        # No caching, and no buffering by a proxy in between: each event is meant to arrive as it happens.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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

    reply = ""
    async with aclosing(_turn(request, brain, transcript, turns, user_id, app_url, speak)) as events:
        async for event in events:
            if event.get("type") == "final":
                reply = str(event.get("message") or "").strip()
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
    spoken = await asyncio.to_thread(_audio_for, reply, speak)
    return VoiceResponse(transcript=transcript, reply=reply, engine=heard.engine, audio=spoken)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT)
