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

WEB CHAT AND THE TRIP API
---------------------------
The Jarvis hub in TripPlanner_Web (api_server.py) is a text chat, and
"plan a trip to Chiang Mai" there has to END in a link the person can open,
which none of the flows above did (they push to LINE, or only log). When
request_context says the request came from the web chat,
generate_trip_plan_async therefore runs synchronously and RETURNS the link:

    'solo_blank'   POST {app}/api/trip/create (HMAC-signed, body-bound - see
                   _post_trip_create) plans the whole trip right now,
                   personalized from the signed-in person's Taste Profile
                   (TasteSwipe), stores it and answers with its page. The
                   tool result carries that link, and the model repeats it.
    'consensus'    the poll is created as before, but its public voting link
                   comes back in the chat. The private ORGANISER link (it
                   carries the admin token, which locks the plan) is still
                   pushed to LINE and NEVER put in the chat: the chat is
                   stored in the database, and the whole point of storing only
                   a hash of the token (lib/adminToken.ts) is that the raw
                   value exists nowhere but in that one private message.
    'solo_draft'   the draft-board link comes back in the chat.

{app} is the origin the hub sent (resolve_api_base_url), not a fixed
deployment: the instance the person is looking at is the one that has the
route and the database their account lives in.

The voice assistant is unchanged in shape - it has no signed-in person and no
screen, so it still starts a background thread and pushes to LINE - except that
'solo_blank' now asks the same /api/trip/create for a real plan and sends its
link, instead of logging a Gemini itinerary and promising a link "in the next
phase". Without TRIP_API_SECRET_KEY that old Gemini path still runs.

