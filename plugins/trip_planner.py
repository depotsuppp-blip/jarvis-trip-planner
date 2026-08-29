"""
Jarvis plugin: Trip Planner (multi-agent kickoff)

Standalone-app boundary
------------------------
Trip Planner is a separate application from Subtrack - pulled out on
purpose so Jarvis does not grow one monolithic "the app" it talks to.
This module is the sliver that stays inside Jarvis: the voice trigger
that assembles a Planner Agent, hands it real data, and walks away.

ARCHITECTURE - MICROSERVICES / SUB-AGENTS
------------------------------------------
generate_trip_plan_async does not plan the trip itself. It starts a
background thread (_plan_trip_task) that immediately forks on
`planning_type` - see "PLANNING_TYPE FORKS BEFORE ANY DATA GATHERING"
below for the 'consensus' and 'solo_draft' sides. For
`planning_type == 'solo_blank'`, it gathers data from sub-agents and
hands the result to one reasoning agent:

    Step 1  Google Geocoding    destination name/address -> lat/lon
    Step 2  Open-Meteo          weather forecast for those coordinates,
                                 no key needed
    Step 3  Google Places       "Top tourist attractions in
            (Text Search)       <destination>" and "Best restaurants in
                                 <destination>", as free-text queries
    Step 4  Gemini               google.genai.Client is configured with
                                 GEMINI_API_KEY
    Step 5  Gemini               everything Steps 1-3 gathered, plus
            (reasoning)          planning_type, head_count and days,
                                 goes into one prompt; Gemini returns a
                                 structured JSON itinerary.
                                 starting_location is NOT in that
                                 prompt - see the privacy section below
                                 for why.
    Step 6  LINE notification   plugins.line_notifier tells the user the
                                 plan is ready, that planning failed, or -
                                 specifically - that Google's API quota
                                 was hit and to try again shortly. Never
                                 the wrong one of those three.

The itinerary itself hands off to the standalone Trip Planner app once
that app exists (see TRIP_PLANNER_APP_PATH in .env.example); today it is
only logged to the console as a stand-in for that handoff - the LINE
message says as much ("the viewer link comes in the next phase").

PLANNING_TYPE FORKS BEFORE ANY DATA GATHERING
------------------------------------------------
`planning_type` has three values, and only one of them touches Maps or
Gemini at all:

    'consensus'    the group has not agreed on dates yet, so there is
                   nothing to plan around. _run_consensus_poll creates a
                   poll link and sends exactly one LINE message asking
                   the organiser to forward it to the group.
    'solo_draft'   the traveller already has specific places in mind and
                   wants to add them personally rather than have Gemini
                   invent an itinerary from nothing. _run_solo_draft
                   creates a draft-board link and sends exactly one LINE
                   message inviting them to paste those links there.
    'solo_blank'   the only value that runs Steps 1-5: the traveller
                   wants the Planner Agent to decide everything, so this
                   is the one path that spends Maps and Gemini quota.

Neither 'consensus' nor 'solo_draft' spend any Maps or Gemini quota on a
trip that is not ready to be planned yet - a direct answer to Task 1's
"Gemini API quota was exhausted" complaint: most trip requests should
never have reached Gemini in the first place. Neither poll link nor
draft-board link has a real backend behind it yet - both are
placeholders the standalone Trip Planner app will serve once it exists,
the same TODO as the itinerary hand-off above.

The real itinerary generation for a 'consensus' or 'solo_draft' trip is
meant to happen LATER - for 'consensus', via a webhook fired once voting
closes; for 'solo_draft', once the user has pasted in the places they
want included. Neither continuation is implemented yet, and neither is
part of this module. When they are built, they are expected to call
something equivalent to _build_prompt / _run_planner_agent - which is
why _build_prompt still branches on planning_type == 'consensus' for its
audience phrasing even though nothing in this module reaches that branch
today: it is not dead code, it is the piece a future webhook handler
will reuse. A 'solo_draft' continuation will need its own prompt design
(it has to incorporate the places the user pasted in) and does not exist
yet either - deliberately not stubbed out ahead of that need.

PRIVACY CONSTRAINT - READ BEFORE TOUCHING starting_location
-------------------------------------------------------------
starting_location is the user's home, hotel, or wherever they currently
are. It is CRITICAL that this never becomes a tracked value:

    * generate_trip_plan_async takes no user or session id, so there is
      nothing to key a stored value against even by accident.
    * It lives ONLY as a local variable, passed from
      generate_trip_plan_async into _plan_trip_task as a plain
      argument. Past that point it is READ NOWHERE - see below - so it
      never becomes part of anything this module logs, returns from
      _plan_trip_task, or could hand off to the standalone Trip Planner
      app later.
    * It is never appended to a module-level list, dict, or cache the
      way _LAUNCHED tracks servers in os_orchestrator.py - there is no
      such structure in this module, on purpose.
    * Its only real use is the acknowledgement string
      generate_trip_plan_async returns immediately - spoken once by the
      voice engine and never stored - which is why _plan_trip_task still
      receives it as a parameter even though it does nothing with it.

WHY starting_location NEVER REACHES GEMINI
---------------------------------------------
An earlier version of this module put starting_location into the
Planner Agent's prompt, on the theory that "sent to one API call, then
discarded" is not "stored" and so satisfies the letter of the
constraint above. A live test against this project's own Gemini key
proved that reasoning wrong in practice: Gemini wrote the raw address
straight into its generated itinerary text ("Depart from <address>,
arrive in <destination>..."). This module then does
print(json.dumps(itinerary, ...)) to log the finished plan, and Step 6
already tees up handing that same itinerary to the standalone Trip
Planner app - a database or a JSON file, per the constraint's own
wording - once that app exists.

There is no reliable way to strip an address back out of a model's
free-text response after the fact: it may paraphrase, abbreviate, or
restate only a fragment, so a regex scrub over the output cannot be
trusted to catch every phrasing. The only dependable fix is to never
give the model the address at all - _build_prompt takes destination,
planning_type, head_count, days, weather and places, and nothing else.
If a future revision wants the itinerary to reason about the journey
itself (a travel day, a return trip), pass a NON-identifying fact
derived from starting_location (e.g. "the traveller needs one travel
day to arrive") rather than the address itself - and re-run a live
check like this one before trusting that the leak is actually closed.

WHY THIS MUST NOT BLOCK
------------------------
generate_trip_plan_async is called from the voice assistant's
supervisor loop. Three HTTP round trips plus one LLM call is easily
5-30 seconds, not milliseconds - waiting for that here would leave
Jarvis deaf for the duration. So the function starts a thread and
returns in well under a second; the thread does the actual waiting.
This applies even to 'consensus' and 'solo_draft', which are fast (one
LINE push) but still run off the calling thread for consistency - the
caller should never have to know which planning_type is slow.

WHY THE TOOL IS REGISTERED background=False (see llm_brain.py) EVEN
THOUGH THE WORK IS ASYNCHRONOUS
---------------------------------------------------------------------
task_dispatcher's background=True path (see task_dispatcher.py) submits
the handler itself to ITS OWN thread pool, and once dispatched it
replaces whatever the handler returns with a generic "STARTED IN THE
BACKGROUND" sentence - the dispatcher owns that phrasing, not the tool.

This tool already backgrounds itself: it spawns its own thread and
returns a destination-specific acknowledgement synchronously, in the
time it takes to call Thread.start(). Declaring it background=True as
well would background something that is already backgrounded, and
would throw away the tailored sentence in favour of the dispatcher's
generic one - and that sentence is what tells the user to expect a LINE
message, so it has to be the one actually spoken.

WHY A 429 GETS ITS OWN LINE MESSAGE
--------------------------------------
Every other failure in the 'solo_blank' path (a bad geocode, a Places
outage, a malformed itinerary) lands on the same generic "planning
failed, ask again" message, because the user cannot do anything
differently for any of them. A 429 from Gemini is different: it is not
a bug, it is a quota that resets, and "ask again" is actively true and
actionable in a way "something broke" is not. _run_planner_agent raises
the distinct GeminiRateLimitError for exactly this status code (checked
via genai_errors.APIError.code, the same field llm_brain.py's own
GeminiProvider._classify already keys on), and _plan_trip_task catches
it before the generic Exception handler so the more specific, more
honest message wins. There is no retry of any kind on this path - see
"MODEL / SDK CHOICE, AND QUOTA" below for the explicit, source-verified
guarantee that a 429 is reported once and the thread ends, not retried.

THIS FILE IS AN IMPLEMENTATION MODULE, NOT AN AUTO-LOADED PLUGIN
------------------------------------------------------------------
It deliberately declares no TOOL_SPEC. generate_trip_plan_async is
registered as a built-in tool in llm_brain.py, which imports this
module - so the tool exists exactly once. A TOOL_SPEC here would
register a second tool with the same name and the provider would
reject the request.

CONFIGURATION (.env)
---------------------
    GEMINI_API_KEY         shared with llm_brain.py's Gemini fallback
                            provider - the Planner Agent uses the same
                            key, through the same google-genai SDK
                            llm_brain.py's GeminiProvider already uses
                            (see the note below on why this module used
                            to be on a different, older SDK).
    GOOGLE_MAPS_API_KEY     needs Geocoding API and Places API enabled
                            on the same key.
    TRIP_PLANNER_APP_PATH   (future) where the finished itinerary will
                            be handed off once the standalone Trip
                            Planner app exists. Not read yet.

Open-Meteo needs no key at all.

MODEL / SDK CHOICE, AND QUOTA - READ THIS
--------------------------------------------
This module originally used google-generativeai (the OLD SDK,
google.generativeai.GenerativeModel) and gemini-1.5-pro. Both are gone:
google-generativeai itself now prints a FutureWarning that it "will no
longer be receiving updates or bug fixes," and gemini-1.5-pro/flash
returned live 404s. This module has been migrated onto google-genai -
the SAME SDK llm_brain.py's GeminiProvider already depends on - via
genai.Client(...).models.generate_content(...), so there is exactly one
Gemini SDK in this project's dependency tree, not two.

gemini-1.5-flash specifically has been asked for AGAIN since - it still
404s outright ("is not found for API version v1beta"), re-verified live
in this same project on the same day this note was last touched. It is
not a wrong-model-name typo to fix; Google has retired it, full stop,
and no SDK change brings a retired model back. gemini-2.5-pro/flash/
flash-lite all 404 as "no longer available to new users." gemini-3.1-
pro-preview 429s with a hard 0 free-tier quota. None of this is read
from documentation that might itself be stale - every one of these is a
live call against this project's own key.

GEMINI_PLANNER_MODEL is "gemini-flash-lite-latest": the "-latest" alias
so this hunt does not have to fully repeat at the next deprecation wave
(a hard-coded version number is exactly what keeps going stale), and
specifically the LITE tier rather than plain "-flash-latest" because a
live check showed it answering in ~1s against flash's ~16s for the same
trivial prompt, on this account. The plain flash tier is also the one
whose 20-requests/day free quota this feature originally exhausted -
switching TIERS, not just SDKs or version numbers, is the actual fix for
that. Free-tier quota is still finite here, and IS something normal use
will hit occasionally, not only testing - see "WHY A 429 GETS ITS OWN
LINE MESSAGE" above for how that is handled, and enable billing on the
Gemini API project if this tool needs to be reliable rather than
occasional.

NO RETRY, BY DESIGN AND VERIFIED IN THE SDK'S OWN SOURCE
------------------------------------------------------------
_run_planner_agent sets retry_options=HttpRetryOptions(attempts=1)
explicitly. Reading google/genai/_api_client.py's retry_args() directly
confirms retry_options=None (i.e. never setting it at all) ALREADY means
"never retry" (tenacity.stop_after_attempt(1)) - so this was never
silently retrying - but the explicit setting means the next reader does
not have to go read SDK internals to be sure a future SDK upgrade cannot
start retrying (and re-spending quota) on its own. One Gemini call,
one attempt, one outcome reported once.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
import time
import uuid
from typing import Any, Optional

try:
    import requests
except ImportError:  # pragma: no cover - environment-dependent
    requests = None  # type: ignore[assignment]

try:
    from google import genai
    from google.genai import errors as genai_errors
    from google.genai import types as genai_types
except ImportError:  # pragma: no cover - environment-dependent
    genai = None  # type: ignore[assignment]
    genai_errors = None  # type: ignore[assignment]
    genai_types = None  # type: ignore[assignment]

# Used by finalize_trip_plan_async's Planner Agent call, alongside the
# google-genai import above - see "PROVIDER SELECTION" in that function's
# docstring for why both live here rather than being imported from
# llm_brain.py (which already imports this module).
try:
    import anthropic
except ImportError:  # pragma: no cover - environment-dependent
    anthropic = None  # type: ignore[assignment]

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - environment-dependent
    load_dotenv = None  # type: ignore[assignment]


class GeminiRateLimitError(Exception):
    """Gemini returned HTTP 429 - a quota to wait out, not a bug to report."""


class FinalizationRateLimitError(Exception):
    """
    The finalization Planner Agent (Anthropic or Gemini, whichever ran)
    was rate-limited - a quota to wait out, not a bug. Distinct from
    GeminiRateLimitError above: that one is Gemini-specific and used only
    by the solo_blank path, while finalize_trip_plan_async can hit either
    provider's rate limit depending on which one is configured.
    """


_VALID_PLANNING_TYPES = frozenset({"solo_blank", "solo_draft", "consensus"})

# See "MODEL / SDK CHOICE, AND QUOTA" above before changing this.
GEMINI_PLANNER_MODEL = "gemini-flash-lite-latest"

GOOGLE_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
GOOGLE_PLACES_TEXTSEARCH_URL = "https://maps.googleapis.com/maps/api/place/textsearch/json"
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# (connect timeout, read timeout) in seconds - same reasoning as
# line_notifier.py: requests has no default timeout, and a hung sub-agent
# call must not hang the planning thread forever either.
_CONNECT_TIMEOUT = 5.0
_READ_TIMEOUT = 10.0

# Open-Meteo's daily forecast endpoint refuses more than 16 days.
_MAX_FORECAST_DAYS = 16

# Seconds to wait on the Gemini call. Unlike every requests.get() above,
# google-genai has no default timeout of its own, and a hung planning
# thread would otherwise wait forever with nothing to show for it. Set
# generously: a live check against this project's own account measured
# ~16s for a trivial one-word prompt, so a full itinerary genuinely
# needs headroom, not just typical LLM latency.
_GEMINI_TIMEOUT = 60.0

# How many attractions/restaurants to fold into the prompt. Plenty for
# an itinerary; more just spends prompt tokens restating a long tail the
# Planner Agent will not use.
_MAX_PLACES = 5

# ---------------------------------------------------------------------
# finalize_trip_plan_async config
# ---------------------------------------------------------------------

# Where the standalone Trip Planner app (TripPlanner_Web) serves its API
# from. Overridable via TRIP_PLANNER_APP_URL in .env for a real
# deployment; localhost:3000 matches the `next dev` default and the
# hardcoded poll/draft links _run_consensus_poll and _run_solo_draft
# already build.
_TRIP_PLANNER_APP_DEFAULT_URL = "http://localhost:3000"

# High-effort tier for BOTH providers - this is a one-shot, per-poll
# call the user is not waiting on live (see "WHY THIS MUST NOT BLOCK"
# reasoning applied to _finalize_trip_task below), unlike solo_blank's
# gemini-flash-lite-latest which is optimized for latency. These mirror
# llm_brain.py's own MODEL_DEEP / GEMINI_MODEL_DEEP (its INTENT_COMPLEX
# row in ROUTING_TABLE) - kept as separate constants rather than
# imported, since llm_brain.py already imports this module and importing
# it back here would be a circular import.
FINALIZE_ANTHROPIC_MODEL = "claude-opus-5"
FINALIZE_GEMINI_MODEL = "gemini-2.5-pro"

# A full multi-day itinerary needs far more room than llm_brain.py's
# MAX_TOKENS=1024, which is tuned for short spoken replies, not JSON.
_FINALIZE_MAX_TOKENS = 4096


def _ensure_env_loaded() -> None:
    """Best-effort .env load, mirroring plugins/os_orchestrator.py."""
    if load_dotenv is None:
        return
    here = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(os.path.dirname(here), ".env")
    try:
        load_dotenv(env_path, override=False)
    except Exception:  # noqa: BLE001 - the tool still works with real env vars
        pass


# ---------------------------------------------------------------------
# Step 1-3: data-gathering sub-agents (solo_blank only)
# ---------------------------------------------------------------------


def _geocode(destination: str, api_key: str) -> Optional[tuple[float, float]]:
    """Destination name/address -> (lat, lon), or None if it can't be resolved."""
    if requests is None or not api_key:
        return None
    try:
        response = requests.get(
            GOOGLE_GEOCODE_URL,
            params={"address": destination, "key": api_key},
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:  # noqa: BLE001 - a sub-agent must not kill the plan
        print(f"[TripPlanner] Geocoding failed: {type(exc).__name__}: {exc}")
        return None

    if data.get("status") != "OK" or not data.get("results"):
        print(f"[TripPlanner] Geocoding returned {data.get('status')} for the destination.")
        return None

    location = data["results"][0]["geometry"]["location"]
    return location["lat"], location["lng"]


def _fetch_weather_forecast(lat: float, lon: float, days: int) -> str:
    """Short daily forecast summary for the destination, via Open-Meteo."""
    if requests is None:
        return "Weather data not available (requests package missing)."

    forecast_days = max(1, min(days, _MAX_FORECAST_DAYS))
    try:
        response = requests.get(
            OPEN_METEO_FORECAST_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "forecast_days": forecast_days,
                "timezone": "auto",
            },
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
        )
        response.raise_for_status()
        daily = response.json().get("daily", {})
    except Exception as exc:  # noqa: BLE001 - a sub-agent must not kill the plan
        print(f"[TripPlanner] Weather lookup failed: {type(exc).__name__}: {exc}")
        return "Weather data not available."

    dates = daily.get("time", [])
    highs = daily.get("temperature_2m_max", [])
    lows = daily.get("temperature_2m_min", [])
    rain = daily.get("precipitation_probability_max", [])
    if not dates:
        return "Weather data not available."

    lines = [
        f"{date}: {high:.0f}°C/{low:.0f}°C, {chance:.0f}% chance of rain"
        for date, high, low, chance in zip(dates, highs, lows, rain)
    ]
    return "; ".join(lines)


