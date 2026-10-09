"""
HTTP front for the brain, for the Trip Planner web app's Jarvis hub.

    POST /chat   { "prompt": "...", "history": [{ "role": "user"|"jarvis", "content": "..." }] }
              -> { "reply": "..." }
    GET  /health -> { "ok": true, "provider": "...", "model": "..." }

Stateless by design: the web app's database is the source of truth for the
transcript, so every request carries its own history and replaces whatever
the brain remembered from the last one (LLMBrain.set_history). `history`
is earlier turns only, oldest first, NOT including `prompt`.

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
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from llm_brain import LLMBrain

HOST = "127.0.0.1"
PORT = 8000
# Next.js calls this server-to-server, where CORS does not apply; the
# middleware is for a browser calling it directly during development.
ALLOWED_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000"]
MAX_PROMPT_CHARS = 4000
MAX_HISTORY_TURNS = 100


class HistoryTurn(BaseModel):
    role: Literal["user", "jarvis", "assistant"]
    content: str = Field(max_length=MAX_PROMPT_CHARS * 2)


class ChatRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    history: list[HistoryTurn] = Field(default_factory=list, max_length=MAX_HISTORY_TURNS)


class ChatResponse(BaseModel):
    reply: str


def _decline(question: str) -> bool:
    print(f"[API] Declined a confirmation-gated action: {question}")
    return False


app = FastAPI(title="Jarvis brain", docs_url=None, redoc_url=None)
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
    return {"ok": brain.available, "provider": brain.provider_name, "model": brain.model}


# A plain `def`, not async: ask() blocks for seconds on the network, and
# FastAPI runs sync endpoints in its thread pool so the event loop stays free.
@app.post("/chat", response_model=ChatResponse)
def chat(body: ChatRequest) -> ChatResponse:
    brain = _get_brain()
    if not brain.available:
        raise HTTPException(status_code=503, detail="No language model is configured (see .env).")

    turns = [("user" if t.role == "user" else "assistant", t.content) for t in body.history]
    with _lock:
        brain.set_history(turns)
        reply = brain.ask(body.prompt).strip()
        # Nothing is carried to the next request; the caller owns the transcript.
        brain.set_history([])

    if not reply:
        raise HTTPException(status_code=502, detail="The brain returned an empty reply.")
    return ChatResponse(reply=reply)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT)
