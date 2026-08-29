"""
Module 5: Conversational Brain - multi-provider LLM supervisor

The only component in Jarvis that talks to a language model. Wake word
detection and speech-to-text stay on-device; this module sends the
transcribed text of a command to an LLM and returns the reply for the
voice engine to speak.

-------------------------------------------------------------------
WHAT CHANGED: SUPERVISOR, NOT JUST CHATBOT
-------------------------------------------------------------------
This module used to be a one-shot question-answerer. It is now a
manager that can reach the rest of the system through tools:

    user speech -> LLM decides -> tool call -> real module runs
                -> result goes back to the LLM -> one spoken sentence

The local intent parsers in main.py still get first refusal on every
transcript, so "open subtrack" and "run workflow data sync" never leave
the machine. The supervisor exists for the open-ended requests those
parsers cannot claim - "log my Netflix subscription, fifteen dollars,
entertainment" - where the value of the command is the structured
payload buried in it, not the workflow name.

Tools live in TOOLS below. Each carries one JSON schema that is
translated into whichever provider's dialect is in use, so adding a
tool means writing one entry, not two.

Four things were added to this module beyond plain tool calling:

  DYNAMIC MODEL ROUTING  _classify_intent labels each question and
      ROUTING_TABLE turns that label into a model, before the turn is
      sent. Greetings and the time go to the cheap tier; ordinary
      transactional work stays on the standard one; "design", "analyse
      this codebase", "think hard" escalate to the reasoning tier. A
      keyword-and-length heuristic on purpose - asking a model which
      model to use costs a round trip on every question. The table has
      one column per provider, so a failover to Gemini re-points every
      tier at once rather than sending Claude model IDs to Google.

  BACKGROUND TOOLS  a ToolSpec marked background=True is handed to
      task_dispatcher and the model is told "started", not "succeeded".
      The user hears "I'm on it" and keeps talking; the outcome is
      announced later by session_manager. Identical calls inside
      DISPATCH_REPLAY_WINDOW are refused, because "started" is exactly
      the kind of result that makes a model try again and write the row
      to the database twice.

  VISION  analyze_screen pulls the newest frame from vision_engine's
      rolling buffer and sends it as a one-shot request, outside the
      conversation transcript - a 200 KB image resent every turn would
      eat the context window and the bill.

  SELF-EXTENSION  create_new_tool writes a plugin into plugins/, which
      plugin_loader picks up on the next turn. It is confirmation-gated
      and the code is printed before it is written. Read the honest
      security note in plugin_loader.py before extending this.

-------------------------------------------------------------------
PROVIDERS
-------------------------------------------------------------------
Two backends, tried in order:

    1. Anthropic Claude   (ANTHROPIC_API_KEY)
    2. Google Gemini      (GEMINI_API_KEY)

Gemini is used when Anthropic has no key configured, and is also failed
over to at runtime when Anthropic rejects the key or the model - the
cases where retrying Anthropic could not possibly help. Transient
failures (rate limits, network, 5xx) do NOT fail over: the SDK already
retries those, and silently switching providers would hide a blip
behind a different voice and a different bill.

-------------------------------------------------------------------
MODEL CHOICE - READ THIS
-------------------------------------------------------------------
The Anthropic path targets "claude-sonnet-5", not "claude-3-5-sonnet",
and "claude-haiku-4-5", not "claude-3-haiku-20240307". Every model in
ROUTING_TABLE is a current one, and that is not cosmetic: the claude-3
generation is retired, so a router pointed at it would fail every
question it routed instead of answering it more cheaply.

Claude 3.5 Sonnet was retired on 2025-10-28 and now returns HTTP 404.
Sonnet 5 also rejects a few parameters that older Claude code commonly
sets - passing any of these returns a 400:

    temperature / top_p / top_k   (removed; steer with the prompt)
    thinking={"budget_tokens": N} (removed; use `effort` instead)
    a trailing assistant "prefill" message

None of them are used here. If you copy request-building code in from
an older project, strip those first.

The cheap tier runs the other way round: Haiku 4.5 PREDATES adaptive
thinking and output_config.effort and returns a 400 for either, so
AnthropicProvider.complete strips both when the router selects it. That
leaves the cheap tier running with thinking off, which is why the router
only ever sends it questions that need no tool call - see the comment
on _ALWAYS_FAST.

Requires:
    pip install anthropic google-genai python-dotenv

Setup (one time) - see .env.example. At least one of:
    ANTHROPIC_API_KEY=sk-ant-...
    GEMINI_API_KEY=...
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import plugin_loader
import task_dispatcher
import vision_engine
from tool_spec import ToolSpec

# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

# THREE-TIER MODEL ROUTING (see _classify_intent and ROUTING_TABLE)
#
# Some of what a voice assistant is asked is not a request at all: a
# greeting, a thank-you, the time. Most of the rest is small but real -
# parse an intent, call one tool, answer in a sentence. And occasionally
# there is "design a schema for this" or "analyse this codebase", which
# is a different job, and paying reasoning-tier latency for it is worth
# it precisely because it is rare.
#
# The router picks per question, before the turn is sent. Nothing is
# sticky: a heavy question followed by "thanks" drops straight back to
# the cheap tier.
#
# MODEL IDS - READ THIS BEFORE CHANGING THEM
# ------------------------------------------
# These are deliberately NOT the claude-3 generation. Both
# claude-3-haiku-20240307 and claude-3-5-sonnet-20240620 are retired and
# answer HTTP 404, and that generation would reject the request this
# module builds in any case - see the note on adaptive thinking and
# output_config.effort below. The tiers below are the current
# equivalents: Haiku for talk, Sonnet for work, Opus for thinking.

INTENT_SIMPLE = "simple"        # greetings, thanks, the time - talk, not work
INTENT_STANDARD = "standard"    # the default: one tool call and a sentence
INTENT_COMPLEX = "complex"      # design, analysis, debugging, writing code

MODEL_SIMPLE = "claude-haiku-4-5"   # cheap tier, for talk that needs no thought
MODEL = "claude-sonnet-5"           # standard tier, and the default
MODEL_DEEP = "claude-opus-5"        # reasoning tier, for heavy requests

# Gemini's equivalents, for the failover provider. Flash covers both
# lower tiers: Gemini prices them close enough together that a third
# name would buy nothing and be one more model ID to keep alive.
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_MODEL_DEEP = "gemini-2.5-pro"

# Thinking depth / token spend: low | medium | high | xhigh | max.
# "low" is the right setting for latency-sensitive conversational work;
# the reasoning tier is the only place a higher setting earns its wait.
EFFORT = "low"
EFFORT_DEEP = "high"

# Models that predate `output_config.effort` and adaptive thinking, both
# of which arrived with the 4.6 generation and are a 400 before it.
# AnthropicProvider.complete strips both for anything named here - which
# is what keeps the cheap tier usable instead of a source of 400s.
MODELS_WITHOUT_EFFORT: frozenset[str] = frozenset({MODEL_SIMPLE})

# Intent -> what to send, per provider. The single place a tier names a
# model: the router looks up a row and hands the pair to the provider,
# which never decides for itself. Adding a fourth tier is a row here and
# a marker list below, and nothing else.
ROUTING_TABLE: dict[str, dict[str, str]] = {
    INTENT_SIMPLE: {
        "anthropic": MODEL_SIMPLE,
        "gemini": GEMINI_MODEL,
        "effort": EFFORT,
    },
    INTENT_STANDARD: {
        "anthropic": MODEL,
        "gemini": GEMINI_MODEL,
        "effort": EFFORT,
    },
    INTENT_COMPLEX: {
        "anthropic": MODEL_DEEP,
        "gemini": GEMINI_MODEL_DEEP,
        "effort": EFFORT_DEEP,
    },
}

# Vision requests go to the standard tier. Reading a screen is not a
# reasoning task, but it is not small talk either, and the cheap tier
# is the one model here that never sees an image.
MODEL_VISION = MODEL

# A question longer than this many words is treated as heavy on length
# alone: nobody dictates forty words to ask what time it is.
DEEP_WORD_COUNT = 40

# ...and the ceiling on the other side. A greeting is a handful of
# words; anything longer is doing something, not saying hello.
SIMPLE_WORD_COUNT = 8

# Seconds during which an identical background call is treated as a
# replay rather than a new request. Two minutes covers "did that work?"
# follow-ups without blocking a genuine "log another coffee".
DISPATCH_REPLAY_WINDOW = 120.0

# System prompt for the one-shot vision calls. Separate from the main
# persona because the job is different: describe what is actually on the
# screen, briefly, out loud, without inventing anything that is not
# visible in the frame.
VISION_SYSTEM_PROMPT = """You are the eyes of a voice assistant. You are shown one screenshot of the user's screen and asked a question about it.

- Answer in one to three spoken sentences. The user hears your answer; they do not read it.
- Describe only what is actually visible in the image. If the answer is not on screen, say so plainly rather than guessing.
- When reading code or text back, read the relevant part, not the whole screen, and say which file or window it is in if that is visible.
- Never read out long paths, URLs, or anything that looks like a password, an API key, or a token, even if it is on screen. Say that it is there instead.
- No markdown, bullet points, or code fences - your words are spoken."""

# Spoken answers are short by nature, so a small cap keeps replies tight
# and avoids the assistant monologuing at the user through the speakers.
MAX_TOKENS = 1024

# Extended thinking costs seconds of latency, which is very noticeable
# when the user is waiting for the speakers. It is disabled for plain
# conversation and ADAPTIVE whenever tools are in play - with thinking
# off, a model will occasionally write a tool call into its visible
# prose instead of emitting a real tool_use block, which fails silently:
# the turn succeeds, the tool never runs, and the assistant cheerfully
# reports something that did not happen. Adaptive thinking at low effort
# costs a beat and removes that failure mode.
THINKING_PLAIN = {"type": "disabled"}
THINKING_TOOLS = {"type": "adaptive"}

# How many past turns (1 turn = 1 user message + 1 reply) to resend.
# The context window is large; this is about cost and latency, not limits.
MAX_HISTORY_TURNS = 12

# How many tool round trips one question may take before the supervisor
# gives up. Each step is a full API call, so this is a latency ceiling
# as much as a loop guard.
SUPERVISOR_MAX_STEPS = 4

# Persona + background. Written plainly on purpose: these models follow
# instructions literally, so shouted "CRITICAL: YOU MUST" phrasing tends
# to make them over-apply a rule rather than obey it better.
SYSTEM_PROMPT = """\
You are Jarvis, a voice-controlled assistant running locally on the \
user's Windows machine.

Your replies are converted to speech and played through speakers. The \
user hears them; they never see them. This shapes everything about how \
you answer:

