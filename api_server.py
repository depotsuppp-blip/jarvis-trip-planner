"""
HTTP front for the brain, for the Trip Planner web app's Jarvis hub.

    POST /chat   { "prompt": "...", "history": [{ "role": "user"|"jarvis", "content": "..." }],
                   "user_id": "...", "app_url": "..." }
              -> { "reply": "..." }   (a plain sentence, or - when a tool showed the
                                       person a widget - a JSON string, see rich_reply.py)
    POST /voice  multipart/form-data: audio (the file the browser recorded), and optionally
                   history (a JSON list shaped like /chat's), user_id, app_url
              -> { "transcript": "...", "reply": "...", "engine": "openai"|"local" }
                 Speech-to-text (stt.py: OpenAI Whisper with OPENAI_API_KEY, otherwise
                 faster-whisper on this machine) first, then the transcript is asked
                 exactly as a typed /chat prompt would be. 422 when nothing was heard.
    GET  /health -> { "ok": true, "provider": "...", "model": "...",
                      "stt": "openai"|"local", "stt_ready": true|false }
                 stt_ready is false while the on-device model is still loading.

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

Things that differ from the voice assistant, deliberately:

* One LLMBrain, one request at a time. The brain keeps per-turn state
  (history, background-dispatch bookkeeping) and is not thread-safe, so
  requests queue on a lock instead of interleaving.
* Tools that ask for spoken confirmation (n8n webhooks, LINE messages,
  starting/stopping servers, creating tools) are DECLINED. There is no
  human at a microphone here, and a web request must never be able to
  trigger an outward side effect or run new code on this machine by
  itself. The model is told they were declined and says so. Wiring a
  real confirm step through the web UI is a separate piece of work.
* Bound to loopback with no authentication: anything on this machine can
  call it, so do not expose the port.
"""

from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

import request_context
import rich_reply
import stt
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


class HistoryTurn(BaseModel):
    role: Literal["user", "jarvis", "assistant"]
    content: str = Field(max_length=MAX_PROMPT_CHARS * 2)


class ChatRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    history: list[HistoryTurn] = Field(default_factory=list, max_length=MAX_HISTORY_TURNS)
    user_id: str = Field(default="", max_length=128)
    app_url: str = Field(default="", max_length=300)


class ChatResponse(BaseModel):
    reply: str


class VoiceResponse(BaseModel):
    transcript: str
    reply: str
    # Who did the listening: "openai" (the Whisper API) or "local" (faster-whisper on this machine).
    engine: str


_history_turns = TypeAdapter(Annotated[list[HistoryTurn], Field(max_length=MAX_HISTORY_TURNS)])


def _decline(question: str) -> bool:
    print(f"[API] Declined a confirmation-gated action: {question}")
    return False


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # The on-device speech model takes seconds to load: start that now, on its own
    # thread (importing it is slow too), so the first Talk press does not wait for it
    # and the server is not held back from answering. A no-op when OPENAI_API_KEY is set.
    threading.Thread(target=stt.warm_up, name="jarvis-stt-warmup", daemon=True).start()
    yield


app = FastAPI(title="Jarvis brain", docs_url=None, redoc_url=None, lifespan=_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

_brain: LLMBrain | None = None
_lock = threading.Lock()


def _get_brain() -> LLMBrain:
    """Built on first use, so importing this module (or /health) stays cheap."""
    global _brain
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
    }


def _answer(brain: LLMBrain, prompt: str, history: list[HistoryTurn], user_id: str, app_url: str) -> str:
    """One turn through the brain, as /chat and /voice both ask it. May return ''."""
    # A reply that carried widgets is stored as JSON; the model remembers it as the sentence it said.
    turns = [
        ("user", t.content) if t.role == "user" else ("assistant", rich_reply.history_text(t.content))
        for t in history
    ]
    caller = request_context.RequestContext(
        channel=request_context.CHANNEL_WEB,
        user_id=user_id.strip(),
        app_url=app_url.strip(),
    )
    with _lock:
        brain.set_history(turns)
        # The context is current only while this request is being answered, on
        # this thread - see request_context.py.
        with request_context.use(caller):
            reply = brain.ask(prompt).strip()
        # Nothing is carried to the next request; the caller owns the transcript.
        brain.set_history([])
    return reply


# A plain `def`, not async: ask() blocks for seconds on the network, and
# FastAPI runs sync endpoints in its thread pool so the event loop stays free.
@app.post("/chat", response_model=ChatResponse)
def chat(body: ChatRequest) -> ChatResponse:
    brain = _get_brain()
    if not brain.available:
        raise HTTPException(status_code=503, detail="No language model is configured (see .env).")

    reply = _answer(brain, body.prompt, body.history, body.user_id, body.app_url)
    if not reply:
        raise HTTPException(status_code=502, detail="The brain returned an empty reply.")
    return ChatResponse(reply=reply)


# Sync for the same reason as /chat: transcription and the brain both block on the network.
# Speech-to-text runs OUTSIDE the brain's lock, so a slow upload to the STT provider never
# holds up another person's typed message.
@app.post("/voice", response_model=VoiceResponse)
def voice(
    audio: UploadFile = File(),
    history: str = Form("[]"),
    user_id: str = Form("", max_length=128),
    app_url: str = Form("", max_length=300),
) -> VoiceResponse | JSONResponse:
    brain = _get_brain()
    # Checked first: no point paying for a transcription nobody can answer.
    if not brain.available:
        raise HTTPException(status_code=503, detail="No language model is configured (see .env).")

    data = audio.file.read(MAX_AUDIO_BYTES + 1)
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
        heard = stt.transcribe(data, audio.content_type)
    except stt.SttError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    transcript = heard.text.strip()[:MAX_PROMPT_CHARS]
    if not transcript:
        raise HTTPException(status_code=422, detail="I couldn't hear anything in that recording.")

    reply = _answer(brain, transcript, turns, user_id, app_url)
    if not reply:
        # The words WERE heard: the caller gets them back so the person's message is not lost.
        return JSONResponse(
            status_code=502,
            content={"detail": "The brain returned an empty reply.", "transcript": transcript, "engine": heard.engine},
        )
    return VoiceResponse(transcript=transcript, reply=reply, engine=heard.engine)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT)
