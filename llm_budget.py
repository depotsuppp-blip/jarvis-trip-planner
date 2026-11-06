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

PROMPT-CACHE TOKENS ARE WEIGHTED
--------------------------------
Anthropic reports a prompt in three parts: `input_tokens` (read at the
full price), `cache_creation_input_tokens` (written to the prompt cache:
1.25x for the default 5-minute cache, 2x for a 1-hour one) and
`cache_read_input_tokens` (served from the cache: 0.1x). Those are price
MULTIPLIERS, the same for every model the brain uses (Sonnet 5, Opus 5,
Haiku 4.5), not prices - so they stay true when a tier is repriced. The
budget counts input-token equivalents (weighted_tokens): a cache hit
costs a tenth of a token, a cache write a quarter more than one. Before,
only `input_tokens` was counted, which left cache reads AND writes out
entirely - an undercount that grew with every cached prefix. Output
tokens still count one for one, as they always have, so a budget set
before this change means what it meant.

WHERE THIS IS WIRED IN
-----------------------
llm_brain.py's LLMBrain.ask() calls is_budget_exceeded() before doing
any work at all and short-circuits to a canned reply if it's over.
AnthropicProvider.complete() and GeminiProvider.complete() each call
record_usage() with the real token counts from that call's response,
immediately after a successful call (agents/openai_compat.py does too).
The day's file also keeps the raw counts, so .data/llm_usage.json shows
how much of the day's input came from the cache.
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

# Prompt-cache price multipliers, relative to one ordinary input token (Anthropic's: see the
# module docstring). Claude Opus 5.5 and Fable 5.1 read the cache cheaper still (0.05x,
# 0.025x); none of them is in the brain's routing table.
CACHE_WRITE_WEIGHT = 1.25  # a write to the default 5-minute cache
CACHE_WRITE_1H_WEIGHT = 2.0  # a write to a 1-hour cache
CACHE_READ_WEIGHT = 0.1  # a cache hit

# Raw counts kept for the day next to the weighted total.
_RAW_FIELDS = ("input", "output", "cache_write", "cache_read", "calls")


def weighted_tokens(
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_1h_tokens: int = 0,
) -> float:
    """
    One call's spend in input-token equivalents. `cache_write_tokens` is the whole cache
    write (Anthropic's cache_creation_input_tokens); `cache_write_1h_tokens` is the part of
    it that went to a 1-hour cache, charged 2x instead of 1.25x.
    """
    write = max(0, cache_write_tokens)
    write_1h = min(write, max(0, cache_write_1h_tokens))
    return (
        max(0, input_tokens)
        + max(0, output_tokens)
        + (write - write_1h) * CACHE_WRITE_WEIGHT
        + write_1h * CACHE_WRITE_1H_WEIGHT
        + max(0, cache_read_tokens) * CACHE_READ_WEIGHT
    )


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


def record_usage(
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_1h_tokens: int = 0,
) -> float:
    """
    Adds one call to today's recorded spend - weighted_tokens(), so a cache hit counts a
    tenth of a token - and returns that amount. Records nothing (but still returns it) when
    no budget is configured.
    """
    spent = weighted_tokens(input_tokens, output_tokens, cache_write_tokens, cache_read_tokens, cache_write_1h_tokens)
    if _daily_budget() <= 0:
        return spent
    with _lock:
        data = _load()
        data["tokens"] = round(data.get("tokens", 0) + spent, 1)
        raw = data.get("raw") if isinstance(data.get("raw"), dict) else {}
        for name, value in zip(_RAW_FIELDS, (input_tokens, output_tokens, cache_write_tokens, cache_read_tokens, 1)):
            raw[name] = int(raw.get(name, 0)) + max(0, int(value))
        data["raw"] = raw
        _save(data)
    return spent


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
    return max(0, int(budget - spent))