- Always speak English. Even if the user speaks to you in Thai or a \
mix of Thai and English, you MUST always generate your spoken \
responses exclusively in English. Never emit Thai script: the speech \
engine uses an English voice, and Thai text comes out as silence.
- Answer in one to three sentences. Lead with the answer itself, then \
add context only if it genuinely changes what the user would do next.
- Write plain spoken prose. No markdown, headings, bullet points, \
numbered lists, emoji, or asterisks - they are read aloud as literal \
junk or silently mangled.
- Never speak code, file paths, long URLs, or configuration blocks. If \
the answer is code, say in one sentence what the approach is and offer \
to write it to a file instead.
- Spell out anything that would be misread aloud: say "about 14 percent" \
rather than "~14%".
- If a question is ambiguous, ask one short clarifying question rather \
than guessing at length.
- TOOL PARAMETER GATHERING: When you must ask the user for missing \
details to fulfill a tool (e.g., asking for a starting location), your \
response MUST contain ONLY that specific question. You are strictly \
forbidden from appending conversational closers (e.g., "What else?", \
"Is there anything else?"). End your sentence immediately after the \
question mark.
- WAKE WORD BEHAVIOR: If the user's input is solely your name \
("Jarvis") or a slight variation with no other commands, you MUST \
simply reply with a brief, natural greeting such as "Yes?" or "I'm \
listening." DO NOT trigger any app-opening tools (like the OS \
Orchestrator) unless the user explicitly uses the word "open" or \
"launch".
- If you dispatch a long-running tool to the background - one whose \
result comes back as "started" rather than as an answer - you MUST say \
explicitly that it is running in the background and that you will let \
them know when it is done. The same no-closers rule above applies here \
too: end there. Never leave the user guessing whether something is \
still happening, but never pad the acknowledgement with a question \
either.
- If the user's input is a simple conversational pleasantry, such as \
"thank you" or "good morning", reply naturally and briefly - "You're \
welcome" - and stop there. Do not ask a follow-up question, do not \
offer further help, and do not reach for a tool.

You are an intelligent router. Evaluate if the request requires deep reasoning. If it does, route to high-tier models; otherwise, use efficient, fast models. The routing itself happens in code before you see the question, so read that as a description of the judgement you are already the product of, and match your effort to the request: answer a greeting in a handful of words without deliberating, and think properly before answering something that has earned it. Never say any of this out loud. The user asked a question, not which model is answering it, and words like "routing", "tier", "Haiku", "Sonnet" or "Opus" spoken through the speakers are noise. If a request clearly needed more thought than the answer you are able to give it, say so plainly and offer to take another pass at it.

You also act as a manager for the systems around you. You have tools \
that reach outside this process: one triggers an n8n automation \
workflow with a payload you build, one reads from the Subtrack \
database.

Your primary automation job is data capture for Subtrack. When the \
user says they spent something, subscribed to something, cancelled \
something, or otherwise gives you a record to keep, pull the \
structured fields out of what they said - the item name, the amount, \
the currency, the category, the billing cycle, the date - and send \
them as a JSON payload to the subtrack log workflow, which writes the \
row into the database. You are the one who turns loose speech into \
clean fields; do not make the user dictate them one at a time.

Use the tools like this:

- Decide first whether a tool is actually needed. Most questions are \
just questions; reach for a tool only when the answer depends on the \
current state of one of those systems, or when the user is asking for \
something to be run or logged.
- When you log something, extract every field the user actually said \
and leave out the ones they did not. Never invent an amount, a date, \
or a category to fill a slot - an unset field is fine, a wrong one is \
not. If the single most important field is missing, or you genuinely \
could not tell what it was, ask one short question rather than \
guessing.
- After logging, confirm in one short sentence naming the item and \
the amount, so the user can hear a misheard number and correct it.
- Call the tool, read what comes back, and then say in one or two \
sentences what it means. Never read a raw tool result aloud verbatim, \
and never speak a JSON payload.
- Tool results are the only source of truth about those systems. If a \
tool reports that it is not configured, not implemented, or that it \
failed, say exactly that. Do not fill the gap with a plausible-sounding \
number or status - a confidently invented answer about whether a \
workflow ran is worse than admitting you could not check.
- Do not ask for permission in words before calling a tool. The \
side-effecting tools already ask the user out loud themselves, so a \
"shall I?" first means being asked twice for one action. If you have \
what you need, call the tool. Ask a question only when a detail you \
genuinely need is missing.
- If the user declines a confirmation prompt, acknowledge it briefly and \
do not try the same action again.

Finalizing a group or consensus trip - one that already has a poll \
running, as opposed to setting one up for the first time - is a \
two-step process. You MUST NOT jump straight to generating the plan:

1. Call check_poll_status_async to get the current voter count and top \
vibes for that trip.
2. Tell the user the count in one short spoken sentence (e.g. "4 people \
have voted so far, mostly for cafe hopping and photo spots.") and \
explicitly ask whether to proceed with generating the plan now. End \
there - this is a confirmation question, so the same no-closers rule \
above applies.
3. Only call finalize_trip_plan_async after the user confirms - never \
generate_trip_plan_async, which starts a brand new poll rather than \
finalizing this one. If the user declines, treat it like any other \
declined confirmation above: acknowledge briefly and stop, without \
generating anything.

Never skip step 1 for a finalization request: the user needs to hear \
how many people have actually voted before agreeing to lock in a plan \
the rest of the group might not be ready for.

About the user, so your answers land at the right technical level:

They are a software developer and a carbon accounting specialist. On the \
engineering side they work in NestJS, Python, PostgreSQL, and n8n for \
workflow automation. On the carbon side they work with ISO 14064-1 for \
greenhouse gas inventories and IPMVP for measurement and verification of \
energy savings.

Assume real fluency in all of that. Skip introductory explanations of \
things they clearly already know, use the correct technical and \
standards terminology directly, and when a question touches both domains \
say so rather than answering only the software half. If you are unsure \
of a specific figure, emission factor, or clause number, say so plainly \
instead of inventing one.\
"""


# ---------------------------------------------------------------------
# Optional dependency probing (never hard-fail at import time)
# ---------------------------------------------------------------------

try:
    import anthropic

    _ANTHROPIC_AVAILABLE = True
    _ANTHROPIC_IMPORT_ERROR = ""
except Exception as exc:  # noqa: BLE001 - missing SDK disables one provider
    anthropic = None  # type: ignore[assignment]
    _ANTHROPIC_AVAILABLE = False
    _ANTHROPIC_IMPORT_ERROR = str(exc)

try:
    from google import genai
    from google.genai import errors as genai_errors
    from google.genai import types as genai_types

    _GEMINI_AVAILABLE = True
    _GEMINI_IMPORT_ERROR = ""
except Exception as exc:  # noqa: BLE001 - missing SDK disables one provider
    genai = genai_errors = genai_types = None  # type: ignore[assignment]
    _GEMINI_AVAILABLE = False
    _GEMINI_IMPORT_ERROR = str(exc)

try:
    from dotenv import load_dotenv

    _DOTENV_AVAILABLE = True
except Exception:  # noqa: BLE001 - .env loading is a convenience, not a requirement
    load_dotenv = None  # type: ignore[assignment]
    _DOTENV_AVAILABLE = False


def _env_path() -> str:
    """Absolute path to the project's .env file."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


_ENV_LOADED = False


def _load_env() -> None:
    """
    Reads .env once per process.

    override=False so an already-exported environment variable takes
    precedence, which is what you want on a server or in CI.
    """
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    if _DOTENV_AVAILABLE:
        load_dotenv(_env_path(), override=False)
    _ENV_LOADED = True


def _env(name: str, default: str = "") -> str:
    _load_env()
    return (os.environ.get(name) or "").strip() or default


def load_api_key(name: str = "ANTHROPIC_API_KEY") -> Optional[str]:
    """Reads one API key from the environment or .env. None if unset."""
    return _env(name) or None


def missing_key_instructions() -> str:
    """Copy-pasteable setup steps, shown when no provider is configured."""
    return (
        "  Jarvis needs one language model provider. Either works:\n"
        "\n"
        "  Option A - Anthropic Claude (preferred)\n"
        "    1. Create a key at https://console.anthropic.com/settings/keys\n"
        "    2. Add to .env:  ANTHROPIC_API_KEY=sk-ant-your-key-here\n"
        "\n"
        "  Option B - Google Gemini (fallback)\n"
        "    1. Create a key at https://aistudio.google.com/apikey\n"
        "    2. Add to .env:  GEMINI_API_KEY=your-key-here\n"
        "\n"
        f"  The .env file lives at:\n       {_env_path()}\n"
        "  Put each key on its own line, no quotes, no spaces around '='.\n"
        "  Then restart Jarvis.\n"
        "\n"
        "  Keep .env out of version control - it holds credentials."
    )


# =====================================================================
# Tool layer
# =====================================================================
#
# One ToolSpec per capability. The JSON schema is written once and
# translated per provider, so the two dialects can never drift apart.


# =====================================================================
# Intent routing (Feature 3)
# =====================================================================
#
# Deliberately a keyword-and-length heuristic rather than a model call.
# Asking a model which model to use costs an extra round trip on EVERY
# question - which is exactly the latency the cheap tier exists to avoid
# - and gets the easy cases no more right than this does.
#
# The three marker lists below are read in a fixed order, chosen by what
# a wrong answer costs. Escalation is checked first: sending a design
# question to Haiku produces a bad answer, while sending a greeting to
# Opus only wastes a fraction of a cent.

# Verbs and nouns that mean "this needs real thinking". Matched as whole
# words against the lowercased question.
_DEEP_MARKERS: tuple[str, ...] = (
    "design", "architect", "architecture", "refactor", "restructure",
    "analyse", "analyze", "analysis", "audit", "review", "critique",
    "debug", "diagnose", "root cause", "troubleshoot",
    "plan", "strategy", "roadmap", "trade off", "tradeoff", "compare",
    "explain why", "walk me through", "reason about", "think through",
    "think hard", "think carefully", "step by step", "in depth",
    "codebase", "whole project", "entire project", "schema", "migration",
    "algorithm", "optimise", "optimize", "performance problem",
    "write a script", "write the code", "implement", "build me",
    "pros and cons", "best approach", "how should i",
)

# Explicit escalation, in both languages this assistant speaks. When the
# user says "think hard about this", that is not a hint.
_FORCE_DEEP: tuple[str, ...] = (
    "think hard", "think carefully", "take your time", "be thorough",
    "use your best model", "deep dive",
    "คิดให้ดี", "คิดดีๆ", "วิเคราะห์",
)

# Short, obviously-transactional openings that stay on the standard tier
# no matter what else is in the sentence. "Log my coffee" contains none
# of the markers above, but "log the design review" would - and it is
# still just a logging command.
#
# These stay OFF the cheap tier as firmly as they stay off the deep one.
# Every one of them ends in a tool call, and the cheap tier is the one
# that runs without thinking enabled (see MODELS_WITHOUT_EFFORT), which
# is exactly the configuration where a model occasionally writes its
# tool call into prose instead of emitting a real tool_use block. Saving
# a tenth of a cent is not worth a silently unlogged expense.
_ALWAYS_FAST: tuple[str, ...] = (
    "log ", "open ", "run ", "trigger ", "check ",
    "what is on my screen", "what am i looking at", "read this",
)

