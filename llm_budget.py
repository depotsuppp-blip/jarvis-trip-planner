"""
A coarse daily token budget for the conversational brain (llm_brain.py),
so a runaway loop or a much-heavier-than-usual day of use gets caught
with a warning instead of an open-ended bill.

OPT-IN, NOT A DEFAULT LIMIT
---------------------------
JARVIS_DAILY_TOKEN_BUDGET unset (or "0") means no limit at all - every
function below becomes a cheap no-op. This exists to catch a genuine
runaway, not to second-guess ordinary use, so it stays invisible until
someone deliberately configures it.

TOKEN COUNT, NOT A LIVE DOLLAR FIGURE
--------------------------------------
Converting to real cost would mean hardcoding a per-model price table
that goes stale the moment Anthropic or Google reprices a tier. A token
count is what both providers' response usage fields already report
directly, with no pricing data to keep in sync - close enough for "did
today get unusually expensive," which is what this guards against.

WHERE THIS IS WIRED IN
-----------------------
llm_brain.py's LLMBrain.ask() calls is_budget_exceeded() before doing
any work at all and short-circuits to a canned reply if it's over.
AnthropicProvider.complete() and GeminiProvider.complete() each call
record_usage() with the real token counts from that call's response,
immediately after a successful call.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_USAGE_FILE = Path(__file__).resolve().parent / ".data" / "llm_usage.json"
_lock = threading.Lock()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _daily_budget() -> int:
    """0 (the default) means unlimited - see the module docstring."""
    try:
        return max(0, int((os.getenv("JARVIS_DAILY_TOKEN_BUDGET") or "0").strip()))
    except ValueError:
        return 0


def _load() -> dict:
    """Today's usage record, or a fresh zeroed one for a new day or a missing/corrupt file."""
    try:
        with open(_USAGE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return {"date": _today(), "tokens": 0}

    if not isinstance(data, dict) or data.get("date") != _today():
        return {"date": _today(), "tokens": 0}
    return data


def _save(data: dict) -> None:
    try:
        _USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_USAGE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError as exc:
        # A budget tracker that can't persist must not take the
        # assistant down over it - log and move on, same as every other
        # "nice to have" failure in this project.
        print(f"[Budget] Couldn't save today's LLM usage: {exc}")


def record_usage(input_tokens: int, output_tokens: int) -> None:
    """Adds to today's recorded spend. A no-op if no budget is configured."""
    if _daily_budget() <= 0:
        return
    with _lock:
        data = _load()
        data["tokens"] = data.get("tokens", 0) + max(0, input_tokens) + max(0, output_tokens)
        _save(data)


def is_budget_exceeded() -> bool:
    """True only when JARVIS_DAILY_TOKEN_BUDGET is set AND today's recorded spend has reached it."""
    budget = _daily_budget()
    if budget <= 0:
        return False
    with _lock:
        spent = _load().get("tokens", 0)
    return spent >= budget


def remaining_budget() -> Optional[int]:
    """Tokens left today, or None if no budget is configured."""
    budget = _daily_budget()
    if budget <= 0:
        return None
    with _lock:
        spent = _load().get("tokens", 0)
    return max(0, budget - spent)