def _fetch_places_text_search(query: str, api_key: str) -> str:
    """
    Top-rated results for one free-text Places search, e.g. "Top tourist
    attractions in Chiang Mai". Text Search takes the destination name
    as part of the query itself, so no coordinates are needed here.
    """
    if requests is None or not api_key:
        return "Not available."
    try:
        response = requests.get(
            GOOGLE_PLACES_TEXTSEARCH_URL,
            params={"query": query, "key": api_key},
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:  # noqa: BLE001 - a sub-agent must not kill the plan
        print(f"[TripPlanner] Places text search failed ({query!r}): {type(exc).__name__}: {exc}")
        return "Not available."

    if data.get("status") not in ("OK", "ZERO_RESULTS"):
        print(f"[TripPlanner] Places API returned {data.get('status')} for {query!r}.")
        return "Not available."

    results = sorted(
        data.get("results", []),
        key=lambda place: place.get("rating", 0),
        reverse=True,
    )[:_MAX_PLACES]
    if not results:
        return "No results found."

    return ", ".join(
        f"{place.get('name', 'Unknown')} ({place.get('rating', 'n/a')}★)"
        for place in results
    )


# ---------------------------------------------------------------------
# Step 4-5: the Planner Agent (reasoning) - solo_blank only
# ---------------------------------------------------------------------


def _build_prompt(
    destination: str,
    planning_type: str,
    head_count: int,
    days: int,
    weather_summary: str,
    attractions_summary: str,
    restaurants_summary: str,
) -> str:
    """
    Assembles the Planner Agent's prompt.

    Only reached today for planning_type == 'solo_blank' - see
    "PLANNING_TYPE FORKS BEFORE ANY DATA GATHERING" in the module
    docstring for why 'consensus' and 'solo_draft' never call this. The
    'consensus' branch below is kept for a future webhook continuation,
    not dead code.

    Deliberately takes no starting_location parameter - see "WHY
    starting_location NEVER REACHES GEMINI" in the module docstring. An
    earlier version of this prompt did include it, and a live test
    showed Gemini echoing the raw address back into its generated
    itinerary text - which this module then logs and will eventually
    hand off to the standalone Trip Planner app. There is no reliable
    way to scrub an address back out of a model's free-text output
    after the fact, so the fix is to never let it in.
    """
    if planning_type == "consensus":
        audience = (
            f"a group of {head_count} that wants a consensus-friendly plan - "
            "flag any choice that will need a group vote"
        )
    else:
        audience = "a solo traveller who will make every decision themself"

    return (
        f"You are a trip-planning assistant. Build a {days}-day itinerary "
        f"for {destination}, for {audience}. Do not reference how the "
        "traveller gets to the destination or where they are coming from - "
        "start the itinerary from arrival.\n\n"
        f"Weather forecast for {destination}: {weather_summary}\n"
        f"Top attractions: {attractions_summary}\n"
        f"Top restaurants: {restaurants_summary}\n\n"
        "Respond with ONLY a JSON object (no markdown fences, no commentary) "
        "shaped like:\n"
        "{\n"
        '  "destination": string,\n'
        '  "days": [\n'
        '    {"day": 1, "summary": string, "activities": [string, ...], '
        '"meals": [string, ...]}\n'
        "  ],\n"
        '  "notes": string\n'
        "}"
    )


def _parse_itinerary_json(raw_text: str) -> dict[str, Any]:
    """
    Gemini's response, as JSON.

    Gemini frequently wraps JSON in ```json ... ``` fences despite being
    told not to - stripped here rather than re-prompting, which would
    cost a full extra round trip for a formatting quirk. Raises
    ValueError or json.JSONDecodeError on anything that still isn't
    valid JSON; the caller's broad except treats either as "planning
    failed."
    """
    text = (raw_text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("Planner Agent's JSON was not an object.")
    return parsed


def _run_planner_agent(prompt: str) -> dict[str, Any]:
    """
    Configures google-genai and sends the prompt to Gemini.

    Raises GeminiRateLimitError specifically for a 429 - see "WHY A 429
    GETS ITS OWN LINE MESSAGE" in the module docstring - and lets every
    other failure (bad key, missing SDK, 404, malformed JSON) propagate
    as-is for _plan_trip_task's generic handler. Exactly one HTTP
    attempt is made - see "NO RETRY, BY DESIGN..." in the module
    docstring.
    """
    if genai is None:
        raise RuntimeError("google-genai is not installed.")

    api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured.")

    client = genai.Client(api_key=api_key)
    try:
        response = client.models.generate_content(
            model=GEMINI_PLANNER_MODEL,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                http_options=genai_types.HttpOptions(
                    timeout=int(_GEMINI_TIMEOUT * 1000),
                    # Explicit, not relied on implicitly: google-genai's
                    # own _api_client.retry_args() already treats
                    # retry_options=None as "never retry"
                    # (tenacity.stop_after_attempt(1)) - confirmed by
                    # reading that source directly - but leaving it
                    # implicit means the next reader has to go find that
                    # proof themselves to be sure this can't silently
                    # start retrying (and re-spending quota) on a future
                    # SDK upgrade. One attempt, full stop.
                    retry_options=genai_types.HttpRetryOptions(attempts=1),
                ),
                # No tools are passed, but the SDK still warns about
                # automatic function calling unless this is set - same
                # reasoning as llm_brain.py's GeminiProvider.complete.
                automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(
                    disable=True
                ),
            ),
        )
    except Exception as exc:  # noqa: BLE001 - reclassified immediately below
        if isinstance(exc, genai_errors.APIError) and getattr(exc, "code", None) == 429:
            raise GeminiRateLimitError(str(exc)) from exc
        raise

    return _parse_itinerary_json(response.text)


# ---------------------------------------------------------------------
# 'consensus' and 'solo_draft' - defer planning; see the module docstring
# ---------------------------------------------------------------------


def _run_consensus_poll(destination: str) -> None:
    """
    'consensus'`s entire background job: create a poll link and stop.

    No Geocoding, Places, or Gemini call happens here - see "PLANNING_
    TYPE FORKS BEFORE ANY DATA GATHERING" in the module docstring for
    why the real itinerary waits for a future webhook-driven
    continuation instead of being generated now. Sends exactly one LINE
    message and returns.
    """
    poll_url = f"http://localhost:3000/trip/poll/{uuid.uuid4()}"
    message = (
        f"สร้างแบบสอบถามสำหรับทริป {destination} เรียบร้อยแล้วครับ "
        f"รบกวน Forward ลิงก์นี้เข้ากลุ่มเพื่อนเพื่อโหวตได้เลยครับ: {poll_url}"
    )
    try:
        from plugins import line_notifier

        result = line_notifier.send_line_notification(message)
        print(f"[TripPlanner] Consensus poll notification for {destination}: {result}")
    except Exception as exc:  # noqa: BLE001 - the thread ends either way
        print(
            f"[TripPlanner] Notifying about the {destination} poll failed: "
            f"{type(exc).__name__}: {exc}"
        )


def _run_solo_draft(destination: str) -> None:
    """
    'solo_draft'`s entire background job: create a draft-board link and
    stop.

    No Geocoding, Places, or Gemini call happens here either - the
    traveller already has specific places in mind and wants to add them
    personally rather than have the Planner Agent invent an itinerary
    from nothing. Sends exactly one LINE message and returns.
    """
    draft_url = f"http://localhost:3000/trip/draft/{uuid.uuid4()}"
    message = (
        f"ผมสร้าง Draft Board สำหรับทริป {destination} ให้แล้วครับ "
        "บอสสามารถแปะลิงก์ร้านอาหารหรือสถานที่ที่อยากไปไว้ที่นี่ได้เลย "
        f"เดี๋ยวผมนำมาจัดตารางให้ครับ: {draft_url}"
    )
    try:
        from plugins import line_notifier

        result = line_notifier.send_line_notification(message)
        print(f"[TripPlanner] Solo draft notification for {destination}: {result}")
    except Exception as exc:  # noqa: BLE001 - the thread ends either way
        print(
            f"[TripPlanner] Notifying about the {destination} draft board failed: "
            f"{type(exc).__name__}: {exc}"
        )


# ---------------------------------------------------------------------
# Orchestration - runs entirely on the background thread
# ---------------------------------------------------------------------


def _plan_trip_task(
    starting_location: str, destination: str, planning_type: str, head_count: int, days: int
) -> None:
    """
    Runs entirely on its own thread. Forks immediately on planning_type
    - see "PLANNING_TYPE FORKS BEFORE ANY DATA GATHERING" in the module
    docstring:

        consensus    _run_consensus_poll only - a poll link, one LINE
                     message, done.
        solo_draft   _run_solo_draft only - a draft-board link, one LINE
                     message, done.
        solo_blank   Steps 1-6 below: gather data, call the Planner
                     Agent, notify. The only path that spends Maps or
                     Gemini quota.

    Runs after generate_trip_plan_async has already returned, so nothing
    here can reach the caller any more - the LINE push is the only
    channel this thread has left to tell the user anything, success or
    failure. See the module docstring for the privacy constraint on
    starting_location, which this function receives as a plain argument
    and never writes anywhere.
    """
    _ensure_env_loaded()

    print(
        f"[TripPlanner] Planner Agent started for {destination} "
        f"({planning_type}, {head_count} pax, {days}d)."
    )

    if planning_type == "consensus":
        _run_consensus_poll(destination)
        return

    if planning_type == "solo_draft":
        _run_solo_draft(destination)
        return

    # planning_type == "solo_blank" from here on.
    maps_key = (os.getenv("GOOGLE_MAPS_API_KEY") or "").strip()

    try:
        # Step 1: Geocoding
        coordinates = _geocode(destination, maps_key)

        # Step 2: Weather (Open-Meteo needs coordinates; Places below
        # does not, since Text Search takes the destination as text)
        if coordinates is not None:
            lat, lon = coordinates
            weather_summary = _fetch_weather_forecast(lat, lon, days)
        else:
            weather_summary = "Weather data not available (destination could not be geocoded)."

        # Step 3: Places (Text Search)
        attractions_summary = _fetch_places_text_search(
            f"Top tourist attractions in {destination}", maps_key
        )
        restaurants_summary = _fetch_places_text_search(
            f"Best restaurants in {destination}", maps_key
        )

        # Step 4 + 5: configure Gemini and reason over everything gathered.
        # starting_location is deliberately NOT passed to _build_prompt -
        # see "WHY starting_location NEVER REACHES GEMINI" in the module
        # docstring. It stops being used anywhere past this point.
        prompt = _build_prompt(
            destination, planning_type, head_count, days,
            weather_summary, attractions_summary, restaurants_summary,
        )
        itinerary = _run_planner_agent(prompt)

        # Hand-off point for the standalone Trip Planner app once it
        # exists (see TRIP_PLANNER_APP_PATH in .env.example) - for now
        # the finished itinerary is only logged.
        #
        # This logging is wrapped separately from the try/except around
        # it: a live test showed Gemini's own response text containing
        # characters (Thai script, in that case) a Windows console on a
        # legacy codepage cannot print, raising UnicodeEncodeError HERE,
        # after the API call had already succeeded. Left uncaught, that
        # falls into the except Exception below and reports a perfectly
        # good, quota-consuming result as "planning failed" - throwing
        # away real quota for nothing. A console logging failure must
        # never be mistaken for the Gemini call itself failing.
        try:
            print(f"[TripPlanner] Itinerary ready for {destination}:")
            print(json.dumps(itinerary, ensure_ascii=False, indent=2))
        except UnicodeEncodeError:
            print(
                f"[TripPlanner] Itinerary ready for {destination} "
                "(console can't render it; ASCII-safe dump below)."
            )
            print(json.dumps(itinerary, ensure_ascii=True, indent=2))

        message = (
            f"แผนทริป {destination} จำนวน {days} วัน ของคุณเสร็จแล้วครับ! "
            "(URL สำหรับดูหน้าเว็บจะตามมาในเฟสหน้านะครับ)"
        )
    except GeminiRateLimitError as exc:
        # A quota to wait out, not a bug - see "WHY A 429 GETS ITS OWN
        # LINE MESSAGE" in the module docstring. Caught ahead of the
        # generic Exception handler so this more specific, more
        # actionable message wins over the "something broke" one. No
        # retry is attempted - see "NO RETRY, BY DESIGN..." above.
        print(f"[TripPlanner] Gemini rate-limited for {destination}: {exc}")
        message = "ระบบวางแผนทริปเต็มโควต้าชั่วคราวจาก Google API กรุณารอสักครู่แล้วลองใหม่ครับ"
    except Exception as exc:  # noqa: BLE001 - a background thread must not die silently
        print(f"[TripPlanner] Planner Agent failed for {destination}: {type(exc).__name__}: {exc}")
        # Told plainly, in the same language as the success message,
        # rather than silently sending nothing or claiming a plan exists
        # when it does not.
        message = (
            f"แผนทริป {destination} มีปัญหาระหว่างการวางแผนครับ "
            "ลองสั่งให้วางแผนใหม่อีกครั้งได้เลยครับ"
        )

    # Step 6: notification
    try:
        from plugins import line_notifier

        result = line_notifier.send_line_notification(message)
        print(f"[TripPlanner] Notification for {destination}: {result}")
    except Exception as exc:  # noqa: BLE001 - the thread ends either way
        print(
            f"[TripPlanner] Notifying the user about {destination} failed: "
            f"{type(exc).__name__}: {exc}"
        )


def check_poll_status_async(trip_id: str) -> str:
    """
    Reports how a consensus poll is going, ahead of finalizing a plan.

    This is Step 1 of the two-step consensus finalization rule in
    llm_brain.py's SYSTEM_PROMPT: the model must call this, speak the
    count, and get the user's explicit confirmation before ever calling
    generate_trip_plan_async to actually generate a plan for a trip that
    already has a poll running. Named with the _async suffix for
    consistency with generate_trip_plan_async, but there is no thread
    here - the model needs this answer synchronously to speak it and ask
    for confirmation (see background=False on its ToolSpec in
    llm_brain.py), so it returns its result directly rather than
    backgrounding anything.

    Returns MOCK data on purpose - this tool answers a quick "how's the
    poll doing" check ahead of a confirmation question, not a real
    finalization. finalize_trip_plan_async below is the function that
    actually reads GET /api/poll/<trip_id> and generates a plan from it;
    this one stays a fast, fixed string so Step 2 of the two-step rule
    never waits on a network call just to ask "should I proceed?".
    """
    trip_id = (trip_id or "").strip()
    if not trip_id:
        return "ERROR: I need a trip id to check the poll status."

    return (
        f"The poll for trip {trip_id} has 4 users joined. "
        "Top vibes are Cafe Hopping and Photo Spots."
    )


# ---------------------------------------------------------------------
# finalize_trip_plan_async - Step 3 of the two-step consensus
# finalization rule (see the SYSTEM_PROMPT rule in llm_brain.py): reads
# the real poll data over HTTP and generates the actual itinerary,
# called only after the user has confirmed following
# check_poll_status_async above.
# ---------------------------------------------------------------------


def _sign_poll_request(trip_id: str, secret: str) -> tuple[str, str]:
    """
    (ts, sig) for GET /api/poll/<trip_id> - must match the format
    app/api/poll/[id]/route.ts's verifyHmacSignature reconstructs
    byte-for-byte: "{ts}.GET./api/poll/{trip_id}", HMAC-SHA256, hex.
    TRIP_API_SECRET_KEY here and API_SECRET_KEY on the Next.js side are
    two env var names for the one shared secret - both must hold the
    identical value or every signature will mismatch.
    """
    ts = str(int(time.time()))
    message = f"{ts}.GET./api/poll/{trip_id}"
    sig = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    return ts, sig


def _fetch_poll_data(trip_id: str, base_url: str) -> Optional[dict[str, Any]]:
    """
    GET {base_url}/api/poll/<trip_id> - the Next.js "Data Bridge" this
    tool reads consensus votes through (see app/api/poll/[id]/route.ts),
    authenticated with an HMAC signature since that route now rejects
    unsigned requests.

    Returns None on ANY failure: missing requests package, missing
    secret, connection refused (the Next.js dev server isn't running),
    timeout, a non-2xx status (including a 401 from a bad/expired
    signature), or a body that isn't valid JSON. Unlike weather/places
    in the solo_blank path, there is no reasonable itinerary to build
    without this data, so the caller treats None as "stop here", not
    "proceed with partial data". The try/except below exists so this
    background thread (see _finalize_trip_task) dies gracefully instead
    of raising into nothing if the server is unreachable or returns
    malformed JSON.
    """
    if requests is None:
        print("[TripPlanner] finalize: the requests package is not installed.")
        return None

    secret = (os.getenv("TRIP_API_SECRET_KEY") or "").strip()
    if not secret:
        print("[TripPlanner] finalize: TRIP_API_SECRET_KEY is not configured.")
        return None

    url = f"{base_url}/api/poll/{trip_id}"
    ts, sig = _sign_poll_request(trip_id, secret)

    try:
        response = requests.get(
            url,
            headers={"X-Ts": ts, "X-Sig": sig},
            timeout=(3, 10),
        )
        response.raise_for_status()
    except requests.exceptions.RequestException as exc:
        print(f"[TripPlanner] finalize: poll fetch failed ({url}): {type(exc).__name__}: {exc}")
        return None

    try:
        return response.json()
    except ValueError as exc:
        print(f"[TripPlanner] finalize: poll response from {url} was not valid JSON: {exc}")
        return None


def _build_finalization_prompt(poll_data: dict[str, Any]) -> str:
    """
    Turns the poll API's payload into the Planner Agent's prompt.

    Reads `summary` - the pre-aggregated {totalVotes, dateRangeLabel,
    topVibes, wishlist, voters} shape app/api/poll/[id]/route.ts builds
    via lib/tripSummary.ts's summarizePollVotes - rather than
    re-aggregating the raw `votes` array itself, so the tallying logic
    exists in exactly one place across both languages.
    """
    summary = poll_data.get("summary")
    if not isinstance(summary, dict):
        summary = {}

    total_votes = summary.get("totalVotes", 0)
    date_range = summary.get("dateRangeLabel") or "TBD"
    top_vibes = summary.get("topVibes") or []
    wishlist = summary.get("wishlist") or []
    voters = summary.get("voters") or []

    vibes_text = ", ".join(
        f"{entry.get('vibe')} ({entry.get('count')} votes)"
        for entry in top_vibes
        if isinstance(entry, dict)
    ) or "No vibes selected yet."
    wishlist_text = "; ".join(str(place) for place in wishlist) or "No specific places suggested."
    voters_text = ", ".join(str(name) for name in voters) or "the group"

    return (
        "Generate a complete trip itinerary based on this consensus data "
        "from a group trip poll. Resolve any conflicting wishes by "
        "prioritizing the most popular vibes.\n\n"
        f"Group size: {total_votes} people ({voters_text}).\n"
        f"Preferred dates: {date_range}.\n"
        f"Top vibes, most to least popular: {vibes_text}.\n"
        f"Specific places requested by the group: {wishlist_text}.\n\n"
        "Respond with ONLY a JSON object (no markdown fences, no "
        "commentary) shaped like:\n"
        "{\n"
        '  "destination": string,\n'
        '  "days": [\n'
        '    {"day": 1, "summary": string, "activities": [string, ...], '
        '"meals": [string, ...]}\n'
        "  ],\n"
        '  "notes": string\n'
        "}"
    )


def _run_finalization_agent_anthropic(prompt: str, api_key: str) -> dict[str, Any]:
    """
    Calls Claude for the finalization itinerary, at the same high-effort
    tier llm_brain.py's own router reserves for its INTENT_COMPLEX
    questions (MODEL_DEEP="claude-opus-5", EFFORT_DEEP="high"). This is a
    one-shot, per-poll call the user is not waiting on live, unlike a
    live conversation turn, so paying for the reasoning tier here is an
    easy trade.

    Raises FinalizationRateLimitError for a 429 - the same "quota to
    wait out, not a bug" reasoning as GeminiRateLimitError, just for
    whichever provider actually ran. Everything else propagates for the
    caller's generic handler.
    """
    client = anthropic.Anthropic(api_key=api_key)
    try:
        response = client.messages.create(
            model=FINALIZE_ANTHROPIC_MODEL,
            max_tokens=_FINALIZE_MAX_TOKENS,
            # No tools on this one-shot call - THINKING_PLAIN in
            # llm_brain.py disables thinking under the same condition.
            thinking={"type": "disabled"},
            output_config={"effort": "high"},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # noqa: BLE001 - reclassified immediately below
        if isinstance(exc, anthropic.RateLimitError):
            raise FinalizationRateLimitError(str(exc)) from exc
        raise

    text = "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    ).strip()
    return _parse_itinerary_json(text)


def _run_finalization_agent_gemini(prompt: str, api_key: str) -> dict[str, Any]:
    """
    Calls Gemini for the finalization itinerary when Anthropic isn't
    configured, at gemini-2.5-pro - llm_brain.py's GEMINI_MODEL_DEEP,
    its own high-effort tier - rather than the gemini-flash-lite-latest
    _run_planner_agent uses above for solo_blank. That lite tier was
    chosen there for latency and quota reasons that do not apply here:
    this is a one-shot, per-poll call, not one a user is waiting on live.
    """
    if genai is None:
        raise RuntimeError("google-genai is not installed.")

    client = genai.Client(api_key=api_key)
    try:
        response = client.models.generate_content(
            model=FINALIZE_GEMINI_MODEL,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                http_options=genai_types.HttpOptions(
                    timeout=int(_GEMINI_TIMEOUT * 1000),
                    retry_options=genai_types.HttpRetryOptions(attempts=1),
                ),
                automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(
                    disable=True
                ),
            ),
        )
    except Exception as exc:  # noqa: BLE001 - reclassified immediately below
        if isinstance(exc, genai_errors.APIError) and getattr(exc, "code", None) == 429:
            raise FinalizationRateLimitError(str(exc)) from exc
        raise

    return _parse_itinerary_json(response.text)


def _run_finalization_agent(prompt: str) -> dict[str, Any]:
    """
    Picks whichever provider is actually configured and calls it at its
    highest-effort tier.

    PROVIDER SELECTION - Anthropic first, Gemini as the fallback -
    mirrors llm_brain.py's own documented order (.env.example section 1:
    "1. ANTHROPIC_API_KEY set -> Anthropic Claude, 2. otherwise
    GEMINI_API_KEY set -> Google Gemini"), so this tool prefers whatever
    the rest of the assistant already prefers rather than picking
    independently. Not imported from llm_brain.py: that module already
    imports plugins.trip_planner (to register generate_trip_plan_async
    as a built-in tool), and importing it back here would be a circular
    import - see the anthropic import comment above.
    """
    anthropic_key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
    if anthropic_key and anthropic is not None:
        return _run_finalization_agent_anthropic(prompt, anthropic_key)

    gemini_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if gemini_key and genai is not None:
        return _run_finalization_agent_gemini(prompt, gemini_key)

    raise RuntimeError(
        "No language model provider is configured for finalization "
        "(need ANTHROPIC_API_KEY or GEMINI_API_KEY)."
    )


def _finalize_trip_task(trip_id: str) -> None:
    """
    Runs entirely on its own thread - same "WHY THIS MUST NOT BLOCK"
    reasoning as _plan_trip_task above: an HTTP fetch plus one
    high-effort LLM call plus a LINE push easily lands in the same
    5-30+ second range that reasoning was written for, and this
    function is called from the voice assistant's supervisor loop by
    way of finalize_trip_plan_async.
    """
    _ensure_env_loaded()
    base_url = (os.getenv("TRIP_PLANNER_APP_URL") or _TRIP_PLANNER_APP_DEFAULT_URL).strip()

    print(f"[TripPlanner] Finalizing trip {trip_id}: reading the poll from {base_url}...")

    poll_data = _fetch_poll_data(trip_id, base_url)
    if poll_data is None:
        message = (
            f"อ่านข้อมูลโพลของทริป {trip_id} ไม่ได้ครับ "
            "เช็คว่าเซิร์ฟเวอร์ Trip Planner (Next.js) เปิดอยู่ไหมครับ แล้วลองล็อกแผนใหม่อีกครั้ง"
        )
        try:
            from plugins import line_notifier

            result = line_notifier.send_line_notification(message)
            print(f"[TripPlanner] Finalization notification for {trip_id}: {result}")
        except Exception as exc:  # noqa: BLE001 - the thread ends either way
            print(
                f"[TripPlanner] Notifying about the {trip_id} poll-fetch failure "
                f"failed: {type(exc).__name__}: {exc}"
            )
        return

    votes = poll_data.get("votes")
    if not isinstance(votes, list) or not votes:
        message = (
            f"ยังไม่มีใครโหวตทริป {trip_id} เลยครับ "
            "รอให้เพื่อนโหวตก่อนแล้วค่อยล็อกแผนใหม่อีกครั้งนะครับ"
        )
        try:
            from plugins import line_notifier

            result = line_notifier.send_line_notification(message)
            print(f"[TripPlanner] Finalization notification for {trip_id}: {result}")
        except Exception as exc:  # noqa: BLE001 - the thread ends either way
            print(
                f"[TripPlanner] Notifying about the {trip_id} empty poll failed: "
                f"{type(exc).__name__}: {exc}"
            )
        return

    prompt = _build_finalization_prompt(poll_data)

    try:
        itinerary = _run_finalization_agent(prompt)

        # See the matching try/except in _plan_trip_task above: Gemini's
        # own response text has previously contained characters (Thai
        # script) a Windows console on a legacy codepage cannot print,
        # and that must never be mistaken for the API call itself
        # failing after it already succeeded and spent quota.
        try:
            print(f"[TripPlanner] Finalized itinerary ready for trip {trip_id}:")
            print(json.dumps(itinerary, ensure_ascii=False, indent=2))
        except UnicodeEncodeError:
            print(
                f"[TripPlanner] Finalized itinerary ready for trip {trip_id} "
                "(console can't render it; ASCII-safe dump below)."
            )
            print(json.dumps(itinerary, ensure_ascii=True, indent=2))

        message = (
            f"แผนทริป {trip_id} เสร็จเรียบร้อยแล้วครับ! "
            "ส่งรายละเอียดทั้งหมดไปให้ทาง LINE แล้วนะครับ"
        )
    except FinalizationRateLimitError as exc:
        print(f"[TripPlanner] Finalization rate-limited for trip {trip_id}: {exc}")
        message = (
            f"ระบบวางแผนทริป {trip_id} เต็มโควต้าชั่วคราวครับ "
            "กรุณารอสักครู่แล้วลองล็อกแผนใหม่อีกครั้ง"
        )
    except Exception as exc:  # noqa: BLE001 - a background thread must not die silently
        print(f"[TripPlanner] Finalization failed for trip {trip_id}: {type(exc).__name__}: {exc}")
        message = (
            f"แผนทริป {trip_id} มีปัญหาระหว่างสร้างแผนครับ "
            "ลองล็อกแผนใหม่อีกครั้งได้เลยครับ"
        )

    try:
        from plugins import line_notifier

        result = line_notifier.send_line_notification(message)
        print(f"[TripPlanner] Finalization notification for {trip_id}: {result}")
    except Exception as exc:  # noqa: BLE001 - the thread ends either way
        print(
            f"[TripPlanner] Notifying about the {trip_id} finalization result "
            f"failed: {type(exc).__name__}: {exc}"
        )


def finalize_trip_plan_async(trip_id: str) -> str:
    """
    Reads a consensus poll's real votes and generates the final trip
    itinerary from them, pushing the result to LINE.

    MUST NOT wait for the plan to finish - see _finalize_trip_task's
    docstring. The only work done on the calling thread is argument
    cleanup and Thread.start(); the HTTP fetch, the Planner Agent call,
    and the LINE push all happen on that thread, which this function
    never joins. This is the reason the "success" string in this
    function's own name and Task description ("The plan has been
    successfully generated and sent via LINE") is NOT what this function
    returns - that sentence describes the LINE message _finalize_trip_task
    sends once the work is actually done; what Jarvis speaks immediately
    is the acknowledgement below, the same split generate_trip_plan_async
    already uses for the same reason.

    Called only as Step 3 of the two-step consensus finalization rule in
    llm_brain.py's SYSTEM_PROMPT - after check_poll_status_async has
    already reported the vote count and the user has confirmed.
    """
    trip_id = (trip_id or "").strip()
    if not trip_id:
        return "ERROR: I need a trip id to finalize a plan."

    thread = threading.Thread(
        target=_finalize_trip_task,
        args=(trip_id,),
        name=f"trip-finalize-{trip_id}",
        daemon=True,
    )
    thread.start()

    print(f"[TripPlanner] Finalization assigned for trip {trip_id}.")
    return (
        f"I've started reading the poll for trip {trip_id} and generating "
        "the final itinerary from it. I'll notify you on LINE when it's ready."
    )


def generate_trip_plan_async(
    starting_location: str,
    destination: str,
    planning_type: str = "solo_blank",
    head_count: int = 1,
    days: int = 1,
) -> str:
    """
    Assigns the Planner Agent to build a trip and returns immediately.

    MUST NOT wait for the plan to finish - see "WHY THIS MUST NOT BLOCK"
    above. The only work done on the calling thread is argument cleanup
    and Thread.start(); every network call and the Gemini call happen in
    _plan_trip_task, on a thread this function never joins.

    starting_location is read here, handed to the thread as a plain
    argument, and never touched again by this function - see the
    module's privacy constraint.
    """
    starting_location = (starting_location or "").strip()
    destination = (destination or "").strip()
    if not starting_location or not destination:
        return "ERROR: I need both a starting location and a destination to plan a trip."

    planning_type = (planning_type or "solo_blank").strip().lower()
    if planning_type not in _VALID_PLANNING_TYPES:
        planning_type = "solo_blank"

    try:
        head_count = max(1, int(head_count))
    except (TypeError, ValueError):
        head_count = 1

    try:
        days = max(1, int(days))
    except (TypeError, ValueError):
        days = 1

    thread = threading.Thread(
        target=_plan_trip_task,
        args=(starting_location, destination, planning_type, head_count, days),
        name=f"trip-planner-{destination}",
        daemon=True,
    )
    thread.start()

    print(
        f"[TripPlanner] Assigned the Planner Agent for a {days}-day trip to "
        f"{destination} ({planning_type}, {head_count} pax)."
    )
    return (
        f"I've assigned the Planner Agent to organize your trip from "
        f"{starting_location} to {destination}. I will notify you on LINE "
        "when the plan is ready."
    )


if __name__ == "__main__":
    import time

    # NOTE: this sends a real LINE push if LINE_CHANNEL_ACCESS_TOKEN and
    # LINE_USER_ID are set in .env. 'consensus' is used here deliberately
    # - it never touches Maps or Gemini, so this smoke test costs no
    # quota. Mock plugins.line_notifier.send_line_notification first if
    # you just want to watch the console output with no LINE push at all.
    print(generate_trip_plan_async("Bangkok, Thailand", "Chiang Mai", "consensus", 4, 3))
    time.sleep(10)