# Talk, not work: nothing to look up, nothing to run, nothing to reason
# about. These are the utterances worth routing to the cheap tier, and
# the list is deliberately narrow for the reason above.
#
# Matched against the same punctuation-stripped text as everything else,
# so the apostrophes are already gone - "what's the time" arrives here
# as "what s the time".
_SIMPLE_MARKERS: tuple[str, ...] = (
    "hello", "hi", "hey", "yo",
    "good morning", "good afternoon", "good evening", "good night",
    "thanks", "thank you", "cheers", "nice one", "well done",
    "how are you", "you there", "are you there", "still there", "wake up",
    "what time", "what s the time", "what day", "what s the date",
    "never mind", "nevermind", "forget it", "no worries",
    "goodbye", "bye", "that s all", "that will be all",
    "ok", "okay", "cool", "great", "sorry", "yes", "no",
    "sawasdee", "khop khun",
)

# Words that mean the user wants something DONE, not said. A short
# utterance containing one of these is never small talk, however politely
# it opens: "hey, run the sync workflow" starts with a greeting and ends
# in a database write.
_TOOL_WORDS: tuple[str, ...] = (
    "log", "open", "run", "trigger", "start", "stop", "launch", "close",
    "kill", "restart", "check", "read", "show", "find", "look",
    "screen", "screenshot", "database", "workflow", "subtrack", "docker",
    "server", "servers", "backend", "frontend", "container", "containers",
    "spent", "spend", "paid", "bought", "subscription", "invoice", "expense",
)


def _classify_intent(text: str) -> tuple[str, str]:
    """
    (intent, reason) for one question. The reason is logged, not spoken.

    The intent is a key into ROUTING_TABLE, so this function is the only
    thing that decides which model answers - and it decides once, before
    the turn is sent, rather than per tool round trip.
    """
    lowered = " " + re.sub(r"[^\w\s]", " ", text.lower()).strip() + " "

    for phrase in _FORCE_DEEP:
        if phrase in text.lower():
            return INTENT_COMPLEX, f"the user asked for it ({phrase!r})"

    for opener in _ALWAYS_FAST:
        if lowered.lstrip().startswith(opener):
            return INTENT_STANDARD, "a short transactional command"

    for marker in _DEEP_MARKERS:
        if f" {marker} " in lowered:
            return INTENT_COMPLEX, f"matched {marker!r}"

    words = lowered.split()
    if len(words) >= DEEP_WORD_COUNT:
        return INTENT_COMPLEX, f"{len(words)} words long"

    # The cheap tier is claimed last and has to earn all three: short
    # enough to be a pleasantry, recognisably one, and free of any word
    # suggesting the user wants something run. Anything else is work,
    # and work goes to the standard tier.
    if len(words) <= SIMPLE_WORD_COUNT and not any(w in _TOOL_WORDS for w in words):
        for marker in _SIMPLE_MARKERS:
            if f" {marker} " in lowered:
                return INTENT_SIMPLE, f"small talk (matched {marker!r})"

    return INTENT_STANDARD, "ordinary transactional work"


# -- tool implementations ---------------------------------------------


def _tool_trigger_n8n_webhook(workflow_name: str, payload: Any = None) -> str:
    """
    Fires a named n8n Production Webhook via the existing Module 6.

    n8n_trigger.trigger_workflow already resolves fuzzy names, enforces
    a timeout, and phrases every outcome as one spoken sentence, so this
    wrapper only has to handle the import and the argument shape.
    """
    try:
        import n8n_trigger
    except ImportError as exc:
        return f"The n8n automation module could not be loaded: {exc}"

    if payload is not None and not isinstance(payload, dict):
        # A model may hand back a JSON string instead of an object.
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                return (
                    "The payload was not valid JSON, so nothing was sent. "
                    "Pass it as an object, not a string."
                )
        if not isinstance(payload, dict):
            return "The payload must be a JSON object. Nothing was sent."

    try:
        return n8n_trigger.trigger_workflow(workflow_name, payload)
    except Exception as exc:  # noqa: BLE001 - a tool must not crash the loop
        return f"The workflow trigger failed unexpectedly: {type(exc).__name__}: {exc}"


# Override point for the Subtrack reader. Register a callable taking the
# query string and returning a string, and it replaces the n8n webhook
# below - for a direct Supabase client, say - without the tool
# definition or the prompt changing.
_SUBTRACK_EXECUTOR: Optional[Callable[[str], str]] = None


def register_subtrack_executor(executor: Callable[[str], str]) -> None:
    """Installs an alternative Subtrack backend, replacing the webhook."""
    global _SUBTRACK_EXECUTOR
    _SUBTRACK_EXECUTOR = executor
    print("[Brain] Subtrack query backend registered.")


def _tool_query_subtrack_database(query_string: str = "") -> str:
    """
    Reads the most recent records back out of the Subtrack project.

    Subtrack lives in Supabase, not in the Neon instance db_connector.py
    talks to, and this deliberately does not go near db_connector:
    answering a Subtrack question with DEMP energy-log numbers would be
    worse than answering nothing, because it would look right.

    The read goes through the same n8n webhook the logging tool posts
    to, which responds with its last node's output - the 5 most recent
    rows. Nothing here interprets those rows; they are handed to the
    model as JSON so it can summarise them out loud.
    """
    if _SUBTRACK_EXECUTOR is not None:
        try:
            return _SUBTRACK_EXECUTOR(query_string)
        except Exception as exc:  # noqa: BLE001 - a tool must not crash the loop
            return f"The Subtrack query failed: {type(exc).__name__}: {exc}"

    try:
        import n8n_trigger
    except ImportError as exc:
        return (
            f"ERROR: the n8n automation module could not be loaded ({exc}), so "
            "no records were read. Do not invent any."
        )

    try:
        return n8n_trigger.fetch_subtrack_records(query_string)
    except Exception as exc:  # noqa: BLE001 - a tool must not crash the loop
        print(f"[Brain] Subtrack fetch failed: {type(exc).__name__}: {exc}")
        return (
            "ERROR: the Subtrack lookup failed unexpectedly, so no records "
            "were read. Tell the user plainly; do not invent any."
        )


def _tool_start_subtrack_env() -> str:
    """
    Brings up the Subtrack development environment.

    The work lives in plugins/os_orchestrator.py, which is an
    implementation module rather than an auto-loaded plugin: the tool is
    declared here, once, and imported lazily so a missing or broken
    orchestrator file cannot stop the brain from starting.
    """
    try:
        from plugins import os_orchestrator
    except Exception as exc:  # noqa: BLE001 - report it, never crash the loop
        return (
            f"ERROR: the OS orchestrator module could not be loaded "
            f"({type(exc).__name__}: {exc}), so nothing was started."
        )

    try:
        return os_orchestrator.start_subtrack_env()
    except Exception as exc:  # noqa: BLE001 - a tool must not kill the loop
        print(f"[Brain] start_subtrack_env failed: {type(exc).__name__}: {exc}")
        return (
            "ERROR: starting the Subtrack environment failed unexpectedly. "
            "Tell the user plainly; do not claim anything came up."
        )


def _tool_stop_subtrack_env() -> str:
    """
    Takes the Subtrack development environment back down.

    Same shape as _tool_start_subtrack_env: the work lives in
    plugins/os_orchestrator.py and is imported lazily, so a missing or
    broken orchestrator file degrades to one spoken sentence rather than
    stopping the brain from starting.
    """
    try:
        from plugins import os_orchestrator
    except Exception as exc:  # noqa: BLE001 - report it, never crash the loop
        return (
            f"ERROR: the OS orchestrator module could not be loaded "
            f"({type(exc).__name__}: {exc}), so nothing was stopped."
        )

    try:
        return os_orchestrator.stop_subtrack_env()
    except Exception as exc:  # noqa: BLE001 - a tool must not kill the loop
        print(f"[Brain] stop_subtrack_env failed: {type(exc).__name__}: {exc}")
        return (
            "ERROR: stopping the Subtrack environment failed unexpectedly. "
            "Tell the user plainly; do not claim anything came down."
        )


def _tool_generate_trip_plan_async(
    starting_location: str = "",
    destination: str = "",
    planning_type: str = "solo_blank",
    head_count: int = 1,
    days: int = 1,
) -> str:
    """
    Kicks off trip planning in the background, for the standalone Trip
    Planner app.

    Same shape as the other implementation-module tools: the work lives
    in plugins/trip_planner.py and is imported lazily, so a missing or
    broken module degrades to one spoken sentence rather than stopping
    the brain from starting.

    Unlike the tools around it, this one is registered background=False
    (see the ToolSpec below) even though the real planning work takes
    many seconds - plugins.trip_planner already backgrounds itself with
    its own thread and returns a tailored acknowledgement immediately,
    so routing it through task_dispatcher as well would background an
    already-backgrounded call and replace that acknowledgement with the
    dispatcher's generic "STARTED IN THE BACKGROUND" text.

    starting_location is forwarded as-is and touched nowhere else in
    this function - see the privacy constraint documented at the top of
    plugins/trip_planner.py before changing that.
    """
    try:
        from plugins import trip_planner
    except Exception as exc:  # noqa: BLE001 - report it, never crash the loop
        return (
            f"ERROR: the Trip Planner module could not be loaded "
            f"({type(exc).__name__}: {exc}), so no trip plan was started."
        )

    try:
        return trip_planner.generate_trip_plan_async(
            starting_location, destination, planning_type, head_count, days
        )
    except Exception as exc:  # noqa: BLE001 - a tool must not kill the loop
        print(f"[Brain] generate_trip_plan_async failed: {type(exc).__name__}: {exc}")
        return (
            "ERROR: starting the trip plan failed unexpectedly. Tell the user "
            "plainly; do not claim planning started."
        )


def _tool_check_poll_status_async(trip_id: str = "") -> str:
    """
    Reports how many people have voted in a trip's consensus poll.

    Same shape as the other implementation-module tools: the work lives
    in plugins/trip_planner.py and is imported lazily, so a missing or
    broken module degrades to one spoken sentence rather than stopping
    the brain from starting.

    Registered background=False (see the ToolSpec below): Step 2 of the
    two-step consensus finalization rule needs this count in hand to
    actually speak it and ask the user to confirm, so it cannot be
    deferred to a background task the way a webhook trigger can.
    """
    try:
        from plugins import trip_planner
    except Exception as exc:  # noqa: BLE001 - report it, never crash the loop
        return (
            f"ERROR: the Trip Planner module could not be loaded "
            f"({type(exc).__name__}: {exc}), so the poll status could not be checked."
        )

    try:
        return trip_planner.check_poll_status_async(trip_id)
    except Exception as exc:  # noqa: BLE001 - a tool must not kill the loop
        print(f"[Brain] check_poll_status_async failed: {type(exc).__name__}: {exc}")
        return (
            "ERROR: checking the poll status failed unexpectedly. Tell the user "
            "plainly; do not invent a vote count."
        )


def _tool_finalize_trip_plan_async(trip_id: str = "") -> str:
    """
    Reads a consensus poll's real votes and generates the final trip
    itinerary from them, pushing the result to LINE.

    Same shape as the other implementation-module tools: the work lives
    in plugins/trip_planner.py and is imported lazily, so a missing or
    broken module degrades to one spoken sentence rather than stopping
    the brain from starting.

    Registered background=False (see the ToolSpec below) for the same
    reason generate_trip_plan_async is: plugins.trip_planner already
    backgrounds itself with its own thread and returns a tailored
    acknowledgement immediately, so routing it through task_dispatcher
    as well would background an already-backgrounded call and replace
    that acknowledgement with the dispatcher's generic "started" text.
    """
    try:
        from plugins import trip_planner
    except Exception as exc:  # noqa: BLE001 - report it, never crash the loop
        return (
            f"ERROR: the Trip Planner module could not be loaded "
            f"({type(exc).__name__}: {exc}), so no finalization was started."
        )

    try:
        return trip_planner.finalize_trip_plan_async(trip_id)
    except Exception as exc:  # noqa: BLE001 - a tool must not kill the loop
        print(f"[Brain] finalize_trip_plan_async failed: {type(exc).__name__}: {exc}")
        return (
            "ERROR: starting the finalization failed unexpectedly. Tell the user "
            "plainly; do not claim a plan was generated."
        )