FINALIZING A POLL
-------------------
finalize_trip_plan_async asks the app to lock the poll and generate its plan
from the votes (POST {app}/api/poll/<id>/finalize, signed like the create
call). It used to make a plan of its own here - a taste-blind JSON itinerary
that was printed to the console and never stored, announced over LINE as if
it had been sent - which is gone: the plan is the app's own grounded one
(real venues, travel times, the voters' Taste Profiles), stored there and
answered with its link. From the web chat the call is synchronous and the link
is the tool result; from the microphone it runs on a thread and the link goes
to LINE, as everything else voice does.

WHO may lock a poll this way is the app's decision, from Poll.createdByUserId,
and the request only says who is asking: a poll made from the web chat (see
_create_poll_admin_token, which records the creator) can be locked from the
chat only by the person who made it, and a poll made by voice can be locked by
voice (no user id - the owner at the microphone), never the other way round.
The poll's voting link is public, so without that rule any signed-in person
could lock someone else's poll just by asking Jarvis. The organizer's own admin
link (POST /api/trigger-jarvis) is unaffected and still works for every poll.

PRIVACY CONSTRAINT - READ BEFORE TOUCHING starting_location
-------------------------------------------------------------
starting_location is the user's home, hotel, or wherever they currently
are. It is CRITICAL that this never becomes a tracked value:

    * generate_trip_plan_async takes no user or session id as an argument,
      so there is nothing to key a stored value against even by accident.
      The one place that CAN see who is asking is the web chat's request
      context (see "WEB CHAT AND THE TRIP API" below), which only attributes
      a trip to its creator - the web branch never receives
      starting_location at all, and the model is no longer even offered it.
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
import secrets
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

# A transitive dependency of google-genai already (it vendors it for its
# own request/response models), so this is only ever missing if that SDK
# is not installed either - a state _run_planner_agent already reports as
# "google-genai is not installed." Guarded the same way as every other
# SDK import below, so importing this module (which llm_brain.py does
# unconditionally at startup) can never crash Jarvis's boot over a
# missing optional package.
try:
    import pydantic
except ImportError:  # pragma: no cover - environment-dependent
    pydantic = None  # type: ignore[assignment]

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

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - environment-dependent
    load_dotenv = None  # type: ignore[assignment]

import request_context
from plugins.trip_planner_config import build_liff_link, get_app_base_url, resolve_api_base_url


class GeminiRateLimitError(Exception):
    """Gemini returned HTTP 429 - a quota to wait out, not a bug to report."""


_VALID_PLANNING_TYPES = frozenset({"solo_blank", "solo_draft", "consensus"})

# The most days the Trip Planner app will plan (lib/tripDates.ts's
# MAX_TRIP_DAYS; it clamps again on its side). There is deliberately no default
# length: a trip whose length the user never gave is asked about, not planned.
MAX_PLAN_DAYS = 14


def _positive_int(value: object) -> int | None:
    """
    `value` as a whole number of at least 1, or None when it is missing or not
    one - the model sending null, 0, "" or words. A bool is not a count.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None
    return number if number >= 1 else None

# (connect, read) seconds for the calls that make the Trip Planner app run a
# whole generation before it answers - POST /api/trip/create and POST
# /api/poll/<id>/finalize (two LLM calls, Places, Routes: ~15-30 s measured,
# capped at the app's 60 s maxDuration). Unlike every other call here, so the
# read timeout is generous - the web chat's own limit, lib/jarvisBrain.ts's
# BRAIN_TIMEOUT_MS, has to cover this plus the model's turns around it.
_PLAN_API_TIMEOUT = (5.0, 100.0)

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


if pydantic is not None:

    class ItineraryDay(pydantic.BaseModel):
        day: int
        summary: str
        activities: list[str]
        meals: list[str]

    class Itinerary(pydantic.BaseModel):
        """
        The shape solo_blank's Planner Agent call (_run_planner_agent,
        the Gemini fallback when the Trip Planner app is not configured)
        must produce. Passed to Gemini as response_schema (structured
        output), so the provider returns data conforming to this shape at
        the API level, rather than "please reply with only a JSON object"
        prose the model could ignore or wrap in markdown fences.
        _validate_itinerary below is still the single place this gets
        checked, since structured output is the provider's best effort,
        not a guarantee this project controls.

        Polls are not planned here: finalize_trip_plan_async asks the Trip
        Planner app to lock and generate them (POST /api/poll/<id>/finalize),
        and that app's own, richer itinerary (real venues, travel times,
        costs) is stored there - see TripPlanner_Web/lib/itinerary.ts.
        """

        destination: str
        days: list[ItineraryDay]
        notes: str

else:  # pragma: no cover - see the pydantic import guard above
    ItineraryDay = None  # type: ignore[assignment,misc]
    Itinerary = None  # type: ignore[assignment,misc]


def _validate_itinerary(data: Any) -> dict[str, Any]:
    """
    Validates an already-parsed itinerary (the dict from json.loads of
    Gemini's reply) against Itinerary and returns it as a plain dict.

    Raises pydantic.ValidationError (a ValueError subclass) on anything
    that doesn't match - the caller's broad `except Exception` already
    treats that as "planning failed," same as the json.JSONDecodeError
    this replaces.
    """
    if Itinerary is None:
        raise RuntimeError("pydantic is not installed.")
    return Itinerary.model_validate(data).model_dump()


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
    if Itinerary is None:
        raise RuntimeError("pydantic is not installed.")

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
                # Structured output: Gemini is constrained to return JSON
                # matching Itinerary's shape at the API level, rather than
                # via the prompt's "respond with ONLY a JSON object"
                # instruction alone, which it has been known to ignore by
                # wrapping the reply in ```json fences anyway.
                response_mime_type="application/json",
                response_schema=Itinerary,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - reclassified immediately below
        if isinstance(exc, genai_errors.APIError) and getattr(exc, "code", None) == 429:
            raise GeminiRateLimitError(str(exc)) from exc
        raise

    return _validate_itinerary(json.loads(response.text))


# ---------------------------------------------------------------------
# 'consensus' and 'solo_draft' - defer planning; see the module docstring
# ---------------------------------------------------------------------


@dataclass
class _ConsensusPoll:
    """What _open_consensus_poll managed to do, so a caller can tell the user the truth."""

    trip_id: str
    # The admin credential was stored on the web app. False means nobody can
    # ever lock this poll - the poll row may not even exist.
    admin_stored: bool
    # Each LINE message actually went out (the public link to forward; the
    # private organiser link).
    public_sent: bool = False
    admin_sent: bool = False


def _open_consensus_poll(
    destination: str, days: int, base_url: str, created_by_user_id: str = ""
) -> _ConsensusPoll:
    """
    The whole job of a 'consensus' trip: create a poll, mint its admin
    token, and send two LINE messages - the public voting link (safe to
    forward into a friend group) and a private admin link that alone can
    later trigger "Lock & Generate Plan" on TripPlanner_Web (see
    app/api/trigger-jarvis/route.ts). Not LINE identity: that proved
    unreliable throughout this project, and a plain voter link was never
    actually distinguishable from an organizer's anyway.

    days is the trip length the user actually asked Jarvis for -
    forwarded to _create_poll_admin_token, which persists it to
    Poll.durationDays server-side (see that function's docstring). It no
    longer rides along as a URL param on the admin link: an earlier
    version did that instead, but a value that lived only in the URL
    reset to the web app's hardcoded 5-day default on every page reload
    or friend link, since nothing about it was actually saved anywhere.
    Before that URL-param version, this was dropped entirely for
    consensus trips regardless of what the user asked for by voice.

    No Geocoding, Places, or Gemini call happens here - see "PLANNING_
    TYPE FORKS BEFORE ANY DATA GATHERING" in the module docstring for
    why the real itinerary waits for a future webhook-driven
    continuation instead of being generated now.

    Runs on the voice assistant's background thread (_run_consensus_poll)
    and, synchronously, for the web chat (_plan_trip_for_web), which wants the
    result to say what reached LINE. Only the LINE pushes are guarded: a
    link that cannot be built (see assert_link_not_localhost_in_production)
    raises, as it always has.

    `created_by_user_id` is the signed-in person whose chat asked for the poll
    ("" for voice): the web app records it so that person - and only that
    person - can later lock the poll from the chat (finalize_trip_plan_async).
    """
    trip_id = str(uuid.uuid4())
    poll_url = build_liff_link(f"/trip/poll/{trip_id}")

    admin_token = secrets.token_urlsafe(32)
    admin_token_stored = _create_poll_admin_token(
        trip_id, admin_token, base_url, days, created_by_user_id
    )
    poll = _ConsensusPoll(trip_id=trip_id, admin_stored=admin_token_stored)

    try:
        from plugins import line_notifier

        message = (
            f"สร้างแบบสอบถามสำหรับทริป {destination} เรียบร้อยแล้วครับ "
            f"รบกวน Forward ลิงก์นี้เข้ากลุ่มเพื่อนเพื่อโหวตได้เลยครับ: {poll_url}"
        )
        result = line_notifier.send_line_notification(message)
        print(f"[TripPlanner] Consensus poll notification for {destination}: {result}")
        poll.public_sent = not str(result).startswith("ERROR")

        if admin_token_stored:
            admin_url = build_liff_link(f"/trip/poll/{trip_id}?admin={admin_token}")
            admin_message = (
                "ลิงก์นี้สำหรับบอสคนเดียวนะครับ ห้าม Forward ต่อเด็ดขาด - "
                f"ใช้กด Lock & Generate Plan ตอนโหวตครบแล้วครับ: {admin_url}"
            )
            admin_result = line_notifier.send_line_notification(admin_message)
            print(f"[TripPlanner] Consensus poll admin link for {destination}: {admin_result}")
            poll.admin_sent = not str(admin_result).startswith("ERROR")
        else:
            # The admin credential was never stored server-side - sending
            # the link anyway would hand out something that LOOKS like
            # admin access but can never actually pass verifyAdminToken
            # (lib/adminToken.ts), which is worse than not sending it at
            # all. See _create_poll_admin_token's docstring for failure
            # causes (TRIP_API_SECRET_KEY missing, TripPlanner_Web
            # unreachable, ...).
            print(
                f"[TripPlanner] Skipped sending an admin link for {destination}'s poll - "
                "the admin token could not be stored server-side."
            )
    except Exception as exc:  # noqa: BLE001 - the caller ends either way
        print(
            f"[TripPlanner] Notifying about the {destination} poll failed: "
            f"{type(exc).__name__}: {exc}"
        )
    return poll


def _run_consensus_poll(destination: str, days: int) -> None:
    """'consensus'`s entire background job for the voice assistant - see _open_consensus_poll."""
    _open_consensus_poll(destination, days, get_app_base_url())


def _run_solo_draft(destination: str) -> None:
    """
    'solo_draft'`s entire background job: create a draft-board link and
    stop.

    No Geocoding, Places, or Gemini call happens here either - the
    traveller already has specific places in mind and wants to add them
    personally rather than have the Planner Agent invent an itinerary
    from nothing. Sends exactly one LINE message and returns.
    """
    draft_url = build_liff_link(f"/trip/draft/{uuid.uuid4()}")
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
        solo_blank   _run_solo_trip_via_api when the Trip Planner app is
                     configured (the plan is made there; one LINE message
                     with its link), otherwise Steps 1-6 below: gather
                     data, call the Planner Agent, notify. The Gemini
                     steps are the only path that spends Maps or Gemini
                     quota.

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
        _run_consensus_poll(destination, days)
        return

    if planning_type == "solo_draft":
        _run_solo_draft(destination)
        return

    # planning_type == "solo_blank" from here on. With the Trip Planner app
    # reachable (TRIP_API_SECRET_KEY set) the plan is made THERE and the user
    # gets its link; the Gemini steps below only run when it is not configured.
    if _trip_api_configured():
        _run_solo_trip_via_api(destination, head_count, days)
        return

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


_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _extract_trip_id(text: str) -> str:
    """
    The trip id in what the model or user passed: the id itself, or a poll
    link that contains it. People paste links into the chat ("is
    https://.../trip/poll/<id> done?"), and the id is all that matters in
    them - anything else in the link (an ?admin= token) is dropped, not kept.
    Text with no UUID in it is returned stripped and unchanged.
    """
    text = (text or "").strip()
    found = _UUID_RE.search(text)
    return found.group(0).lower() if found else text


def _clean_vibe(vibe: Any) -> str:
    """'🛍️ Shopping' -> 'Shopping': the poll's vibe labels start with an emoji a spoken reply would mangle."""
    return re.sub(r"[^\w\s/&-]", "", str(vibe)).strip()


def _describe_poll_status(trip_id: str, poll_data: dict[str, Any]) -> str:
    """
    One sentence about a poll, from the payload GET /api/poll/<id> returns
    (`summary.totalVotes`, `summary.topVibes`, `locked`) - what
    check_poll_status_async hands the model to say out loud.
    """
    summary = poll_data.get("summary")
    if not isinstance(summary, dict):
        summary = {}

    try:
        total = int(summary.get("totalVotes") or 0)
    except (TypeError, ValueError):
        total = 0

    top = [
        _clean_vibe(entry.get("vibe"))
        for entry in (summary.get("topVibes") or [])
        if isinstance(entry, dict) and entry.get("vibe")
    ]
    top = [vibe for vibe in top if vibe][:3]

    if poll_data.get("locked") is True:
        return (
            f"The poll for trip {trip_id} is already locked and its plan has been "
            "generated, so there is nothing left to finalize."
        )
    if total == 0:
        return f"Nobody has voted on trip {trip_id} yet."

    who = "1 person has" if total == 1 else f"{total} people have"
    sentence = f"{who} voted on trip {trip_id} so far."
    if top:
        sentence += f" The top vibes are {', '.join(top)}."
    return sentence


def check_poll_status_async(trip_id: str) -> str:
    """
    Reports how a consensus poll is going, ahead of finalizing a plan.

    This is Step 1 of the two-step consensus finalization rule in
    llm_brain.py's SYSTEM_PROMPT: the model must call this, speak the
    count, and get the user's explicit confirmation before ever calling
    finalize_trip_plan_async to actually generate a plan for a trip that
    already has a poll running. Named with the _async suffix for
    consistency with generate_trip_plan_async, but there is no thread
    here - the model needs this answer synchronously to speak it and ask
    for confirmation (see background=False on its ToolSpec in
    llm_brain.py), so it returns its result directly rather than
    backgrounding anything.

    Reads the poll for real: a signed GET /api/poll/<trip_id> on the web app
    the request came from (the same call finalize_trip_plan_async makes).
    This used to return a FIXED mock string ("4 users joined...") whatever
    the trip, so the user was asked to confirm a vote count nobody had
    counted. One short HTTP read (3 s connect, 10 s read) is the price of
    the count being true; if it cannot be read, that is said plainly instead.
    """
    trip_id = _extract_trip_id(trip_id)
    if not trip_id:
        return "ERROR: I need a trip id to check the poll status."

    context = request_context.current()
    base_url = resolve_api_base_url(context.app_url if context else "")
    poll_data = _fetch_poll_data(trip_id, base_url)
    if not isinstance(poll_data, dict):
        return (
            f"ERROR: I couldn't read the poll for trip {trip_id} from the Trip Planner app "
            f"at {base_url} (the console says why). Tell the user plainly; do not invent a vote count."
        )
    return _describe_poll_status(trip_id, poll_data)


# ---------------------------------------------------------------------
# Signed calls to the Trip Planner app. finalize_trip_plan_async (below)
# is Step 3 of the two-step consensus finalization rule (see the
# SYSTEM_PROMPT rule in llm_brain.py): called only after the user has
# confirmed following check_poll_status_async above.
# ---------------------------------------------------------------------


def _sign_request(
    method: str, path: str, secret: str, body: Optional[bytes] = None
) -> tuple[str, str]:
    """
    (ts, sig) for an HMAC-authenticated call to TripPlanner_Web's Next.js
    API - must match the format the corresponding route's
    verifyHmacSignature reconstructs byte-for-byte: "{ts}.{method}.{path}",
    HMAC-SHA256, hex. TRIP_API_SECRET_KEY here and API_SECRET_KEY on the
    Next.js side are two env var names for the one shared secret - both
    must hold the identical value or every signature will mismatch.

    With `body` (the exact bytes about to be sent) the signed string also
    ends in ".{sha256(body) as hex}" - lib/serverAuth.ts on the Next.js side.
    Used where the body carries authority - the user a trip is for (POST
    /api/trip/create), who is asking to lock a poll (POST
    /api/poll/<id>/finalize), who created a poll (POST
    /api/poll/<id>/admin-token with createdByUserId): a captured signature
    must not be replayable with a different one. The older calls sign no body
    and are made without it.
    """
    ts = str(int(time.time()))
    message = f"{ts}.{method}.{path}"
    if body is not None:
        message += f".{hashlib.sha256(body).hexdigest()}"
    sig = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    return ts, sig


def _sign_poll_request(trip_id: str, secret: str) -> tuple[str, str]:
    """(ts, sig) for GET /api/poll/<trip_id> - see _sign_request."""
    return _sign_request("GET", f"/api/poll/{trip_id}", secret)


def _create_poll_admin_token(
    trip_id: str, admin_token: str, base_url: str, days: int, created_by_user_id: str = ""
) -> bool:
    """
    POST {base_url}/api/poll/<trip_id>/admin-token - mints this trip's
    admin credential for "Lock & Generate Plan" (see
    app/api/trigger-jarvis/route.ts and lib/adminToken.ts). Sends the RAW
    admin_token over this HMAC-authenticated, server-to-server channel;
    the Next.js route hashes it before ever writing anything to
    Postgres, so the raw value only ever exists here and in the LINE
    message _run_consensus_poll sends right after this call succeeds -
    never in a database.

    Also sends `days` - the trip length the user requested by voice -
    which the route persists to Poll.durationDays. That's now the ONLY
    place this value is captured for a consensus trip: it used to also
    ride along as a ?days=<n> URL param on the admin link, but that reset
    to the hardcoded default on every page reload or friend link since
    nothing about it was actually saved server-side.

    `created_by_user_id` is the signed-in person whose web chat is creating
    the poll ("" for a poll made by voice). The route stores it as
    Poll.createdByUserId, which is what later lets that person - and nobody
    else - lock the poll from the chat (finalize_trip_plan_async). Because it
    carries authority, a request that names a creator is signed over its BODY
    (see _sign_request); a poll made by voice sends no creator and keeps the
    path-only signature it always had, byte for byte.

    Returns False on ANY failure: missing requests package, missing
    TRIP_API_SECRET_KEY, connection refused, timeout, or a non-2xx
    status. The caller MUST treat False as "do not send an admin link" -
    a link whose token was never actually stored would look valid but
    always be rejected by verifyAdminToken, silently locking the trip
    creator out of their own poll.
    """
    if requests is None:
        print("[TripPlanner] admin-token: the requests package is not installed.")
        return False

    secret = (os.getenv("TRIP_API_SECRET_KEY") or "").strip()
    if not secret:
        print("[TripPlanner] admin-token: TRIP_API_SECRET_KEY is not configured.")
        return False

    path = f"/api/poll/{trip_id}/admin-token"
    if created_by_user_id:
        # The exact bytes that are signed are the exact bytes that are sent.
        body = json.dumps(
            {"adminToken": admin_token, "days": days, "createdByUserId": created_by_user_id},
            separators=(",", ":"),
        ).encode("utf-8")
        ts, sig = _sign_request("POST", path, secret, body)
        request_args: dict[str, Any] = {
            "data": body,
            "headers": {"Content-Type": "application/json", "X-Ts": ts, "X-Sig": sig},
        }
    else:
        ts, sig = _sign_request("POST", path, secret)
        request_args = {
            "json": {"adminToken": admin_token, "days": days},
            "headers": {"X-Ts": ts, "X-Sig": sig},
        }

    try:
        response = requests.post(f"{base_url}{path}", timeout=(3, 10), **request_args)
        response.raise_for_status()
    except requests.exceptions.RequestException as exc:
        print(
            f"[TripPlanner] admin-token: request failed ({base_url}{path}): "
            f"{type(exc).__name__}: {exc}"
        )
        return False

    return True


def _fetch_poll_data(trip_id: str, base_url: str) -> Optional[dict[str, Any]]:
    """
    GET {base_url}/api/poll/<trip_id> - the Next.js "Data Bridge" this
    tool reads consensus votes through (see app/api/poll/[id]/route.ts),
    authenticated with an HMAC signature: a request that presents
    X-Ts/X-Sig is verified in full, one without is an ordinary anonymous read.
    Used by check_poll_status_async.

    Returns None on ANY failure: missing requests package, missing
    secret, connection refused (the Next.js dev server isn't running),
    timeout, a non-2xx status (including a 401 from a bad/expired
    signature), or a body that isn't valid JSON. The caller treats None as
    "say plainly that the poll could not be read", never as "proceed with
    made-up numbers".
    """
    if requests is None:
        print("[TripPlanner] poll-read: the requests package is not installed.")
        return None

    secret = (os.getenv("TRIP_API_SECRET_KEY") or "").strip()
    if not secret:
        print("[TripPlanner] poll-read: TRIP_API_SECRET_KEY is not configured.")
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
        print(f"[TripPlanner] poll-read: poll fetch failed ({url}): {type(exc).__name__}: {exc}")
        return None

    try:
        return response.json()
    except ValueError as exc:
        print(f"[TripPlanner] poll-read: poll response from {url} was not valid JSON: {exc}")
        return None


# ---------------------------------------------------------------------
# The Trip Planner web app as the planner - POST /api/trip/create
# ---------------------------------------------------------------------


class TripApiError(Exception):
    """
    The Trip Planner app could not plan the trip. str(exc) is a plain
    reason, safe to hand to the model and the user (it never contains the
    secret).
    """


def _log(message: str) -> None:
    """
    print() that cannot raise on the text it is given.

    The log lines of the web-chat path carry what the USER typed (a
    destination) and what the web app answered (an error that quotes it), and
    either can be Thai. A Windows console on a legacy codepage - or stdout
    redirected to a file - raises UnicodeEncodeError on that, and the
    module's own notes record exactly that failure after a call had already
    succeeded. Here it would fail a tool call over a log line, so anything the
    stream cannot encode is written as a \\uXXXX escape instead.
    """
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    print(message.encode(encoding, errors="backslashreplace").decode(encoding))


def _trip_api_configured() -> bool:
    """True when this process can sign calls to the Trip Planner app at all."""
    _ensure_env_loaded()
    return requests is not None and bool((os.getenv("TRIP_API_SECRET_KEY") or "").strip())


def _explain_api_failure(status: int, data: dict[str, Any], base_url: str, what: str) -> str:
    """
    What an unexpected answer from a signed call to the Trip Planner app means,
    in a sentence. `what` names the route ("trip-creation", "poll-finalize").
    The app's own words are passed on when it gave any: they are the most
    specific thing available ("No real places were found for ...", "Only the
    person who created this poll ...").
    """
    message = data.get("error") if isinstance(data.get("error"), str) else ""
    message = message.strip().rstrip(".")

    if status in (401, 503):
        return (
            f"{message or 'the Trip Planner app rejected the request'} "
            "(TRIP_API_SECRET_KEY in Jarvis's .env must equal API_SECRET_KEY in TripPlanner_Web/.env.local)"
        )
    if status == 404 and not message:
        # Next's own HTML 404 page: no JSON, no "error" field.
        return (
            f"the Trip Planner app at {base_url} has no {what} route, so it is an "
            "older version (restart it, or deploy the latest)"
        )
    if message:
        return message
    return f"the Trip Planner app answered HTTP {status}"


def _signed_post(
    base_url: str,
    path: str,
    payload: dict[str, Any],
    *,
    what: str,
    ok_statuses: tuple[int, ...],
) -> tuple[int, dict[str, Any]]:
    """
    POST {base_url}{path} with `payload` as the JSON body and a signature that
    covers that body (see _sign_request), for the calls that make the Trip
    Planner app run a whole generation before it answers (hence
    _PLAN_API_TIMEOUT). Returns (status, data) when the app answers with one of
    `ok_statuses`; raises TripApiError for everything else - not configured,
    unreachable, timed out, or any other answer, with the app's own reason
    passed on (see _explain_api_failure). `what` names the route in messages.

    Blocks for as long as the generation takes (~15-30 s), so only the web
    chat's synchronous path and background threads call it.
    """
    _ensure_env_loaded()
    if requests is None:
        raise TripApiError("the requests package is not installed on this machine")
    secret = (os.getenv("TRIP_API_SECRET_KEY") or "").strip()
    if not secret:
        raise TripApiError(
            "TRIP_API_SECRET_KEY is not set in Jarvis's .env, so Jarvis cannot call the Trip Planner app "
            "(it must equal API_SECRET_KEY in TripPlanner_Web/.env.local)"
        )

    # The exact bytes that are signed are the exact bytes that are sent -
    # json=payload would let requests serialize it a second time, differently.
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ts, sig = _sign_request("POST", path, secret, body)

    try:
        response = requests.post(
            f"{base_url}{path}",
            data=body,
            headers={"Content-Type": "application/json", "X-Ts": ts, "X-Sig": sig},
            timeout=_PLAN_API_TIMEOUT,
        )
    except requests.exceptions.Timeout as exc:
        _log(f"[TripPlanner] {what}: timed out ({base_url}{path}): {exc}")
        raise TripApiError(f"the Trip Planner app took too long to answer the {what} request") from exc
    except requests.exceptions.RequestException as exc:
        _log(f"[TripPlanner] {what}: request failed ({base_url}{path}): {type(exc).__name__}: {exc}")
        raise TripApiError(f"the Trip Planner app at {base_url} is not reachable (is it running?)") from exc

    try:
        data = response.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        data = {}

    if response.status_code not in ok_statuses:
        _log(
            f"[TripPlanner] {what}: HTTP {response.status_code} from {base_url}{path}: "
            f"{str(data.get('error') or response.text[:200])[:200]}"
        )
        raise TripApiError(_explain_api_failure(response.status_code, data, base_url, what))

    return response.status_code, data


def _post_trip_create(
    base_url: str, user_id: str, destination: str, days: int, head_count: int
) -> dict[str, Any]:
    """
    POST {base_url}/api/trip/create - asks the Trip Planner app to plan ONE
    trip to `destination` right now (the pipeline in
    TripPlanner_Web/lib/tripGeneration.ts, personalized from `user_id`'s Taste
    Profile when there is one) and store it.

    Authenticated like the admin-token call, with one addition: the signature
    also covers the request body (see _sign_request), because the body names
    the user the trip is for. `user_id` "" plans for nobody in particular
    (the voice assistant).

    Returns the app's answer - {tripId, path, destination, days, stops,
    personalized, taste} - and raises TripApiError for every failure: not
    configured, unreachable, timed out, or any non-201 answer (the app's own
    reason is passed on: no real places for that destination, rate-limited...).
    """
    payload: dict[str, Any] = {"destination": destination, "days": days, "headCount": head_count}
    if user_id:
        payload["userId"] = user_id
    _, data = _signed_post(base_url, "/api/trip/create", payload, what="trip-creation", ok_statuses=(201,))

    trip_id, page = data.get("tripId"), data.get("path")
    if not isinstance(trip_id, str) or not isinstance(page, str) or not page.startswith("/trip/poll/"):
        raise TripApiError("the Trip Planner app answered, but not with a trip link")
    return data


def _post_poll_finalize(base_url: str, trip_id: str, user_id: str) -> dict[str, Any]:
    """
    POST {base_url}/api/poll/<trip_id>/finalize - asks the Trip Planner app to
    lock the poll and generate its plan from the votes (lib/pollLock.ts,
    personalized from the voters' Taste Profiles) without the organizer's admin
    token.

    `user_id` is the signed-in person asking, from the web chat, and "" from
    the microphone. The app decides whether that caller may lock THIS poll
    (Poll.createdByUserId: a poll made in the chat is lockable from the chat by
    its creator only, a poll made by voice by voice only) and answers 403 with
    its reason otherwise; Jarvis only reports who is asking.

    Returns the app's answer - {tripId, path, locked, alreadyLocked,
    destination, days, stops, and for a fresh plan voters, tasteVoters,
    personalized, taste} - and raises TripApiError for every failure: not
    configured, unreachable, timed out, not allowed, no such poll, no votes,
    already being generated (409), rate-limited...
    """
    payload: dict[str, Any] = {"userId": user_id} if user_id else {}
    _, data = _signed_post(
        base_url, f"/api/poll/{trip_id}/finalize", payload, what="poll-finalize", ok_statuses=(200, 201)
    )

    page = data.get("path")
    if data.get("tripId") != trip_id or not isinstance(page, str) or not page.startswith("/trip/poll/"):
        raise TripApiError("the Trip Planner app answered, but not with the poll's plan link")
    return data


def _describe_created_trip(data: dict[str, Any], url: str, requested_days: int, has_user: bool) -> str:
    """
    The tool result for a plan the app made: what the model needs to answer
    in a sentence or two - that it is ready, the link, and how personal it
    is. The link is the point of the answer, so the instruction to repeat
    it exactly comes with it.
    """
    destination = str(data.get("destination") or "that destination")
    try:
        days = int(data.get("days") or requested_days)
    except (TypeError, ValueError):
        days = requested_days
    stops = data.get("stops") if isinstance(data.get("stops"), int) else 0

    taste = data.get("taste") if isinstance(data.get("taste"), dict) else {}
    loved = [str(label) for label in (taste.get("loved") or [])]

    if data.get("personalized") is True:
        taste_note = "It was tuned to this person's saved taste profile"
        if loved:
            taste_note += f": they like {', '.join(loved)}"
        taste_note += "."
    elif has_user:
        taste_note = (
            "This person has no usable taste profile saved yet, so it is a general plan; "
            "finishing the taste quiz in the Trip Planner app personalizes future ones."
        )
    else:
        taste_note = "No signed-in account came with this request, so it is a general plan."

    capped = ""
    if days < requested_days:
        capped = f" The planner's longest trip is {MAX_PLAN_DAYS} days, so that is how many it made."

    return (
        f"DONE: the {days}-day trip plan for {destination} is ready"
        f"{f' ({stops} stops)' if stops else ''}. Link: {url}\n"
        f"{taste_note}{capped}\n"
        "Tell the user it is ready in one or two short sentences and include the link exactly as written "
        "above - never shorten, rewrite or invent a link. No travel dates are set; say so only if they ask."
    )


def _plan_trip_for_web(
    context: request_context.RequestContext,
    destination: str,
    planning_type: str,
    head_count: int,
    days: int,
) -> str:
    """
    generate_trip_plan_async for a request from the web chat: synchronous,
    and it returns the LINK - see "WEB CHAT AND THE TRIP API" in the module
    docstring. Every failure becomes an "ERROR: ..." result the model reads
    and relays; nothing here raises.
    """
    base_url = resolve_api_base_url(context.app_url)

    if planning_type == "solo_draft":
        # A draft board is only a link until the person pastes something into
        # it (no row exists before that), exactly as in _run_solo_draft.
        draft_url = f"{base_url}/trip/draft/{uuid.uuid4()}"
        _log(f"[TripPlanner] Web draft board for {destination}: {draft_url}")
        return (
            f"DONE: a draft board for the {destination} trip is ready. Link: {draft_url}\n"
            "Tell the user in one short sentence that they can paste the places they like there, and "
            "include the link exactly as written above."
        )

    if planning_type == "consensus":
        try:
            poll = _open_consensus_poll(destination, days, base_url, context.user_id)
        except Exception as exc:  # noqa: BLE001 - e.g. a localhost link refused in production
            _log(f"[TripPlanner] Web poll for {destination} failed: {type(exc).__name__}: {exc}")
            return (
                f"ERROR: the poll for the {destination} trip could not be created ({type(exc).__name__}). "
                "Tell the user plainly; do not claim a poll exists and do not invent a link."
            )
        if not poll.admin_stored:
            return (
                f"ERROR: the poll for the {destination} trip could not be created - the Trip Planner app "
                f"at {base_url} did not accept it (the console says why; usually TRIP_API_SECRET_KEY and "
                "API_SECRET_KEY differ, or the app is not running). Tell the user plainly; do not claim a "
                "poll exists and do not invent a link."
            )
        poll_url = f"{base_url}/trip/poll/{poll.trip_id}"
        # The raw admin token is in the private LINE message only - never in
        # this result, because the model repeats results and the chat is stored.
        # Locking later has TWO ways, and the result must say so: the organizer
        # link in LINE, and asking Jarvis right here - the app lets the person
        # who made the poll in this chat lock it (finalize_trip_plan_async).
        if poll.admin_sent:
            line_note = (
                "The voting link to forward to the group and the private organizer link (which locks the plan) "
                "were sent to LINE."
            )
        else:
            line_note = (
                "The private organizer link could NOT be sent to LINE (check the LINE settings), so the person "
                "cannot use it - but they can still lock the poll from this chat."
            )
        return (
            f"DONE: a voting poll for the {destination} trip is ready. Link: {poll_url}\n"
            f"{line_note}\n"
            "Once the votes are in, the person can ask you to finalize this poll and you will lock it from this "
            "chat (check the vote count first and ask them to confirm).\n"
            "Tell the user in one or two short sentences, and include the link exactly as written above."
        )

    # solo_blank: the app plans the whole trip now.
    try:
        data = _post_trip_create(base_url, context.user_id, destination, days, head_count)
    except TripApiError as exc:
        _log(f"[TripPlanner] Web trip to {destination} failed: {exc}")
        return (
            f"ERROR: the trip to {destination} could not be planned - {exc}. "
            "Tell the user plainly what went wrong; do not claim a plan exists and do not invent a link."
        )
    url = f"{base_url}{data['path']}"
    _log(f"[TripPlanner] Web trip to {destination} ready: {url}")
    return _describe_created_trip(data, url, days, bool(context.user_id))


def _run_solo_trip_via_api(destination: str, head_count: int, days: int) -> None:
    """
    'solo_blank' for the voice assistant when the Trip Planner app is
    configured: the plan is made by the app (POST /api/trip/create) and one
    LINE message carries its link. Runs on _plan_trip_task's background
    thread; the user is not waiting, so a failure is reported by LINE too,
    never silently. Plans for nobody in particular - a voice assistant has no
    signed-in account whose Taste Profile could be used.
    """
    base_url = resolve_api_base_url("")
    try:
        data = _post_trip_create(base_url, "", destination, days, head_count)
        link = build_liff_link(str(data["path"]))
        message = (
            f"แผนทริป {destination} จำนวน {data.get('days', days)} วัน ของคุณเสร็จแล้วครับ! "
            f"ดูแผนได้ที่นี่: {link}"
        )
    except Exception as exc:  # noqa: BLE001 - TripApiError, or a link refused in production
        _log(f"[TripPlanner] Solo trip to {destination} failed: {type(exc).__name__}: {exc}")
        message = (
            f"แผนทริป {destination} มีปัญหาระหว่างการวางแผนครับ ({exc}) "
            "ลองสั่งให้วางแผนใหม่อีกครั้งได้เลยครับ"
        )

    try:
        from plugins import line_notifier

        result = line_notifier.send_line_notification(message)
        _log(f"[TripPlanner] Solo trip notification for {destination}: {result}")
    except Exception as exc:  # noqa: BLE001 - the thread ends either way
        _log(
            f"[TripPlanner] Notifying about the {destination} trip failed: "
            f"{type(exc).__name__}: {exc}"
        )


def _describe_finalized_poll(data: dict[str, Any], url: str) -> str:
    """
    The tool result for a poll the app locked: what the model needs to answer
    in a sentence or two - that it is locked and ready, the link, and what the
    plan was built from. The link is the point of the answer, so the instruction
    to repeat it exactly comes with it.
    """
    destination = str(data.get("destination") or "the trip")
    days = data.get("days") if isinstance(data.get("days"), int) else 0
    stops = data.get("stops") if isinstance(data.get("stops"), int) else 0
    size = f" ({days} days, {stops} stops)" if days and stops else ""

    if data.get("alreadyLocked") is True:
        head = f"DONE: the poll for {destination} was already locked{size}. Link: {url}"
        note = "Nothing new was generated: this is the plan that already exists."
    else:
        head = f"DONE: the poll for {destination} is locked and its plan is ready{size}. Link: {url}"
        voters = data.get("voters") if isinstance(data.get("voters"), int) else 0
        taste_voters = data.get("tasteVoters") if isinstance(data.get("tasteVoters"), int) else 0
        taste = data.get("taste") if isinstance(data.get("taste"), dict) else {}
        loved = [str(label) for label in (taste.get("loved") or [])]

        if data.get("personalized") is True:
            note = (
                f"It was built from the {voters} votes and tuned to the saved taste profiles of "
                f"{taste_voters} of the voters"
            )
            if loved:
                note += f": together they like {', '.join(loved)}"
            note += "."
        else:
            note = (
                f"It follows the {voters} votes only: none of the voters has a usable taste profile saved."
                if voters
                else "It follows the votes only: none of the voters has a usable taste profile saved."
            )

    return (
        f"{head}\n{note}\n"
        "Tell the user it is locked and ready in one or two short sentences and include the link exactly "
        "as written above - never shorten, rewrite or invent a link."
    )


# trip_ids with a finalization currently in flight - guards against
# finalize_trip_plan_async being called twice for the SAME trip from the
# microphone before the first call's thread finishes, which would push the
# result to LINE twice. (The web app's own claim already stops a second
# GENERATION, answering 409, so this only spares the second call a pointless
# round trip and a confusing LINE message.) A plain set, not a dict of Locks:
# membership alone is the guard, and the thread lock below only protects the
# check-and-add being atomic across threads.
_finalizing_trip_ids: set[str] = set()
_finalizing_trip_ids_lock = threading.Lock()


def _finalize_trip_task(trip_id: str, base_url: str) -> None:
    """
    The voice assistant's finalization, on its own thread - same "WHY THIS MUST
    NOT BLOCK" reasoning as _plan_trip_task above: the app's generation takes
    ~15-30 s and the supervisor loop that called finalize_trip_plan_async must
    not wait for it. Asks the app to lock the poll (no user id: the owner at the
    microphone) and sends the plan's link - or the reason it failed - to LINE.

    `base_url` is the web app to call, resolved on the calling thread (a new
    thread has no request context of its own). Always releases trip_id from
    _finalizing_trip_ids before returning, however it exits - see
    finalize_trip_plan_async, which is what reserves it.
    """
    try:
        _ensure_env_loaded()
        _log(f"[TripPlanner] Finalizing poll {trip_id} on {base_url}...")

        try:
            data = _post_poll_finalize(base_url, trip_id, "")
            link = build_liff_link(str(data["path"]))
            if data.get("alreadyLocked") is True:
                message = f"โพล {trip_id} ถูกล็อกไปแล้วครับ ดูแผนได้ที่นี่: {link}"
            else:
                message = f"แผนทริปของโพล {trip_id} ล็อกและสร้างเสร็จแล้วครับ! ดูแผนได้ที่นี่: {link}"
        except Exception as exc:  # noqa: BLE001 - TripApiError, or a link refused in production
            _log(f"[TripPlanner] Finalizing poll {trip_id} failed: {type(exc).__name__}: {exc}")
            message = f"ล็อกแผนทริป {trip_id} ไม่สำเร็จครับ ({exc}) ลองสั่งล็อกอีกครั้งได้เลยครับ"

        try:
            from plugins import line_notifier

            result = line_notifier.send_line_notification(message)
            _log(f"[TripPlanner] Finalization notification for {trip_id}: {result}")
        except Exception as exc:  # noqa: BLE001 - the thread ends either way
            _log(
                f"[TripPlanner] Notifying about the {trip_id} finalization result "
                f"failed: {type(exc).__name__}: {exc}"
            )
    finally:
        with _finalizing_trip_ids_lock:
            _finalizing_trip_ids.discard(trip_id)


def finalize_trip_plan_async(trip_id: str) -> str:
    """
    Locks a consensus poll and generates its final plan from the votes, by
    asking the Trip Planner app (POST /api/poll/<id>/finalize - see "FINALIZING
    A POLL" in the module docstring). The plan is the app's own: real venues,
    travel times, the voters' Taste Profiles, stored there.

    From the web chat it runs SYNCHRONOUSLY and returns the link to the plan
    (~15-30 s): the person is watching a chat and the link is the answer. The
    request carries the signed-in person's id, and the app only lets the person
    who created the poll in the chat lock it from there - everyone else gets its
    reason back as an error for the model to relay (the organizer's link, sent
    to LINE, still works for any poll).

    From the voice assistant it MUST NOT wait - see "WHY THIS MUST NOT BLOCK":
    the only work done on the calling thread is argument cleanup and
    Thread.start(); the call and the LINE message with the plan's link happen in
    _finalize_trip_task, which this function never joins. The acknowledgement it
    returns now is what Jarvis speaks; the LINE message arrives when the work is
    done. The voice caller has no user id and may lock a poll that was made by
    voice.

    Called only as Step 3 of the two-step consensus finalization rule in
    llm_brain.py's SYSTEM_PROMPT - after check_poll_status_async has already
    reported the vote count and the user has confirmed.
    """
    trip_id = _extract_trip_id(trip_id)
    if not trip_id:
        return "ERROR: I need a trip id to finalize a plan."

    # Resolved here, on the calling thread: it is the one that knows which web
    # app the request came from (check_poll_status_async read the same one).
    context = request_context.current()
    base_url = resolve_api_base_url(context.app_url if context else "")

    if context is not None and context.is_web:
        if not context.user_id:
            return (
                "ERROR: this chat did not tell me who is asking, and the web app only lets the person who "
                "created a poll lock it from the chat, so I can't lock it from here. They can use the "
                "organizer link that was sent to LINE. Tell the user plainly; do not claim the poll was locked."
            )
        try:
            data = _post_poll_finalize(base_url, trip_id, context.user_id)
        except TripApiError as exc:
            _log(f"[TripPlanner] Web poll {trip_id} could not be locked: {exc}")
            return (
                f"ERROR: the poll for trip {trip_id} could not be locked - {exc}. "
                "Tell the user plainly what went wrong; do not claim a plan exists and do not invent a link."
            )
        url = f"{base_url}{data['path']}"
        _log(f"[TripPlanner] Web poll {trip_id} locked: {url}")
        return _describe_finalized_poll(data, url)

    with _finalizing_trip_ids_lock:
        if trip_id in _finalizing_trip_ids:
            return (
                f"I'm already finalizing trip {trip_id} - I'll send the plan to LINE "
                "the moment it's ready, no need to ask again."
            )
        _finalizing_trip_ids.add(trip_id)

    thread = threading.Thread(
        target=_finalize_trip_task,
        args=(trip_id, base_url),
        name=f"trip-finalize-{trip_id}",
        daemon=True,
    )
    thread.start()

    print(f"[TripPlanner] Finalization assigned for trip {trip_id}.")
    return (
        f"I've started locking the poll for trip {trip_id} and generating the final plan from the votes. "
        "I'll send the link to LINE when it's ready."
    )


def generate_trip_plan_async(
    starting_location: str = "",
    destination: str = "",
    planning_type: str = "solo_blank",
    head_count: int | None = None,
    days: int | None = None,
) -> str:
    """
    Plans a trip. Needs the destination, the number of days and the number of
    people, all three stated by the user.

    Nothing is defaulted. A missing or unusable `days` or `head_count` returns
    a "NOT STARTED" sentence that tells the model to ask the user for exactly
    that - this is the code half of the prompt rule against assuming "3 days"
    or "1 person" (see "Planning a trip" in llm_brain.SYSTEM_PROMPT); the
    model is told to ask first, and this makes the tool refuse when it did
    not. A value that merely looks made up cannot be told from a real one
    here, which is why the tool schema also marks all three as required.

    From the web chat (request_context says so) this runs SYNCHRONOUSLY and
    returns the link to the finished plan, poll or draft board - see "WEB
    CHAT AND THE TRIP API" in the module docstring. The person is watching a
    chat, a link is the answer, and the work is one call to the Trip Planner
    app (~15-30 s for a plan).

    From the voice assistant it MUST NOT wait for the plan to finish - see
    "WHY THIS MUST NOT BLOCK" above. The only work done on the calling
    thread is argument cleanup and Thread.start(); every network call and
    the Gemini call happen in _plan_trip_task, on a thread this function
    never joins.

    starting_location is optional and, on the web path, never read. On the
    voice path it is read here, handed to the thread as a plain argument,
    and never touched again by this function - see the module's privacy
    constraint. It used to be required, and the model was told to ask for it,
    though nothing ever used it beyond the acknowledgement sentence.
    """
    starting_location = (starting_location or "").strip()
    destination = " ".join((destination or "").split())
    if not destination:
        return "ERROR: I need a destination to plan a trip."

    planning_type = (planning_type or "solo_blank").strip().lower()
    if planning_type not in _VALID_PLANNING_TYPES:
        planning_type = "solo_blank"

    head_count = _positive_int(head_count)
    days = _positive_int(days)
    missing = [
        label
        for label, value in (("how many days the trip is", days), ("how many people are going", head_count))
        if value is None
    ]
    if missing:
        print(f"[TripPlanner] Not starting a trip to {destination}: missing {', '.join(missing)}.")
        return (
            f"NOT STARTED: a trip to {destination} cannot be planned until the user says "
            f"{' and '.join(missing)}. Do not assume or guess "
            "it - not 3 days, not 1 person. Ask the user one short question for exactly "
            "that, then call this tool again with their answer."
        )

    context = request_context.current()
    if context is not None and context.is_web:
        return _plan_trip_for_web(context, destination, planning_type, head_count, days)

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
    route = f"from {starting_location} to {destination}" if starting_location else f"to {destination}"
    return (
        f"I've assigned the Planner Agent to organize your trip {route}. "
        "I will notify you on LINE when the plan is ready."
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
