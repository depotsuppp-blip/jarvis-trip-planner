"""
The Head of Productivity: turns a planning request into a structured draft.

    primary      GPT-4o-mini (OpenAI)  JARVIS_PRODUCTIVITY_MODEL           (default gpt-4o-mini)
    fallback     Gemini Flash          JARVIS_PRODUCTIVITY_FALLBACK_MODEL  (default gemini-flash-latest)
    last resort  Jarvis's Gemini model (agents/base.py)

No tools. Its answer is one JSON object (PLAN_SHAPE below), checked and tidied by
`finish_plan` before the manager sees it; an answer that does not fit gets one
retry. It is a DRAFT: there is no calendar or LINE integration yet, so nothing is
saved or sent - the "integrations" field says which one would carry it out, for
when they exist.

Note: OPENAI_API_KEY is also what switches voice transcription (stt.py) to OpenAI's
Whisper API. Set JARVIS_STT_ENGINE=local to keep recordings on this machine.
"""

from __future__ import annotations

import json
import re
from datetime import datetime

from llm_brain import DEFAULT_TIMEZONE, _env

from .base import Route, SpecialistAgent, gemini_route, last_resort_route, parse_json_object
from .openai_compat import OpenAIProvider

PRIMARY_MODEL = "gpt-4o-mini"
FALLBACK_MODEL = "gemini-flash-latest"

KINDS = ("schedule", "tasks", "reminder")
PRIORITIES = ("low", "normal", "high")
INTEGRATIONS = ("calendar", "line")
MAX_ITEMS = 50
_WHEN = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?)?$")

PLAN_SHAPE = """\
{
  "kind": "schedule" | "tasks" | "reminder",
  "title": "a short title",
  "summary": "one sentence",
  "timezone": "the IANA time zone the times are in",
  "items": [
    {"title": "...", "start": "YYYY-MM-DDTHH:MM" or null, "end": "YYYY-MM-DDTHH:MM" or null,
     "due": "YYYY-MM-DD" or "YYYY-MM-DDTHH:MM" or null, "priority": "low" | "normal" | "high", "notes": "..."}
  ],
  "assumptions": ["anything you had to assume because the request did not say"],
  "integrations": ["calendar" and/or "line": what would carry this out]
}"""

SYSTEM_PROMPT = f"""\
You are the Head of Productivity on Jarvis's team. Jarvis (the manager) gives you one request about plans, schedules, to-dos or reminders. You turn it into ONE JSON object and reply with that JSON only - no other text, no code fence:

{PLAN_SHAPE}

Rules:
- "schedule" is a timetable (items have start and end), "tasks" a to-do list (due dates where given), "reminder" one or a few things to be reminded of (due is when).
- Resolve "tomorrow", "next Monday", "at 9" against the current date, time and time zone given below, and write absolute dates.
- Only what the request asks for: never invent appointments, people or places. Where a time or date is missing, leave it null or pick a sensible one and write that in "assumptions".
- Nothing is saved or sent anywhere: this is a draft."""

DELEGATE_DESCRIPTION = (
    "Hands a planning request to your Head of Productivity, who turns it into a structured draft: "
    "a schedule, a to-do list or a reminder (JSON with titles, dates and times). Use it when the "
    "user wants a day or week planned, tasks organised or a reminder written up. Nothing is saved "
    "to a calendar or sent on LINE yet - those integrations do not exist - so present the draft to "
    "the user in words and say it is not saved anywhere. It does not see this conversation: put "
    "the whole request in `task`, with every date, time and detail the user gave."
)


def _when(value: object, key: str) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not _WHEN.match(value.strip()):
        raise ValueError(f'"{key}" must be "YYYY-MM-DD" or "YYYY-MM-DDTHH:MM", or null (got {value!r})')
    try:
        datetime.fromisoformat(value.strip())
    except ValueError:
        raise ValueError(f'"{key}" is not a real date: {value!r}') from None
    return value.strip()


def _strings(value: object, limit: int, size: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip()[:size] for item in value if str(item or "").strip()][:limit]


def finish_plan(text: str) -> str:
    """The model's JSON, checked and tidied into PLAN_SHAPE (compact JSON). Raises ValueError, which gets one retry."""
    plan = parse_json_object(text)
    kind = plan.get("kind")
    if kind not in KINDS:
        raise ValueError(f'"kind" must be one of {", ".join(KINDS)}')
    items = plan.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError('"items" must be a list with at least one item')

    tidy_items = []
    for item in items[:MAX_ITEMS]:
        if not isinstance(item, dict) or not str(item.get("title") or "").strip():
            raise ValueError("every item needs a title")
        tidy = {
            "title": str(item["title"]).strip()[:200],
            "start": _when(item.get("start"), "start"),
            "end": _when(item.get("end"), "end"),
            "due": _when(item.get("due"), "due"),
            "priority": item.get("priority") if item.get("priority") in PRIORITIES else "normal",
        }
        notes = str(item.get("notes") or "").strip()
        if notes:
            tidy["notes"] = notes[:500]
        tidy_items.append(tidy)

    result = {
        "kind": kind,
        "title": str(plan.get("title") or "").strip()[:120],
        "summary": str(plan.get("summary") or "").strip()[:300],
        "timezone": str(plan.get("timezone") or _env("JARVIS_TIMEZONE", DEFAULT_TIMEZONE)).strip()[:64],
        "items": tidy_items,
        "assumptions": _strings(plan.get("assumptions"), 10, 200),
        "integrations": list(dict.fromkeys(name for name in _strings(plan.get("integrations"), 10, 20) if name in INTEGRATIONS)),
        "status": "draft",
    }
    return json.dumps(result, ensure_ascii=False)


def build(last_resort: Route | None = None) -> SpecialistAgent:
    primary = _env("JARVIS_PRODUCTIVITY_MODEL", PRIMARY_MODEL)
    fallback = _env("JARVIS_PRODUCTIVITY_FALLBACK_MODEL", FALLBACK_MODEL)
    return SpecialistAgent(
        name="productivity",
        title="Head of Productivity",
        system_prompt=SYSTEM_PROMPT,
        routes=[
            Route(OpenAIProvider(model=primary), primary, "primary"),
            gemini_route(fallback, "fallback"),
            last_resort or last_resort_route(),
        ],
        servers=(),
        max_steps=1,
        json_output=True,
        finish=finish_plan,
        delegate_description=DELEGATE_DESCRIPTION,
        task_description="The whole request, self-contained: with every date, time and detail the user gave.",
    )