def _tool_send_line_notification(message: str = "") -> str:
    """
    Pushes a LINE message to the configured user.

    Same shape as the orchestrator tools: the work lives in
    plugins/line_notifier.py and is imported lazily, so a missing or
    broken module degrades to one spoken sentence rather than stopping
    the brain from starting.
    """
    try:
        from plugins import line_notifier
    except Exception as exc:  # noqa: BLE001 - report it, never crash the loop
        return (
            f"ERROR: the LINE notifier module could not be loaded "
            f"({type(exc).__name__}: {exc}), so nothing was sent."
        )

    try:
        return line_notifier.send_line_notification(message)
    except Exception as exc:  # noqa: BLE001 - a tool must not kill the loop
        print(f"[Brain] send_line_notification failed: {type(exc).__name__}: {exc}")
        return (
            "ERROR: sending the LINE notification failed unexpectedly. "
            "Tell the user plainly; do not claim it was sent."
        )


# The provider used for vision calls, injected by LLMBrain so the tool
# function stays a plain callable the schema layer can describe.
_VISION_PROVIDER: Optional["_Provider"] = None


def _tool_analyze_screen(question: str = "What is on the screen?") -> str:
    """
    Answers a question about whatever is on the user's screen right now.

    The screenshot is taken here, synchronously, at the moment the model
    calls this tool - there is no background capture and no buffered
    frame any more. That costs a few tens of milliseconds, which is
    nothing beside the vision round trip that follows, and it means the
    image describes the screen as it is now rather than as it was when a
    timer last fired.
    """
    usable, reason = vision_engine.is_available()
    if not usable:
        return (
            f"I can't see the screen: {reason}. Tell the user that plainly. "
            "Screen capture needs the mss and Pillow packages, and "
            "JARVIS_VISION must not be set to 0."
        )

    frame = vision_engine.capture_now()
    if frame is None:
        return (
            "NOT AVAILABLE: no screen frame could be captured, so there is "
            "nothing to look at. Say so plainly; do not describe an imagined screen."
        )

    if _VISION_PROVIDER is None or not _VISION_PROVIDER.available:
        return "No vision-capable model is configured, so I couldn't look at the screen."

    print(
        f"[Vision] Sending a {frame.width}x{frame.height} frame "
        f"({frame.size_kb:.0f} KB, just captured) to {_VISION_PROVIDER.name}."
    )
    try:
        answer = _VISION_PROVIDER.describe_image(
            question=question or "What is on the screen?",
            image_b64=frame.to_base64(),
            media_type="image/jpeg",
        )
    except ProviderError as exc:
        return f"I couldn't look at the screen: {exc.spoken}"
    except Exception as exc:  # noqa: BLE001 - a tool must not kill the loop
        return f"The screen analysis failed: {type(exc).__name__}: {exc}"

    if not answer:
        return "The vision model returned nothing about that screenshot."
    return answer


def _tool_create_new_tool(
    name: str,
    description: str,
    python_code: str,
    input_schema: Any = None,
) -> str:
    """
    Writes a new plugin into plugins/, making it a tool on the next turn.

    This is the self-extension path, and it is the most dangerous thing
    in this file: it writes code that this process will later execute.
    Three things stand between a model's idea and running code, and all
    three are here rather than in the plugin loader, because this is
    where a human is still in the loop:

        1. the loader's static validation (parses, has the symbols,
           does not shadow a built-in)
        2. the risky-pattern scan, whose findings are spoken aloud in
           the confirmation prompt
        3. the confirmation gate itself, wired to the user's voice

    It is not a sandbox and must not be described as one - see the
    plugin_loader docstring.
    """
    name = (name or "").strip().lower().replace(" ", "_").replace("-", "_")
    code = (python_code or "").strip()

    if not code:
        return "No code was provided, so nothing was written."

    # The schema the model passes describes the new tool's arguments. It
    # may arrive as a JSON string from either provider.
    if isinstance(input_schema, str):
        try:
            input_schema = json.loads(input_schema)
        except json.JSONDecodeError:
            input_schema = None
    if not isinstance(input_schema, dict):
        input_schema = {}
    # Normalise before it is written. A model that omits "type": "object"
    # produces a plugin whose schema the Messages API rejects outright -
    # and that 400 kills every tool call in the turn, not just this one.
    input_schema = plugin_loader.normalise_schema(input_schema)

    # TOOL_SPEC is authoritative for the loader, so make sure the file's
    # own metadata matches what was requested here rather than trusting
    # the model to have written a consistent copy of it.
    spec_literal = json.dumps(
        {"name": name, "description": description.strip(), "input_schema": input_schema},
        ensure_ascii=False,
        indent=4,
    )
    if "TOOL_SPEC" in code:
        code = re.sub(
            r"^TOOL_SPEC\s*=\s*\{.*?^\}", f"TOOL_SPEC = {spec_literal}", code,
            count=1, flags=re.MULTILINE | re.DOTALL,
        )
    else:
        code = f"TOOL_SPEC = {spec_literal}\n\n\n{code}"

    # Validation runs AFTER the metadata is injected, because the tool
    # description promises the model that TOOL_SPEC is optional - failing
    # it for the omission this code just repaired would be a lie.
    ok, reason = plugin_loader.validate_source(code, name)
    if not ok:
        return (
            f"That plugin was rejected: {reason}. Fix the code and call "
            "create_new_tool again - do not tell the user it worked."
        )

    try:
        path = plugin_loader.write_plugin(name, code)
    except (ValueError, OSError) as exc:
        return f"The plugin could not be written: {exc}"

    print(f"[Plugins] Wrote {path}")
    return (
        f"Created the {name} tool at plugins/{name}.py. It loads automatically on "
        "the next command - tell the user it is ready to use now, in one sentence, "
        "and what it does."
    )


def _tool_list_capabilities(include_tasks: bool = True) -> str:
    """What Jarvis can currently do, including plugins and running work."""
    builtin = [t.name for t in TOOLS]
    plugins = plugin_loader.plugin_names()

    parts = [f"Built-in tools: {', '.join(builtin)}."]
    parts.append(
        f"Plugin tools written at runtime: {', '.join(plugins)}."
        if plugins
        else "No plugin tools have been created yet."
    )
    parts.append(vision_engine.status())
    if include_tasks:
        parts.append(task_dispatcher.dispatcher().status_line())
    return " ".join(parts)


TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="trigger_n8n_webhook",
        description=(
            "Trigger an n8n automation workflow by calling its production "
            "webhook, sending a JSON payload of values you extracted from what "
            "the user said. The main workflow is 'subtrack log', which writes a "
            "record into the Subtrack database - use it whenever the user "
            "reports a purchase, a subscription, an expense, or anything else "
            "they want logged. Read the user's sentence and pull out the "
            "individual fields it contains (for example item_name, amount, "
            "currency, category, billing_cycle, vendor, date, notes) and pass "
            "them as the payload. Include only fields the user actually stated; "
            "never invent a value to fill a field. The workflow name is matched "
            "loosely, so a close paraphrase is fine. This has real side effects "
            "outside this machine and asks the user to confirm out loud before "
            "it runs."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "workflow_name": {
                    "type": "string",
                    "description": (
                        "Name of the workflow to run, e.g. 'subtrack log'. Leave "
                        "empty to use the default workflow."
                    ),
                },
                "payload": {
                    "type": "object",
                    "description": (
                        "JSON object of the values extracted from the user's "
                        "request, sent as the webhook body for n8n to write into "
                        "the database. Use flat snake_case keys with real typed "
                        "values - numbers as numbers, not words. Example: for "
                        "\"log my Netflix subscription, fifteen dollars a month, "
                        "entertainment\" send {\"item_name\": \"Netflix\", "
                        "\"amount\": 15, \"currency\": \"USD\", \"category\": "
                        "\"entertainment\", \"billing_cycle\": \"monthly\"}. "
                        "Omit any key the user did not give you rather than "
                        "guessing at it, and omit the payload entirely for a "
                        "workflow that takes no parameters."
                    ),
                    "additionalProperties": True,
                },
            },
            "required": ["workflow_name"],
        },
        handler=_tool_trigger_n8n_webhook,
        requires_confirmation=True,
        confirmation_template="Do you authorize running the {workflow_name} workflow?",
        # Backgrounded: an n8n workflow can take many seconds, and its
        # result is not part of the sentence Jarvis is about to say. The
        # user hears "I'm on it" and can keep talking; the outcome is
        # announced when it lands.
        background=True,
        label="{workflow_name} workflow",
    ),
    ToolSpec(
        name="query_subtrack_database",
        description=(
            "Fetches the 5 most recent ESG carbon/expense records from the "
            "Subtrack database. Use this tool when the user asks for a summary "
            "of recent data. It returns the rows as JSON for you to read: "
            "summarise them out loud in one or two sentences - totals, the "
            "most recent item, anything that stands out - and never read the "
            "JSON itself aloud. This is read-only and never modifies "
            "anything. If it comes back with ERROR or EMPTY, say so plainly "
            "and do not invent records."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query_string": {
                    "type": "string",
                    "description": (
                        "Plain-language description of what the user asked "
                        "for, e.g. 'a summary of my recent spending'. The "
                        "workflow currently returns the latest 5 records "
                        "regardless, so this is context, not a filter."
                    ),
                },
            },
            "required": ["query_string"],
        },
        handler=_tool_query_subtrack_database,
        requires_confirmation=False,
        # Foreground, unlike the logging webhook. The records ARE the
        # answer: backgrounding them would hand the model "started" and
        # then read the raw JSON aloud when it landed, instead of the
        # summary the user asked for. It is one localhost round trip,
        # and the capture filler already covers it.
        background=False,
        label="database query",
    ),
    ToolSpec(
        name="start_subtrack_env",
        description=(
            "Starts the Subtrack development environment by waking up Docker "
            "containers, and launching the Python backend and Next.js frontend "
            "servers in separate windows. Use this when the user asks to start "
            "work, open Subtrack, or initialize the environment. It reports "
            "what came up and what did not - read that back as one or two "
            "sentences, and never claim a service started if the result says "
            "it did not."
        ),
        input_schema={"type": "object", "properties": {}},
        handler=_tool_start_subtrack_env,
        # Launching development servers on the user's machine is a real
        # side effect, and the spoken trigger for it ("start work") is
        # loose enough to be misheard - so it asks first, like the
        # webhook does.
        requires_confirmation=True,
        confirmation_template="Do you want me to start the Subtrack environment?",
        # Foreground: the summary IS the answer. Backgrounding it would
        # hand the model "started" and then read the raw result aloud
        # later, instead of the sentence the user is waiting for. The
        # servers are detached into their own consoles, so this returns
        # as soon as they are launched rather than waiting on them.
        background=False,
        label="Subtrack environment",
    ),
    ToolSpec(
        name="stop_subtrack_env",
        description=(
            "Stops the Subtrack development environment, including terminating "
            "backend/frontend servers and stopping n8n Docker containers. Use "
            "this when the user asks to stop work, shut Subtrack down, or close "
            "the environment for the day. Only the servers this assistant "
            "started are terminated - anything the user launched by hand is "
            "left alone, and the result says so. It reports what came down and "
            "what did not: read that back as one or two sentences, and never "
            "claim a service stopped if the result says it did not."
        ),
        input_schema={"type": "object", "properties": {}},
        handler=_tool_stop_subtrack_env,
        # Killing the user's running dev servers loses unsaved work in
        # them, and the spoken trigger ("stop work") is one mishearing
        # away from ordinary conversation - so it asks first, exactly
        # like the tool that starts them.
        requires_confirmation=True,
        confirmation_template="Do you want me to stop the Subtrack environment?",
        # Backgrounded, unlike its opposite number, because the two are
        # not symmetrical in cost: start_subtrack_env detaches two
        # consoles and returns, while this one waits out `docker stop`'s
        # ten-second SIGTERM grace period and two process trees. That is
        # long enough to hold the listening thread, which is what
        # task_dispatcher exists to prevent. The user hears "I'm on it"
        # and session_manager announces the outcome when it lands.
        background=True,
        label="Subtrack shutdown",
    ),
    ToolSpec(
        name="send_line_notification",
        description=(
            "Sends a push notification message to the user's LINE application "
            "via Messaging API. Use this when the user asks you to send them a "
            "reminder, a summary, or notify them about a completed task."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "The text message to send.",
                },
            },
            "required": ["message"],
        },
        handler=_tool_send_line_notification,
        # Pushing to a real device is an outward-facing side effect
        # reached through two lossy steps - speech recognition, then a
        # language model - same as the n8n webhook, and gated the same
        # way: a human confirms before anything leaves the machine.
        requires_confirmation=True,
        confirmation_template="Do you want me to send that as a LINE notification?",
        # Foreground: the return value IS the confirmation the user is
        # waiting for - "sent" or the specific reason it was not - so
        # backgrounding it would hand the model "started" and then
        # announce the real outcome later, for a call that finishes in
        # one HTTP round trip.
        background=False,
        label="LINE notification",
    ),
    ToolSpec(
        name="generate_trip_plan_async",
        description=(
            "Use this to plan trips. IMPORTANT:\n"
            "- If the user does not specify their starting location, you "
            "MUST ask them where they are traveling from before calling "
            "this tool. Do NOT assume their location.\n"
            "- If the user wants a group trip, use 'consensus'.\n"
            "- If the user wants a solo trip, you MUST ask if they want "
            "you to plan it from scratch (blank canvas) or if they "
            "already have some specific places in mind. Ask EXACTLY ONE "
            "question to clarify this before calling the tool.\n"
            "- Set planning_type to 'solo_blank' if they want you to "
            "decide everything.\n"
            "- Set planning_type to 'solo_draft' if they have specific "
            "places in mind."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "starting_location": {
                    "type": "string",
                    "description": (
                        "Where the trip starts from, e.g. 'Bangkok' or the "
                        "user's home address if they gave one. Ask the user "
                        "for this if they did not say it - never guess it."
                    ),
                },
                "destination": {
                    "type": "string",
                    "description": "Where the trip is to, e.g. 'Chiang Mai'.",
                },
                "planning_type": {
                    "type": "string",
                    "enum": ["solo_blank", "solo_draft", "consensus"],
                    "description": (
                        "'consensus' for a group trip that needs a poll first. "
                        "'solo_blank' for a solo trip where the user wants you "
                        "to decide everything from scratch. 'solo_draft' for a "
                        "solo trip where the user already has specific places "
                        "in mind they want to add themselves."
                    ),
                },
                "head_count": {
                    "type": "integer",
                    "description": "How many people are going, including the user.",
                },
                "days": {
                    "type": "integer",
                    "description": "How many days the trip is planned for.",
                },
            },
            "required": ["starting_location", "destination", "planning_type", "head_count", "days"],
        },
        handler=_tool_generate_trip_plan_async,
        requires_confirmation=False,
        # Deliberately NOT backgrounded through task_dispatcher: the
        # handler backgrounds itself (its own thread, see
        # plugins/trip_planner.py) and returns its own tailored
        # acknowledgement in well under a second. That is exactly the
        # string this tool needs the model to speak, so it must run
        # inline rather than be replaced by task_dispatcher's generic
        # "started" text.
        background=False,
        label="trip plan from {starting_location} to {destination}",
    ),
    ToolSpec(
        name="check_poll_status_async",
        description=(
            "Use this to check how many people have voted in a trip poll "
            "BEFORE generating the final plan. Call this FIRST, every time, "
            "when the user asks to finalize or generate a plan for a group "
            "or consensus trip that already has a poll running - never call "
            "finalize_trip_plan_async straight away for a finalization "
            "request. After calling this, tell the user the count out loud "
            "and explicitly ask them to confirm before calling "
            "finalize_trip_plan_async."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "trip_id": {
                    "type": "string",
                    "description": (
                        "The trip's id, e.g. from its poll link. Ask the user "
                        "for it if they have not already given it."
                    ),
                },
            },
            "required": ["trip_id"],
        },
        handler=_tool_check_poll_status_async,
        requires_confirmation=False,
        # Foreground on purpose: Step 2 of the two-step finalization rule
        # needs this count in hand to speak it and ask for confirmation -
        # backgrounding it would hand the model "started" instead of the
        # real number it has to say next.
        background=False,
        label="poll status for trip {trip_id}",
    ),
    ToolSpec(
        name="finalize_trip_plan_async",
        description=(
            "Use this to actually generate the final trip itinerary AFTER "
            "the user has confirmed they want to finalize the poll. This "
            "will read the database, process the votes, and send the final "
            "plan via LINE. Do NOT call this on its own for a finalization "
            "request - it is Step 3 of a three-step process: first call "
            "check_poll_status_async, then tell the user the count and ask "
            "them to confirm, and only call this tool once they say yes."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "trip_id": {
                    "type": "string",
                    "description": (
                        "The trip's id, e.g. from its poll link. Ask the user "
                        "for it if they have not already given it."
                    ),
                },
            },
            "required": ["trip_id"],
        },
        handler=_tool_finalize_trip_plan_async,
        requires_confirmation=False,
        # Deliberately NOT backgrounded through task_dispatcher, and NOT
        # gated by requires_confirmation: the user already confirmed in
        # words as part of the two-step SYSTEM_PROMPT rule, so a second,
        # generic "do you authorize running finalize_trip_plan_async?"
        # prompt here would ask the same question twice. The handler
        # backgrounds itself (its own thread, see plugins/trip_planner.py)
        # and returns its own tailored acknowledgement in well under a
        # second - that is exactly the string this tool needs the model
        # to speak, so it must run inline rather than be replaced by
        # task_dispatcher's generic "started" text.
        background=False,
        label="trip finalization for {trip_id}",
    ),
    ToolSpec(
        name="analyze_screen",
        description=(
            "Takes a fresh screenshot of the user's current screen right now "
            "and analyzes it. Use this whenever the question is about what is "
            "visible at this moment - 'what am I looking at', 'read this "
            "code', 'what does this error say', 'which file is open', "
            "'summarise this page'. The screen is captured on demand when you "
            "call this, so what you see is what is on screen right now, and "
            "nothing is captured at any other time. Ask about one thing at a "
            "time."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": (
                        "What to determine from the screenshot, phrased as a "
                        "question, e.g. 'what error is shown in the terminal?'. "
                        "Pass the user's actual question, not a generic one."
                    ),
                },
            },
            "required": ["question"],
        },
        handler=_tool_analyze_screen,
        # Foreground on purpose: the answer IS the reply. Backgrounding
        # it would make Jarvis say "I'm on it" to "what am I looking at".
        requires_confirmation=False,
    ),
    ToolSpec(
        name="create_new_tool",
        description=(
            "Write a brand new tool for yourself, as a Python plugin, when the "
            "user asks for a capability you do not have. The file is saved into "
            "the plugins folder and becomes a real tool you can call on the "
            "NEXT command - not this one. Write a complete, self-contained "
            "module: a top-level `run(...)` function whose keyword arguments "
            "match the input_schema you pass, returning ONE short spoken "
            "sentence, with any imports inside the function. Keep it small and "
            "dependency-free. The user is asked out loud to approve the code "
            "before it is written, so explain in one sentence what you are "
            "about to create. Do not use this for anything an existing tool "
            "already does."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Tool name in lower_snake_case, e.g. 'roll_dice'. "
                        "Becomes the filename."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": (
                        "What the tool does and when to use it - this is what "
                        "you will read later to decide whether to call it."
                    ),
                },
                "input_schema": {
                    "type": "object",
                    "description": (
                        "JSON Schema for the new tool's arguments, same shape "
                        "as this one: an object with properties and required."
                    ),
                },
                "python_code": {
                    "type": "string",
                    "description": (
                        "The module source. Must define a top-level `run` "
                        "function taking the schema's properties as keyword "
                        "arguments and returning a string. A TOOL_SPEC dict is "
                        "added for you if you omit it."
                    ),
                },
            },
            "required": ["name", "description", "python_code"],
        },
        handler=_tool_create_new_tool,
        # The gate that matters: this writes code the process will run.
        requires_confirmation=True,
        confirmation_template=(
            "I want to write a new tool called {name}. Should I create it?"
        ),
    ),
    ToolSpec(
        name="list_capabilities",
        description=(
            "List what you can currently do: built-in tools, plugin tools "
            "written at runtime, whether screen vision is active, and any "
            "background tasks still running. Use it when the user asks what "
            "you can do, what is running, or whether a task finished."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "include_tasks": {
                    "type": "boolean",
                    "description": "Include background task status. Default true.",
                },
            },
        },
        handler=_tool_list_capabilities,
        requires_confirmation=False,
    ),
]

TOOLS_BY_NAME: dict[str, ToolSpec] = {tool.name: tool for tool in TOOLS}


# ---------------------------------------------------------------------
# Confirmation judging
# ---------------------------------------------------------------------

# The spoken yes/no gate used to be a hardcoded word list, which could
# not cope with code-switching ("chai", "krub"), mishearings from an
# English STT model, or an answer phrased as a sentence. A one-shot
# fast-tier call reads the intent instead, and costs a fraction of a
# second on a question the user is already waiting on.
CONFIRM_JUDGE_PROMPT = (
    "The user was asked to confirm an action. Their response is: "
    "'{user_input}'. Does this mean YES/Proceed or NO/Cancel? "
    "Reply with exactly True or False."
)

# Anything slower than this is worse than the word list it replaced, so
# give up and let the caller fall back.
CONFIRM_JUDGE_TIMEOUT = 8.0


def judge_confirmation(user_input: str) -> Optional[bool]:
    """
    Reads a spoken answer to a yes/no gate and returns what it meant.

    Returns True for a clear go-ahead, False for a refusal, and None if
    the model could not be reached at all - the caller decides what an
    unreachable judge means (it must never mean "yes").
    """
    text = (user_input or "").strip()
    if not text:
        return False
    if not _ANTHROPIC_AVAILABLE:
        return None

    key = load_api_key("ANTHROPIC_API_KEY")
    if not key:
        return None

    try:
        client = anthropic.Anthropic(api_key=key, timeout=CONFIRM_JUDGE_TIMEOUT)
        response = client.messages.create(
            model=MODEL,
            max_tokens=8,
            messages=[
                {
                    "role": "user",
                    "content": CONFIRM_JUDGE_PROMPT.format(user_input=text),
                }
            ],
        )
    except Exception as exc:  # noqa: BLE001 - any failure is "no verdict"
        print(f"[Brain] Confirmation judge failed: {type(exc).__name__}: {exc}")
        return None

    verdict = "".join(
        block.text for block in response.content
        if getattr(block, "type", None) == "text"
    ).strip().lower()

    if verdict.startswith("true"):
        return True
    if verdict.startswith("false"):
        return False

    print(f"[Brain] Confirmation judge returned {verdict!r}; treating as no verdict.")
    return None


def anthropic_tool_schemas(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    """Renders ToolSpecs into the Anthropic Messages API tool format."""
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
        }
        for tool in tools
    ]


def gemini_tool_declarations(tools: list[ToolSpec]):
    """Renders ToolSpecs into a Gemini types.Tool with function declarations."""
    return genai_types.Tool(
        function_declarations=[
            genai_types.FunctionDeclaration(
                name=tool.name,
                description=tool.description,
                # parameters_json_schema takes plain JSON Schema, so the
                # same dict feeds both providers unchanged.
                parameters_json_schema=tool.input_schema,
            )
            for tool in tools
        ]
    )


# =====================================================================
# Provider-neutral conversation representation
# =====================================================================
#
# History is stored in this neutral form and translated per provider on
# every request. That is what makes the providers swappable mid-session:
# a conversation started on Claude can finish on Gemini without either
# SDK's message objects leaking into the stored transcript.


@dataclass
class ToolCall:
    """One tool invocation requested by the model."""

    # Anthropic matches results by id; Gemini matches by name. Carry both.
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class ToolResult:
    call: ToolCall
    content: str
    is_error: bool = False


@dataclass
class Turn:
    """
    One entry in the neutral transcript.

    kind is one of:
        "user"       - user text
        "assistant"  - final assistant text
        "tool_calls" - assistant asked for tools (optionally with text)
        "tool_results" - the results handed back
    """

    kind: str
    text: str = ""
    calls: list[ToolCall] = field(default_factory=list)
    results: list[ToolResult] = field(default_factory=list)


class ProviderError(Exception):
    """
    A provider call failed.

    `kind` drives the failover decision, `spoken` is what the user hears
    if no failover is possible.
    """

    def __init__(self, kind: str, spoken: str) -> None:
        super().__init__(spoken)
        self.kind = kind
        self.spoken = spoken

    @property
    def is_fatal_for_provider(self) -> bool:
        """
        True when retrying this provider cannot help.

        Only these justify switching providers. Rate limits, 5xx, and
        connection errors are transient - the SDK already retries them,
        and failing over would hide a blip behind a different bill.
        """
        return self.kind in ("auth", "permission", "not_found", "no_key", "no_sdk")


# =====================================================================
# Providers
# =====================================================================


class _Provider:
    """Common interface: turn a neutral transcript into one model turn."""

    name = "unknown"

    @property
    def available(self) -> bool:
        raise NotImplementedError

    @property
    def model(self) -> str:
        raise NotImplementedError

    def complete(
        self,
        system: str,
        transcript: list[Turn],
        tools: list[ToolSpec],
        model: Optional[str] = None,
        effort: Optional[str] = None,
    ) -> Turn:
        raise NotImplementedError

    def describe_image(self, question: str, image_b64: str, media_type: str) -> str:
        """One-shot vision call. Returns a spoken sentence."""
        raise NotImplementedError


class AnthropicProvider(_Provider):
    """Claude via the Anthropic Messages API."""

    name = "anthropic"

    def __init__(
        self,
        model: str = MODEL,
        max_tokens: int = MAX_TOKENS,
        effort: str = EFFORT,
        api_key: Optional[str] = None,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._effort = effort
        self._client = None
        self.unavailable_reason = ""

        if not _ANTHROPIC_AVAILABLE:
            self.unavailable_reason = f"anthropic SDK not installed ({_ANTHROPIC_IMPORT_ERROR})"
            return

        key = api_key or load_api_key("ANTHROPIC_API_KEY")
        if not key:
            self.unavailable_reason = "no ANTHROPIC_API_KEY configured"
            return

        try:
            self._client = anthropic.Anthropic(api_key=key)
        except Exception as exc:  # noqa: BLE001 - bad key format, proxy issues, ...
            self.unavailable_reason = f"could not create the Anthropic client: {exc}"

    @property
    def available(self) -> bool:
        return self._client is not None

    @property
    def model(self) -> str:
        return self._model

    # -- translation ---------------------------------------------------

    @staticmethod
    def _to_messages(transcript: list[Turn]) -> list[dict[str, Any]]:
        """Neutral transcript -> Anthropic `messages` array."""
        messages: list[dict[str, Any]] = []

        for turn in transcript:
            if turn.kind == "user":
                messages.append({"role": "user", "content": turn.text})

            elif turn.kind == "assistant":
                messages.append({"role": "assistant", "content": turn.text})

            elif turn.kind == "tool_calls":
                blocks: list[dict[str, Any]] = []
                # An empty text block is a 400, so only include real text.
                if turn.text:
                    blocks.append({"type": "text", "text": turn.text})
                blocks.extend(
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": call.args,
                    }
                    for call in turn.calls
                )
                messages.append({"role": "assistant", "content": blocks})

            elif turn.kind == "tool_results":
                # All results for one assistant turn go back in a SINGLE
                # user message. Splitting them across messages teaches the
                # model to stop making parallel calls.
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": result.call.id,
                                "content": result.content,
                                **({"is_error": True} if result.is_error else {}),
                            }
                            for result in turn.results
                        ],
                    }
                )

        return messages

    # -- the call ------------------------------------------------------

    def complete(
        self,
        system: str,
        transcript: list[Turn],
        tools: list[ToolSpec],
        model: Optional[str] = None,
        effort: Optional[str] = None,
    ) -> Turn:
        if self._client is None:
            raise ProviderError("no_key", "My Anthropic model isn't configured.")

        # `model`/`effort` come from the intent router, per question.
        # Nothing is stored on self: the next question re-decides.
        target = model or self._model
        request: dict[str, Any] = {
            "model": target,
            "max_tokens": self._max_tokens,
            "system": system,
            "thinking": THINKING_TOOLS if tools else THINKING_PLAIN,
            "output_config": {"effort": effort or self._effort},
            "messages": self._to_messages(transcript),
        }

        if target in MODELS_WITHOUT_EFFORT:
            # The cheap tier is a 4.5-generation model, which knows
            # neither output_config.effort nor thinking type "adaptive"
            # and returns a 400 for either. Omitting `thinking` is what
            # that generation means by off - and it is only ever sent
            # work the router judged needs no tool call, which is the
            # trade being made here (see _ALWAYS_FAST).
            request.pop("output_config", None)
            request.pop("thinking", None)

        if tools:
            request["tools"] = anthropic_tool_schemas(tools)

        try:
            response = self._client.messages.create(**request)
        except Exception as exc:  # noqa: BLE001 - classified immediately below
            raise self._classify(exc) from exc

        # Safety classifiers can decline a request: HTTP 200, but with
        # stop_reason "refusal" and no usable content. Check before
        # indexing into content, which would otherwise IndexError.
        if getattr(response, "stop_reason", None) == "refusal":
            return Turn(kind="assistant", text="I can't help with that one.")

        text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        ).strip()

        calls = [
            ToolCall(id=block.id, name=block.name, args=dict(block.input or {}))
            for block in response.content
            if getattr(block, "type", None) == "tool_use"
        ]

        if calls:
            return Turn(kind="tool_calls", text=text, calls=calls)

        if getattr(response, "stop_reason", None) == "max_tokens" and text:
            text += " I had to cut that short."

        return Turn(kind="assistant", text=text)

    def describe_image(self, question: str, image_b64: str, media_type: str) -> str:
        """
        One-shot vision call: a question plus one screenshot.

        Deliberately outside the conversation transcript. A 200 KB image
        resent with every subsequent turn would dominate the context
        window and the bill, and the answer - "the file open is
        os_executor.py" - is what belongs in the history, not the pixels.
        """
        if self._client is None:
            raise ProviderError("no_key", "My Anthropic model isn't configured.")

        try:
            response = self._client.messages.create(
                model=MODEL_VISION,
                max_tokens=self._max_tokens,
                system=VISION_SYSTEM_PROMPT,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": image_b64,
                            },
                        },
                        {"type": "text", "text": question},
                    ],
                }],
            )
        except Exception as exc:  # noqa: BLE001
            raise self._classify(exc) from exc

        return "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        ).strip()

    def _classify(self, exc: Exception) -> ProviderError:
        """
        Maps SDK exceptions to a kind plus a short spoken sentence, most
        specific first. The detail goes to the console; the user hears a
        summary.
        """
        print(f"[Brain] Anthropic call failed: {type(exc).__name__}: {exc}")

        if not _ANTHROPIC_AVAILABLE:  # pragma: no cover - defensive
            return ProviderError("no_sdk", "My language model isn't available right now.")

        if isinstance(exc, anthropic.AuthenticationError):
            return ProviderError(
                "auth",
                "My Anthropic key was rejected. Check the key in the .env file.",
            )
        if isinstance(exc, anthropic.PermissionDeniedError):
            return ProviderError(
                "permission", "My Anthropic key doesn't have permission for that model."
            )
        if isinstance(exc, anthropic.NotFoundError):
            return ProviderError(
                "not_found",
                f"The model {self._model} wasn't found. It may have been retired.",
            )
        if isinstance(exc, anthropic.RateLimitError):
            return ProviderError(
                "rate_limit", "I'm being rate limited. Give me a moment and ask again."
            )
        if isinstance(exc, anthropic.BadRequestError):
            return ProviderError(
                "bad_request", "That request was rejected as invalid. See the console."
            )
        if isinstance(exc, anthropic.APIConnectionError):
            return ProviderError(
                "connection", "I couldn't reach the network. Check your internet connection."
            )
        if isinstance(exc, anthropic.APIStatusError):
            status = getattr(exc, "status_code", "unknown")
            if isinstance(status, int) and status >= 500:
                return ProviderError("server", "The service is having trouble. Try again shortly.")
            return ProviderError("unknown", f"The API returned an error, status {status}.")
        return ProviderError("unknown", "Something went wrong while I was thinking.")


class GeminiProvider(_Provider):
    """Gemini via the google-genai SDK."""

    name = "gemini"

    def __init__(
        self,
        model: Optional[str] = None,
        max_tokens: int = MAX_TOKENS,
        api_key: Optional[str] = None,
    ) -> None:
        self._model = model or _env("GEMINI_MODEL", GEMINI_MODEL)
        self._max_tokens = max_tokens
        self._client = None
        self.unavailable_reason = ""

        if not _GEMINI_AVAILABLE:
            self.unavailable_reason = f"google-genai SDK not installed ({_GEMINI_IMPORT_ERROR})"
            return

        # GOOGLE_API_KEY is the SDK's own convention; accept either.
        key = api_key or load_api_key("GEMINI_API_KEY") or load_api_key("GOOGLE_API_KEY")
        if not key:
            self.unavailable_reason = "no GEMINI_API_KEY configured"
            return

        try:
            self._client = genai.Client(api_key=key)
        except Exception as exc:  # noqa: BLE001
            self.unavailable_reason = f"could not create the Gemini client: {exc}"

    @property
    def available(self) -> bool:
        return self._client is not None

    @property
    def model(self) -> str:
        return self._model

    # -- translation ---------------------------------------------------

    @staticmethod
    def _to_contents(transcript: list[Turn]):
        """Neutral transcript -> Gemini `contents` list."""
        contents = []

        for turn in transcript:
            if turn.kind == "user":
                contents.append(
                    genai_types.Content(
                        role="user", parts=[genai_types.Part.from_text(text=turn.text)]
                    )
                )

            elif turn.kind == "assistant":
                contents.append(
                    genai_types.Content(
                        role="model", parts=[genai_types.Part.from_text(text=turn.text)]
                    )
                )

            elif turn.kind == "tool_calls":
                parts = []
                if turn.text:
                    parts.append(genai_types.Part.from_text(text=turn.text))
                parts.extend(
                    genai_types.Part(
                        function_call=genai_types.FunctionCall(
                            name=call.name, args=call.args
                        )
                    )
                    for call in turn.calls
                )
                contents.append(genai_types.Content(role="model", parts=parts))

            elif turn.kind == "tool_results":
                # Gemini matches results to calls by function name, not by
                # id, so the id on ToolCall is unused on this path.
                contents.append(
                    genai_types.Content(
                        role="user",
                        parts=[
                            genai_types.Part.from_function_response(
                                name=result.call.name,
                                response={"result": result.content},
                            )
                            for result in turn.results
                        ],
                    )
                )

        return contents

    # -- the call ------------------------------------------------------

    def complete(
        self,
        system: str,
        transcript: list[Turn],
        tools: list[ToolSpec],
        model: Optional[str] = None,
        effort: Optional[str] = None,
    ) -> Turn:
        if self._client is None:
            raise ProviderError("no_key", "My Gemini model isn't configured.")

        config_kwargs: dict[str, Any] = {
            "system_instruction": system,
            "max_output_tokens": self._max_tokens,
        }
        if tools:
            config_kwargs["tools"] = [gemini_tool_declarations(tools)]
            # The SDK will otherwise try to invoke Python callables itself.
            # This module runs tools by hand so the confirmation gate and
            # the console logging stay in one place.
            config_kwargs["automatic_function_calling"] = (
                genai_types.AutomaticFunctionCallingConfig(disable=True)
            )

        try:
            response = self._client.models.generate_content(
                model=model or self._model,
                contents=self._to_contents(transcript),
                config=genai_types.GenerateContentConfig(**config_kwargs),
            )
        except Exception as exc:  # noqa: BLE001 - classified immediately below
            raise self._classify(exc) from exc

        text_parts: list[str] = []
        calls: list[ToolCall] = []

        for index, candidate in enumerate(response.candidates or []):
            content = getattr(candidate, "content", None)
            for part in getattr(content, "parts", None) or []:
                # Thought parts are reasoning, not the answer.
                if getattr(part, "thought", False):
                    continue
                if getattr(part, "text", None):
                    text_parts.append(part.text)
                call = getattr(part, "function_call", None)
                if call is not None and call.name:
                    calls.append(
                        ToolCall(
                            # Gemini has no call id; synthesise a stable one
                            # so the neutral transcript stays translatable
                            # back to Anthropic mid-conversation.
                            id=f"gemini_{index}_{len(calls)}_{call.name}",
                            name=call.name,
                            args=dict(call.args or {}),
                        )
                    )
            break  # only the first candidate is ever used

        text = "".join(text_parts).strip()

        if calls:
            return Turn(kind="tool_calls", text=text, calls=calls)
        return Turn(kind="assistant", text=text)

    def describe_image(self, question: str, image_b64: str, media_type: str) -> str:
        """One-shot vision call. See the Anthropic version for the why."""
        if self._client is None:
            raise ProviderError("no_key", "My Gemini model isn't configured.")

        try:
            response = self._client.models.generate_content(
                model=self._model,
                contents=[
                    genai_types.Part.from_bytes(
                        data=base64.b64decode(image_b64), mime_type=media_type
                    ),
                    question,
                ],
                config=genai_types.GenerateContentConfig(
                    system_instruction=VISION_SYSTEM_PROMPT,
                    max_output_tokens=self._max_tokens,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            raise self._classify(exc) from exc

        return (getattr(response, "text", "") or "").strip()

    def _classify(self, exc: Exception) -> ProviderError:
        """Maps google-genai exceptions to a kind plus a spoken sentence."""
        print(f"[Brain] Gemini call failed: {type(exc).__name__}: {exc}")

        if not _GEMINI_AVAILABLE:  # pragma: no cover - defensive
            return ProviderError("no_sdk", "My language model isn't available right now.")

        if isinstance(exc, genai_errors.APIError):
            code = getattr(exc, "code", None)
            if code in (401, 403):
                return ProviderError(
                    "auth", "My Gemini key was rejected. Check the key in the .env file."
                )
            if code == 404:
                return ProviderError(
                    "not_found",
                    f"The model {self._model} wasn't found. Set GEMINI_MODEL in the .env file.",
                )
            if code == 429:
                return ProviderError(
                    "rate_limit", "I'm being rate limited. Give me a moment and ask again."
                )
            if code == 400:
                return ProviderError(
                    "bad_request", "That request was rejected as invalid. See the console."
                )
            if isinstance(code, int) and code >= 500:
                return ProviderError("server", "The service is having trouble. Try again shortly.")
            return ProviderError("unknown", f"Gemini returned an error, status {code}.")

        return ProviderError("unknown", "Something went wrong while I was thinking.")


# =====================================================================
# The supervisor
# =====================================================================


class LLMBrain:
    """
    A manager over the LLM providers and the tool layer.

    Every failure path returns a short spoken-friendly string instead of
    raising, so a network blip or an expired key degrades the assistant
    to "I couldn't reach my language model" rather than crashing the
    wake word loop.
    """

    def __init__(
        self,
        model: str = MODEL,
        system_prompt: str = SYSTEM_PROMPT,
        max_tokens: int = MAX_TOKENS,
        effort: str = EFFORT,
        max_history_turns: int = MAX_HISTORY_TURNS,
        api_key: Optional[str] = None,
        tools: Optional[list[ToolSpec]] = None,
        confirm: Optional[Callable[[str], bool]] = None,
        max_steps: int = SUPERVISOR_MAX_STEPS,
    ) -> None:
        self._system_prompt = system_prompt
        self._max_history_turns = max_history_turns
        self._max_steps = max_steps
        self._tools = TOOLS if tools is None else tools
        self._confirm = confirm

        self._history: list[Turn] = []
        self._unavailable_reason = ""

        # Feature 3: which column of ROUTING_TABLE this brain reads.
        # Set once the provider is chosen and again on failover, because
        # sending "claude-opus-5" to Gemini is a 404.
        self._route_key = "anthropic"

        # Feature 5: plugins are loaded now and re-scanned every turn.
        self._plugin_tools: list[ToolSpec] = []

        # Feature 2 bookkeeping: what went to the background, so the same
        # side effect cannot be fired twice (see _execute).
        self._recent_dispatches: dict[str, float] = {}
        self._dispatched_this_turn: list[str] = []

        # Anthropic first, Gemini as the alternative. Both are built so a
        # runtime failover has somewhere to go without a reconnect.
        self._primary = AnthropicProvider(
            model=model, max_tokens=max_tokens, effort=effort, api_key=api_key
        )
        self._secondary = GeminiProvider(max_tokens=max_tokens)

        if self._primary.available:
            self._provider: Optional[_Provider] = self._primary
        elif self._secondary.available:
            print(f"[Brain] Anthropic unavailable ({self._primary.unavailable_reason}).")
            self._provider = self._secondary
        else:
            self._provider = None

        # Feature 4: the vision tool needs a provider to send frames to.
        global _VISION_PROVIDER
        _VISION_PROVIDER = self._provider

        # The routing column has to match whichever provider actually won.
        if self._provider is self._secondary:
            self._route_key = "gemini"

        self._refresh_plugins(verbose=True)
        self._report_startup()

    def _report_startup(self) -> None:
        """Announces which provider won and why, once, at construction."""
        if self._provider is None:
            self._unavailable_reason = (
                f"anthropic: {self._primary.unavailable_reason}; "
                f"gemini: {self._secondary.unavailable_reason}"
            )
            print("-" * 68)
            print("  NO LANGUAGE MODEL PROVIDER CONFIGURED")
            print("-" * 68)
            print(missing_key_instructions())
            print("-" * 68)
            print("[Brain] Conversational answers are disabled; OS commands still work.")
            return

        tools = self._active_tools()
        print(
            f"[Brain] Provider: {self._provider.name} "
            f"(model: {self._provider.model}), "
            f"{len(tools)} tools: {', '.join(t.name for t in tools)}."
        )
        table = ", ".join(
            f"{intent}={route[self._route_key]}"
            for intent, route in ROUTING_TABLE.items()
        )
        print(f"[Brain] Routing table ({self._route_key}): {table} - picked per question.")
        standby = self._secondary if self._provider is self._primary else self._primary
        if standby.available:
            print(f"[Brain] Standby provider: {standby.name} ({standby.model}).")
        else:
            print(f"[Brain] No standby provider ({standby.unavailable_reason}).")

    # -- capabilities --------------------------------------------------

    @property
    def available(self) -> bool:
        """True when the brain can actually answer questions."""
        return self._provider is not None

    @property
    def unavailable_reason(self) -> str:
        return self._unavailable_reason

    @property
    def provider_name(self) -> str:
        return self._provider.name if self._provider else "none"

    @property
    def model(self) -> str:
        return self._provider.model if self._provider else "none"

    @property
    def tool_names(self) -> list[str]:
        return [tool.name for tool in self._active_tools()]

    def _active_tools(self) -> list[ToolSpec]:
        """Built-in tools plus whatever is currently in plugins/."""
        return list(self._tools) + list(self._plugin_tools)

    def _refresh_plugins(self, verbose: bool = False) -> None:
        """
        Re-scans plugins/ so a tool written during the last command is
        callable in this one. Cheap (a directory stat unless something
        changed) and never fatal: a broken plugin is skipped, not raised.
        """
        try:
            self._plugin_tools = plugin_loader.load_plugins(verbose=verbose)
        except Exception as exc:  # noqa: BLE001 - plugins must never break the brain
            print(f"[Brain] Plugin scan failed: {type(exc).__name__}: {exc}")
            self._plugin_tools = []

    # -- history -------------------------------------------------------

    def _trim_history(self) -> None:
        """
        Keeps the most recent turns.

        Both providers require the transcript to open on a user turn, so
        trim to that boundary rather than slicing blindly.
        """
        max_messages = self._max_history_turns * 2
        if len(self._history) <= max_messages:
            return
        self._history = self._history[-max_messages:]
        while self._history and self._history[0].kind != "user":
            self._history.pop(0)

    def reset(self) -> None:
        """Clears conversation memory - a fresh topic, same session."""
        self._history.clear()
        print("[Brain] Conversation history cleared.")

    @property
    def turn_count(self) -> int:
        return len(self._history) // 2

    # -- tool execution ------------------------------------------------

    def _ask_confirmation(self, question: str) -> bool:
        """
        Runs the human-in-the-loop gate for a side-effecting tool.

        Falls back to a typed console prompt when no confirmer was wired
        in, so the gate can never be skipped just because this module was
        constructed without a voice engine.
        """
        if self._confirm is not None:
            return bool(self._confirm(question))

        try:
            answer = input(f"[Brain] {question} [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return answer in ("y", "yes")

    def _execute(self, call: ToolCall) -> ToolResult:
        """
        Runs one tool call. Never raises: a failure becomes a tool result
        the model can read and explain.
        """
        # Search the ACTIVE set, not just the built-ins: a plugin
        # written last turn is a real tool this turn, and looking it up
        # here is the difference between "unknown tool" and it working.
        tool = next((t for t in self._active_tools() if t.name == call.name), None)
        if tool is None:
            tool = TOOLS_BY_NAME.get(call.name)

        if tool is None:
            print(f"[Brain] Tool call rejected: unknown tool {call.name!r}.")
            return ToolResult(
                call=call,
                content=f"There is no tool called {call.name}.",
                is_error=True,
            )

        print(f"[Brain] Tool call: {call.name}({json.dumps(call.args, default=str)})")

        if tool.requires_confirmation:
            question = tool.confirmation_question(call.args)
            question = self._augment_confirmation(tool, call, question)
            print(f"[Brain] Confirmation required for {call.name}.")
            if not self._ask_confirmation(question):
                print(f"[Brain] Tool call denied by the user: {call.name}.")
                return ToolResult(
                    call=call,
                    content=(
                        "The user declined to authorize this action, so nothing "
                        "was executed. Acknowledge that and do not retry it."
                    ),
                )
            print(f"[Brain] Tool call authorized: {call.name}.")

        # Feature 2: slow, fire-and-forget work leaves the listening
        # thread here. The model gets an immediate "started" result and
        # tells the user "I'm on it"; the real outcome is announced by
        # session_manager when the dispatcher reports it.
        if tool.background:
            label = tool.spoken_label(call.args)

            # Replay guard. A backgrounded call returns "started", not
            # "succeeded", and a model reading that on the next turn can
            # decide the job never happened and fire it again - which,
            # for a webhook, means the row is written to the database
            # twice. Identical calls inside the window are refused.
            fingerprint = f"{tool.name}:{json.dumps(call.args, sort_keys=True, default=str)}"
            previous = self._recent_dispatches.get(fingerprint)
            now = time.monotonic()
            if previous is not None and now - previous < DISPATCH_REPLAY_WINDOW:
                print(f"[Brain] Duplicate background call refused: {tool.name}.")
                return ToolResult(
                    call=call,
                    content=(
                        f"ALREADY RUNNING: the {label} was dispatched "
                        f"{now - previous:.0f} seconds ago with exactly these "
                        "values and has not been repeated. Do not call it again. "
                        "Tell the user it is already in progress."
                    ),
                )
            self._recent_dispatches[fingerprint] = now
            task_dispatcher.dispatcher().submit(
                tool.handler, name=tool.name, label=label, **call.args
            )
            self._dispatched_this_turn.append(label)
            return ToolResult(
                call=call,
                content=(
                    f"STARTED IN THE BACKGROUND: the {label} is now running and "
                    "will report back when it finishes. Tell the user you are on "
                    "it in one short sentence and ask if there is anything else. "
                    "Do NOT claim it succeeded, and do not wait for it."
                ),
            )

        try:
            result = tool.handler(**call.args)
        except TypeError as exc:
            # Wrong or missing arguments from the model - recoverable, so
            # hand it back rather than aborting the turn.
            print(f"[Brain] Tool {call.name} got bad arguments: {exc}")
            return ToolResult(
                call=call,
                content=f"Those arguments were not valid for {call.name}: {exc}",
                is_error=True,
            )
        except Exception as exc:  # noqa: BLE001 - a tool must not kill the loop
            print(f"[Brain] Tool {call.name} raised: {type(exc).__name__}: {exc}")
            return ToolResult(
                call=call,
                content=f"{call.name} failed: {type(exc).__name__}: {exc}",
                is_error=True,
            )

        text = str(result)
        print(f"[Brain] Tool result: {call.name} -> {text[:160]}")
        return ToolResult(call=call, content=text)

    # -- failover ------------------------------------------------------

    def _augment_confirmation(self, tool: ToolSpec, call: ToolCall, question: str) -> str:
        """
        Adds what the user needs to hear before saying yes.

        For create_new_tool that is the code itself: it goes to the
        console in full, and any flagged behaviour (shell commands, file
        writes, network sends) is named in the spoken question. Approving
        code you have not seen is not consent.
        """
        if tool.name != "create_new_tool":
            return question

        code = str(call.args.get("python_code", ""))
        print("-" * 68)
        print(f"  PROPOSED NEW TOOL: {call.args.get('name', '?')}")
        print("-" * 68)
        print(code)
        print("-" * 68)

        risky = plugin_loader.risky_behaviours(code)
        if risky:
            print(f"[Plugins] Flagged behaviour: {', '.join(risky)}.")
            return (
                f"{question} Be aware, the code {' and '.join(risky)}. "
                "It is printed in full in the console."
            )
        return f"{question} The code is printed in the console."

    def _failover(self) -> bool:
        """
        Switches to the standby provider. Returns False if there isn't one.
        """
        standby = self._secondary if self._provider is self._primary else self._primary
        if not standby.available:
            return False

        previous = self._provider.name if self._provider else "none"
        self._provider = standby
        # Vision follows the active provider, and so does the route table.
        global _VISION_PROVIDER
        _VISION_PROVIDER = standby
        self._route_key = "anthropic" if standby is self._primary else "gemini"
        print(f"[Brain] FAILOVER: {previous} -> {standby.name} ({standby.model}).")
        return True

    # -- the supervisor loop -------------------------------------------

    def _route(self, intent: str) -> tuple[Optional[str], Optional[str]]:
        """
        (model, effort) for one intent on the ACTIVE provider.

        The model comes back as None for the standard tier, which is
        deliberate: that tier is whatever the provider was constructed
        with, and that may be an .env override. Passing None keeps the
        user's choice in play instead of overwriting it with this
        module's default on every ordinary question.
        """
        route = ROUTING_TABLE.get(intent) or ROUTING_TABLE[INTENT_STANDARD]
        model: Optional[str] = route[self._route_key]
        if model == ROUTING_TABLE[INTENT_STANDARD][self._route_key]:
            model = None
        return model, route["effort"]

    def ask(self, user_text: str) -> str:
        """
        Answers `user_text`, calling tools as needed, and returns one
        plain-text reply ready for the voice engine.

        Never raises: every error becomes a short spoken sentence.
        """
        if not user_text or not user_text.strip():
            return "I didn't catch a question."

        if not self.available:
            return (
                "My language model isn't configured. Add an Anthropic or Gemini "
                "API key to the .env file and restart me. The console has the steps."
            )

        # Feature 5: pick up any tool written during the previous turn.
        self._refresh_plugins()
        tools = self._active_tools()

        # Feature 3: route this question by intent, BEFORE the turn is
        # sent. Decided once per question and held for all of its tool
        # round trips, so a heavy request does not silently drop back to
        # a cheaper model halfway through answering itself.
        self._dispatched_this_turn = []

        intent, why = _classify_intent(user_text)
        model, effort = self._route(intent)
        print(
            f"[Brain] Routing: {intent.upper()} ({why}) "
            f"-> {model or self._provider.model}, effort {effort}."
        )

        # A working copy: only the question and the final answer are kept
        # in history. The tool round trips are scaffolding for this one
        # answer and would otherwise dominate the context window.
        working: list[Turn] = list(self._history)
        working.append(Turn(kind="user", text=user_text.strip()))

        for step in range(1, self._max_steps + 1):
            try:
                turn = self._call_provider(working, tools, model, effort)
            except ProviderError as exc:
                return exc.spoken

            if turn.kind == "assistant":
                reply = turn.text.strip()
                if not reply:
                    return "I didn't get an answer back. Try asking again."

                # The transcript keeps the question and the answer, not
                # the tool scaffolding - so a backgrounded action would
                # be invisible next turn, and "on it" reads like nothing
                # happened. Record it for the model only; the user hears
                # `reply` unchanged.
                remembered = reply
                if self._dispatched_this_turn:
                    remembered += (
                        " [Already dispatched in the background: "
                        + ", ".join(self._dispatched_this_turn)
                        + ". Do not trigger these again.]"
                    )

                self._history.append(Turn(kind="user", text=user_text.strip()))
                self._history.append(Turn(kind="assistant", text=remembered))
                self._trim_history()
                return reply

            # turn.kind == "tool_calls"
            print(
                f"[Brain] Step {step}/{self._max_steps}: "
                f"{len(turn.calls)} tool call(s) requested."
            )
            working.append(turn)
            working.append(
                Turn(kind="tool_results", results=[self._execute(c) for c in turn.calls])
            )

        print(f"[Brain] Supervisor hit the {self._max_steps}-step ceiling.")
        return (
            "I went back and forth on that too many times without landing on an "
            "answer. Try asking it a different way."
        )

    def _call_provider(
        self,
        working: list[Turn],
        tools: Optional[list[ToolSpec]] = None,
        model: Optional[str] = None,
        effort: Optional[str] = None,
    ) -> Turn:
        """
        One provider round trip, with a single failover retry.

        Only errors that retrying cannot fix trigger the switch - see
        ProviderError.is_fatal_for_provider.
        """
        assert self._provider is not None
        tools = self._active_tools() if tools is None else tools

        try:
            return self._provider.complete(self._system_prompt, working, tools, model, effort)
        except ProviderError as exc:
            if not exc.is_fatal_for_provider:
                raise
            print(f"[Brain] {self._provider.name} is unusable ({exc.kind}): {exc.spoken}")
            if not self._failover():
                raise
            # The standby provider names its models differently, so the
            # Anthropic-flavoured override is dropped rather than sent
            # somewhere it means nothing.
            return self._provider.complete(self._system_prompt, working, tools, None, effort)


if __name__ == "__main__":
    brain = LLMBrain()
    print(f"[Brain] available={brain.available}")
    if not brain.available:
        raise SystemExit(1)

    print("[Brain] Type a question, or 'quit' to exit.\n")
    while True:
        try:
            question = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if question.lower() in ("quit", "exit", "stop"):
            break
        if not question:
            continue
        print(f"Jarvis: {brain.ask(question)}\n")
