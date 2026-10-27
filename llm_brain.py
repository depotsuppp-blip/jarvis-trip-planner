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

Several things were added to this module beyond plain tool calling:

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

  FRONT DOOR  before the supervisor loop, one tool-less call to the cheap
      tier reads the question against FRONT_DOOR_PROMPT and answers with
      JSON: {intent_type, route_to, response}. route_to "SELF" means
      response is spoken as it stands, so greetings and general
      conversation never pay for a tool-carrying supervisor turn.
      "ORCHESTRATOR" (trip planning, anything that needs a tool) hands
      the question on to the supervisor loop below, which is where the
      orchestration actually lives. Tool commands, reasoning-tier
      questions and answers to something Jarvis just asked never reach
      it - see LLMBrain._front_door. JARVIS_FRONT_DOOR=0 turns it off.

  LINK GUARD  a trip link or trip id in a reply must come from a tool that
      ran during the same request (or from the user's own message), never
      from the earlier conversation: a model that has seen last week's trip
      link will sometimes hand it out for a new trip. link_guard.py finds
      the references; LLMBrain.ask gives the model one chance to fix a bad
      reply and otherwise says it has no link - the guarantee behind the
      prompt rule, because a prompt is only a request.

  MCP CLIENT  tools served by other programs over the Model Context
      Protocol. mcp_client.py starts the bundled servers - local_fs,
      web_search, fetch (read a URL), memory (long-term facts about the
      person) and data (a SQLite workspace for CSV/JSON number crunching),
      none of which needs a key or an account - and those listed in
      mcp_servers.json, asks each for its tools (tools/list) and returns
      them as ToolSpecs named mcp__<server>__<tool>; calling one forwards a
      tools/call and the text comes back to the model labelled as external
      data. memory and data keep one store per person (the voice assistant
      and JARVIS_OWNER_USER_ID are the owner). The snapshot is re-read every
      turn like plugins. A tool that is neither read-only nor marked as only
      adding things asks for confirmation (the web server declines those).
      The front door is told what each server is for, so it hands those
      requests on. JARVIS_MCP=0 turns it off, JARVIS_MCP_CORE=0 turns off
      just the bundled ones.

  WHEN AND WHERE  every call tells the model the date, the time and the
      user's location (Bangkok, Thailand unless JARVIS_LOCATION and
      JARVIS_TIMEZONE say otherwise) in a short system block after the
      cached persona - and, when a web search tool is connected, that it
      must search for anything real-time (weather, news, prices, places)
      instead of saying it has no tool for it (turn_context, REALTIME_RULE).

  THE TEAM  specialist agents (agents/) that the brain - the manager -
      delegates to, each on its own models with a fixed fallback order:
      the Head of Data & Documents (Gemini Pro -> Groq; owns the local_fs
      and data servers), the Head of Research (Gemini Flash; owns
      web_search and fetch) and the Head of Productivity (GPT-4o-mini ->
      Gemini Flash; schedules and to-dos as JSON drafts), each ending on
      Jarvis's own Gemini model as a last resort. The manager holds
      delegate_to_data / _research / _productivity instead of those tools,
      writes each a self-contained task (they do not see the conversation)
      and answers the user from their reports (TEAM_RULE). An agent with no
      usable model, or whose servers are down, is left out and its tools
      stay with the manager. JARVIS_AGENTS=0 turns the team off.

-------------------------------------------------------------------
PROVIDERS
-------------------------------------------------------------------
Two backends, tried in order:

    1. Anthropic Claude   (ANTHROPIC_API_KEY)
    2. Google Gemini      (GEMINI_API_KEY)

Gemini is used when Anthropic has no key configured, and is failed over
to at runtime when Anthropic cannot answer:

    rejected key or model             for good (until restart)
    out of credit (400 "credit
      balance is too low", 402)       for 15 minutes, then Anthropic is tried again
    rate limited (429), overloaded
      or failing (5xx, 529)           for 60 seconds, then Anthropic again

The SDK has already retried a 429/5xx before it reaches this point. A
network failure and a bad request do NOT fail over: Gemini would fail the
same way. The switch happens at the failing call, so the question being
asked is still answered - the transcript is provider-neutral (Turn), and
a model name meant for the other provider is dropped, not sent.

Gemini's models are chosen to answer NOW (GEMINI_MODEL below): pinned
versions retire under us - gemini-1.5-flash is gone, and gemini-2.5-flash
was closed to new API users - and a standby that 404s or times out is no
standby. A model that is busy or out of quota on the key falls through to
the next one within the same call, and Gemini 3's "thought signatures" go
back with each tool call (ToolCall.signature) - without them every second
step of a tool-using answer is a 400.

-------------------------------------------------------------------
MODEL CHOICE - READ THIS
-------------------------------------------------------------------
The Anthropic path targets "claude-sonnet-5", not "claude-3-5-sonnet",
and "claude-haiku-4-5-20251001", not "claude-3-haiku-20240307". Every model in
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
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Callable, Optional

import link_guard
import llm_budget
import mcp_client
import plugin_loader
import request_context
import rich_reply
import system_status
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

MODEL_SIMPLE = "claude-haiku-4-5-20251001"   # cheap tier, for talk that needs no thought
MODEL = "claude-sonnet-5"           # standard tier, and the default
MODEL_DEEP = "claude-opus-5"        # reasoning tier, for heavy requests

# Gemini's equivalents, for the failover provider. Flash covers both
# lower tiers: Gemini prices them close enough together that a third
# name would buy nothing and be one more model ID to keep alive.
# Aliases where possible: "-latest" always names Google's current model, so a
# retirement cannot break the standby (checked 2026-10-26: 1.5-flash is gone,
# 2.5-flash answers 404 "no longer available to new users"). Chosen by a
# measurement the same day, three tool calls each with a 20 s limit:
#     gemini-flash-lite-latest   0.7-1.2 s, 3 of 3
#     gemini-3.5-flash           5-7 s, 2 of 3 (one 504)
#     gemini-flash-latest        504 DEADLINE_EXCEEDED, 0 of 3
#     gemini-pro-latest          429: no quota on this (free) key
# A standby must answer now, so the default is Flash-Lite; the deep tier asks
# Flash first and drops down when it is busy (GeminiProvider.complete).
# GEMINI_MODEL in .env pins another default.
GEMINI_MODEL = "gemini-flash-lite-latest"
GEMINI_MODEL_DEEP = "gemini-flash-latest"
# Tried when the model asked for and the default are both busy or out of quota.
GEMINI_MODEL_LAST_RESORT = "gemini-3.5-flash"
# Per attempt (milliseconds). A busy model answers 504 after ~18 s anyway.
GEMINI_TIMEOUT_MS = 20_000
# The placeholder signature Google documents for a function call the model did
# not make itself (see ToolCall.signature): it skips the signature check.
GEMINI_FOREIGN_CALL_SIGNATURE = b"skip_thought_signature_validator"

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

# System prompt for the front door (see LLMBrain._front_door). The first
# two paragraphs are the router brief as written; everything after them
# is what this deployment needs on top. The router has no tools, so the
# worst thing it can do is answer something that needed one, and the
# reply is spoken verbatim, so it needs the same voice rules as the main
# persona. SYSTEM_PROMPT stays the source of truth for those rules; this
# is the short form of them.
FRONT_DOOR_PROMPT = """You are JARVIS's Front-Door Router. Analyze the user's input and classify the intent. If the input is a greeting or general conversation, provide a helpful response. If the input requires complex trip planning or agent coordination, leave the response null and set the route_to flag to "ORCHESTRATOR".

Output JSON format:
{ "intent_type": "CHAT" | "TRIP_PLANNING", "route_to": "SELF" | "ORCHESTRATOR", "response": "string or null" }

How this deployment reads your output:

- Reply with the JSON object and nothing else: no code fence, no commentary before or after it.
- Use route_to "SELF" only with intent_type "CHAT" and a real answer in response. Every other case is route_to "ORCHESTRATOR" with response null.
- intent_type says what the input is about and route_to says who answers it, so they are separate decisions. "TRIP_PLANNING" is only for input about a trip: planning one, its group poll, voting, or finalizing the plan. Every other input is "CHAT", including a request that needs a tool, which is "CHAT" with route_to "ORCHESTRATOR".
- The assistant behind you has tools that you do not. It logs and reads Subtrack expenses and records, runs n8n workflows, looks at the user's screen, sends LINE messages, starts and stops apps and servers, plans trips and trip polls, and reports what it can do and what is still running. A request that needs one of those, or live information about the user's own systems, is never yours to answer: route it to "ORCHESTRATOR" with response null. Never say or imply that you have done, started, or checked anything.
- If the input answers a question the assistant just asked, or accepts or declines something it offered, route it to "ORCHESTRATOR".
- Never write a trip link or a trip id, and never say a trip was created or is ready: you cannot make one, and a link from earlier in the conversation belongs to an earlier trip. Anything about planning, creating or finding a trip, a poll or a link is route_to "ORCHESTRATOR" with response null.

When you answer, the response is converted to speech and played through a speaker, so:

- Speak English only, even if the user spoke Thai or a mix of Thai and English. Never emit Thai script.
- Lead with the answer, in one to three short sentences of plain spoken prose, and add context only if it changes what the user would do next. No markdown, lists, emoji, asterisks, code, file paths, or URLs.
- A greeting or a thank-you gets a brief natural reply and nothing else, such as "Hello." or "You're welcome." Do not end on a question or an offer of help ("How can I help?", "What do you need?"). If the input is only the name "Jarvis", reply "Yes?" or "I'm listening."
- If you are unsure of a figure, a clause number, or a fact, say so plainly instead of inventing one."""

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

# ...and how much text they may be. A turn count alone let a long chat - pasted
# text, long answers, search results written out - resend tens of thousands of
# tokens with every call, and a tool loop makes several calls per question.
# Earlier messages are cut to a size (the newest exchange less, since "and the
# second one?" needs it), then the oldest are dropped until the whole fits.
# A file's "[System Context: ...]" line is never cut: it is how a follow-up
# finds the file again - the model re-reads what it needs with its tools
# instead of carrying the document from turn to turn.
HISTORY_CHAR_BUDGET = 16_000   # all earlier turns together, about 4k tokens
HISTORY_MESSAGE_CHARS = 2_000  # one earlier message
HISTORY_LAST_CHARS = 6_000     # each message of the newest exchange

# How long a failover that should pass lasts before Anthropic is tried again
# (see LLMBrain._maybe_fail_back). Kinds not listed hold until restart.
FAILBACK_SECONDS = {"billing": 15 * 60, "rate_limit": 60, "server": 60}

# Notes the brain itself adds to a message, kept whole when the message is cut:
# where an attached file is (api_server.py / attachments.py) and what already
# ran in the background (LLMBrain.ask).
_KEPT_NOTES = re.compile(r"\[(?:System Context|Already dispatched)[^\]]*\]")


def _shorten(text: str, limit: int) -> str:
    """`text` cut to about `limit` characters, its kept notes intact and the cut said."""
    if len(text) <= limit:
        return text
    notes = _KEPT_NOTES.findall(text)
    body = _KEPT_NOTES.sub(" ", text).strip()
    room = max(200, limit - sum(len(note) + 2 for note in notes))
    if len(body) > room:
        body = f"{body[:room].rstrip()} [... {len(body) - room} more characters left out of the history]"
    return "\n\n".join(part for part in (body, *notes) if part)

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
details to fulfill a tool (e.g., asking which trip a poll belongs to), \
your response MUST contain ONLY that specific question. You are strictly \
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
current state of one of those systems, on real-time information (see the \
real-time rule at the end), or when the user is asking for something to \
be run or logged.
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

Planning a trip: generate_trip_plan_async needs three facts, and the user \
must have told you all three - in this message or earlier in this \
conversation - before you call it: the destination, how long the trip is \
(a number of days, or dates you can count into days, counting the first \
and the last day: 5 to 8 November is 4 days, not 3), and how many people \
are going (the user included: "me and 3 \
friends" is 4, "just me" is 1, "4 friends" on its own is a group of 4). \
NEVER assume, default or guess any of them - not "3 days", not "1 \
person", not "a long weekend". If one or more is missing, do not call the \
tool: ask ONE short question that names everything still missing, such as \
"How many days are you going, and how many of you?", then stop and wait \
for the answer. Details the user gave you earlier still count, so only ask \
for what is really missing, and once the answers are in, call the tool. \
Do not ask anything beyond those three - not where they are travelling \
from, not budget. Choose planning_type from what they said: a trip with \
friends, family or colleagues who should vote is 'consensus'; a trip where \
they already have places in mind to add is 'solo_draft'; anything else is \
'solo_blank', which plans the whole trip for them. When the tool returns a \
link, the link is the answer: give it to the user exactly as the tool \
wrote it. If the tool reports an error, say what went wrong in one \
sentence and never claim a plan exists or make up a link. If the tool says \
it did not start because something is missing, ask the user for exactly \
that and do not retry with a guess.

A trip link is something a tool hands you, never something you remember. \
Write a trip link or a trip id only if a tool returned it during this \
very request, or the user typed it in their latest message. A link \
earlier in the conversation belongs to an earlier trip, even when the \
destination sounds similar, so never repeat, adapt or reuse one for a new \
request. If you have not called the tool, or it has not returned a link, \
you do not have a link: say so in one sentence instead of giving one, and \
do not describe a trip as created, planned or ready unless the tool's \
result says DONE.

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
generate_trip_plan_async, which starts a brand new trip rather than \
finalizing this one. If it returns a link, give the user that exact link; \
if it says they are not allowed to lock this poll, say so in one sentence \
and mention the organizer link that was sent to LINE. If the user declines, \
treat it like any other declined confirmation above: acknowledge briefly \
and stop, without generating anything.

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

# Appended to SYSTEM_PROMPT when a question comes from the web chat rather than
# a microphone (request_context says which - see LLMBrain._call_provider).
# SYSTEM_PROMPT is written for speakers, and one of its rules would defeat a
# chat whose answer is often a link: never speak URLs. The rest of it (plain
# text, short) fits a chat as well as a call, so only that rule is lifted.
WEB_CHAT_ADDENDUM = """

This conversation is a text chat in the Trip Planner web app, not a voice \
call: the user reads your reply on a screen instead of hearing it. That \
changes one rule above. A link that a tool returns is the whole point of \
the answer, so write it into your reply exactly as the tool gave it - on \
its own line after a short sentence is best - and never shorten, rewrite, \
or invent a link, or reuse one from earlier in the conversation. \
A tool may also put a card (a widget) on the person's screen with its \
result; when its result says so, the person can already see it, so do not \
read it out or repeat what is in it - one short sentence is enough. \
Everything else stands: plain text with no markdown, and short."""

# Appended after WEB_CHAT_ADDENDUM when the person has voice replies turned on
# (request_context.voice_reply). The page reads a reply aloud only when its text is
# at most rich_reply.VOICE_MAX_CHARS characters, so this asks for conversational
# answers inside that size - and says plainly that a longer, needed answer is fine,
# so the limit never costs an answer its substance.
VOICE_REPLY_ADDENDUM = """

The person has voice replies turned on: a reply of 200 characters or fewer \
is also read aloud to them. So keep conversational answers - small talk, a \
confirmation, a quick fact, a yes or no - to one or two short sentences \
within 200 characters, in plain words that sound natural spoken. That limit \
is for conversation only: when the task needs more (search results, a plan, \
what is in a file) write what it needs - it is shown on screen and not read \
aloud."""

# ---------------------------------------------------------------------
# Where and when: told to the model on every turn
# ---------------------------------------------------------------------
# A model knows neither today's date nor where its user is, so "what's the weather?"
# got "where are you?" or "I have no weather tool" instead of an answer. Every call
# now carries one short block that says both (turn_context), and - when a web search
# tool is connected - the rule to search for anything real-time (REALTIME_RULE).
#
# That block goes AFTER the persona as a separate system block (see the providers'
# `context`), never into it: the persona carries the prompt-cache breakpoint, and a
# clock inside it would make every turn a cache miss.
#
# JARVIS_LOCATION and JARVIS_TIMEZONE (an IANA name) change the defaults.
DEFAULT_LOCATION = "Bangkok, Thailand"
DEFAULT_TIMEZONE = "Asia/Bangkok"
# Thailand has had no daylight saving since 1920, so a fixed offset is exact. It is the
# fallback when the IANA database is missing - Windows has none unless the `tzdata`
# package is installed, and slim Docker images may lack it too.
_FIXED_ZONES = {"Asia/Bangkok": timezone(timedelta(hours=7), "ICT")}

REALTIME_RULE = (
    "Real-time information: when the user asks about anything that changes or that you "
    "cannot know from training - the weather or a forecast, news and current events, prices "
    "and exchange rates, opening hours, places nearby, sports results, traffic - you MUST "
    "call {search} straight away, adding the location above to the query whenever the "
    "answer depends on place. Never say you lack a weather, news or maps tool, and never "
    "tell the user to check for themselves: search, then answer from what you found.{read} "
    "Answer from the first useful results - two searches at most - instead of searching again "
    "for a perfect one: every extra search makes the answer slower and dearer. "
    "If the search fails or finds nothing useful, say so in one sentence instead of guessing."
)


def _zone(name: str) -> tzinfo:
    """The IANA zone `name`, its fixed-offset stand-in, or this machine's own zone."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - ZoneInfoNotFoundError, a bad name, no database
        fixed = _FIXED_ZONES.get(name)
        if fixed is not None:
            return fixed
        local = datetime.now().astimezone().tzinfo
        return local if local is not None else timezone.utc


def turn_context(now: Optional[datetime] = None) -> str:
    """One sentence pair: what time it is where the user is, and where that is."""
    location = os.environ.get("JARVIS_LOCATION", "").strip() or DEFAULT_LOCATION
    zone_name = os.environ.get("JARVIS_TIMEZONE", "").strip() or DEFAULT_TIMEZONE
    moment = (now or datetime.now(timezone.utc)).astimezone(_zone(zone_name))
    offset = moment.strftime("%z") or "+0000"
    stamp = (
        f"{moment:%A} {moment.day} {moment:%B %Y}, {moment:%H:%M} "
        f"({zone_name}, UTC{offset[:3]}:{offset[3:]})"
    )
    return (
        f"Right now it is {stamp}. The user is in {location} unless they say otherwise: "
        "use that for anything that depends on place - the weather, local news, opening "
        "hours, places nearby, travel times - and do not ask where they are."
    )


def realtime_rule(tools: list[ToolSpec]) -> str:
    """
    REALTIME_RULE naming this turn's web search tool (and page reader), or "" when
    there is none. With the agent team on (agents/), the manager has no search tool
    of its own: the rule then names delegate_to_research, which searches and reads
    pages for it.
    """
    search = next((t.name for t in tools if t.name == "web_search" or t.name.endswith("__web_search")), None)
    if search is None:
        if any(t.name == DELEGATE_RESEARCH for t in tools):
            return REALTIME_RULE.format(
                search=f"the {DELEGATE_RESEARCH} tool (your Head of Research searches the web and reads pages for you)",
                read="",
            )
        return ""
    reader = next((t.name for t in tools if t.name.endswith("__fetch_url")), None)
    read = f" If a result's snippet is not enough, read that page with {reader}." if reader else ""
    return REALTIME_RULE.format(search=f"the {search} tool", read=read)


# The specialist agents' delegation tools (agents/; LLMBrain._team_tools builds them).
DELEGATE_RESEARCH = "delegate_to_research"

# Added to the manager's per-turn context while it has a team (LLMBrain._call_provider).
TEAM_RULE = (
    "Your team: {members}. You cannot read files or search the web yourself - those tools "
    "belong to them. Hand a task to the right one instead of guessing or saying you cannot. "
    "They do not see this conversation, so each task must stand on its own: the question, "
    "every file path or link, the place and dates, and what you need back. Their reports "
    "are for you: answer the user yourself, in your own words."
)

# What the brain does when a reply holds a trip link or id that no tool returned
# during this request (link_guard.py says which): it is not sent. The model gets
# one chance to fix it - the likeliest cause is a plain request to plan a trip
# answered from memory instead of by the tool - and if the second reply is no
# better the user gets the honest sentence below. Neither is ever stored in the
# conversation history.
LINK_CORRECTION = (
    "[Not sent] Your last reply held a trip link or trip id that no tool returned "
    "during this request: {references}. A link from earlier in the conversation "
    "belongs to an earlier trip, so it must not be repeated for a new request. If "
    "the user wants a trip planned or created and has told you the destination, the "
    "number of days and the number of people, call generate_trip_plan_async now and "
    "use only the link it returns; if any of those is missing, ask for it instead of "
    "guessing. If no tool can give you a link, answer without any link and say "
    "plainly that you do not have one."
)
LINK_FALLBACK = (
    "I can't give you a trip link for that one: I only share a link that I created "
    "or looked up during this same request, and I don't have one right now. Please "
    "ask again, or open the trip from the earlier message in this chat."
)


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
    "trip", "trips", "itinerary", "poll",
    # The bundled MCP servers: long-term memory, reading a URL, crunching a data file.
    # "forget" costs "never mind, forget it" its cheap path; a "forget my address"
    # answered by a tool-less model ("Done!") while nothing is deleted costs more.
    "remember", "recall", "forget", "fetch", "url", "website", "webpage",
    "csv", "spreadsheet", "dataset", "sql",
    # Real-time questions: the web search tool answers them (REALTIME_RULE), the
    # tool-less front door cannot.
    "weather", "forecast", "news", "headlines",
)

# The same for Thai, as substrings: weather / news / forecast. ("อากาศ" is also in
# "air conditioner"; a tool-capable model answering that costs nothing.)
_REALTIME_SUBSTRINGS: tuple[str, ...] = ("อากาศ", "ข่าว", "พยากรณ์")

# Thai has no spaces between words, so these are matched as substrings, like
# _TRIP_SUBSTRINGS below: remember / help me remember / forget / do you remember.
_MEMORY_SUBSTRINGS: tuple[str, ...] = ("จำไว้", "ช่วยจำ", "ลืม", "จำได้")

# A request about a trip - "plan a trip to Chiang Mai" - is a tool call to make
# (generate_trip_plan_async), not a design question to reason about. But "plan"
# is one of _DEEP_MARKERS, which would send it to the reasoning tier: slower,
# dearer, and given to asking clarifying questions instead of calling the tool.
# So a trip request is recognised first and stays on the standard tier, and
# _looks_like_tool_command keeps it away from the front door, which has no
# tools and could only answer it conversationally.
#
# The English words are matched as whole words, like every list above. Thai has
# no spaces between words, so the Thai ones are substrings of the raw text.
_TRIP_WORDS: tuple[str, ...] = ("trip", "trips", "itinerary", "itineraries", "poll")
_TRIP_SUBSTRINGS: tuple[str, ...] = ("ทริป", "เที่ยว", "แผนเดินทาง")


def _mentions_trip(text: str) -> bool:
    """True when the utterance is about a trip: planning one, its poll, its plan."""
    lowered = " " + re.sub(r"[^\w\s]", " ", text.lower()).strip() + " "
    if any(f" {word} " in lowered for word in _TRIP_WORDS):
        return True
    raw = text.lower()
    return any(fragment in raw for fragment in _TRIP_SUBSTRINGS)


# "How is the system doing?" is a tool call (get_system_status), not conversation: the front
# door has no tools and would answer it from nothing. Whole words, like the lists above.
_STATUS_WORDS: tuple[str, ...] = ("cpu", "ram", "memory", "uptime", "telemetry")
_STATUS_PHRASES: tuple[str, ...] = (
    "system status", "systems status", "status of the system", "status of the systems",
    "system health", "how are the systems", "how is the system", "how are you running",
)


def _asks_system_status(text: str) -> bool:
    """True when the utterance asks how the machine or the systems are doing."""
    lowered = " " + re.sub(r"[^\w\s]", " ", text.lower()).strip() + " "
    if any(f" {word} " in lowered for word in _STATUS_WORDS):
        return True
    return any(phrase in lowered for phrase in _STATUS_PHRASES)


# A note the web API adds after the person's words - "[System Context: The user attached a
# file ...]", attachments.py - is for the model, not part of the question. Routing reads the
# person's own words only: the note alone is some 25 words, which would send every question
# about an attached file to the reasoning tier, and a file's name ("architecture review.pdf")
# must not pick the model either. The front door still sees the whole text.
_SYSTEM_CONTEXT_NOTE = re.compile(r"\[System Context:[^\]]*\]")


def _routing_text(text: str) -> str:
    """`text` without the API's [System Context: ...] notes: what _classify_intent should read."""
    return _SYSTEM_CONTEXT_NOTE.sub(" ", text).strip()


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

    # Before the deep markers: "plan a trip to Chiang Mai" contains "plan".
    if _mentions_trip(text):
        return INTENT_STANDARD, "a trip request"

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


# =====================================================================
# Front door (Feature 6)
# =====================================================================
#
# One tool-less call to the cheap tier, ahead of the supervisor loop,
# that either answers the question itself or says "not mine". It covers
# what _classify_intent cannot: telling a stray bit of general
# conversation ("tell me a joke") from a request that only sounds like
# one, without a keyword list that has to know every phrasing.
#
# It costs a round trip, which is why it is not consulted for everything.
# LLMBrain._front_door_skip_reason keeps it off the questions where it
# could only lose. What is left either gets its whole reply from this one
# small call (cheaper than the supervisor turn it replaces) or pays one
# small extra call before the supervisor takes over.

_FRONT_DOOR_INTENTS = ("CHAT", "TRIP_PLANNING")

# Added to the router's prompt when the assistant behind it can search the web: without
# it the cheap model answered "what's the weather?" itself - "I don't have a weather tool".
_FRONT_DOOR_REALTIME = (
    "\n- Real-time information - the weather, news, prices, opening hours, places nearby, "
    "anything that changes - is found by a web search the assistant behind you can do: "
    "route_to \"ORCHESTRATOR\" with response null. Never answer it from memory, and never "
    "say that no tool can do it."
)

# A web address in the utterance: "what does https://... say", "summarise www.example.com".
_HAS_LINK = re.compile(r"\bhttps?://|\bwww\.[a-z0-9-]", re.IGNORECASE)
_FRONT_DOOR_ROUTES = ("SELF", "ORCHESTRATOR")


@dataclass
class FrontDoorDecision:
    """The router's verdict, as parsed from its JSON."""

    intent_type: str
    route_to: str
    response: Optional[str] = None

    @property
    def spoken(self) -> Optional[str]:
        """
        The sentence to speak if the front door is answering, else None.

        Both fields have to agree. A "SELF" verdict on a trip request
        would speak an answer from a model with no trip tools, which is
        the silent failure the thinking-mode comment above warns about -
        so anything other than CHAT/SELF with a real reply is a hand-off.
        """
        if self.route_to == "SELF" and self.intent_type == "CHAT":
            return self.response
        return None


def _parse_front_door(raw: str) -> Optional[FrontDoorDecision]:
    """
    Reads the router's JSON, or None if it did not produce a decision.

    Lenient about the packaging - a model told "JSON only" still
    sometimes wraps the object in a code fence or a sentence - and strict
    about the content: a label outside the two lists is not a verdict.
    The caller treats None as "hand it to the supervisor".
    """
    text = (raw or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    intent_type = str(data.get("intent_type") or "").strip().upper()
    route_to = str(data.get("route_to") or "").strip().upper()
    if intent_type not in _FRONT_DOOR_INTENTS or route_to not in _FRONT_DOOR_ROUTES:
        return None

    response = data.get("response")
    if isinstance(response, str):
        response = response.strip()
        # The literal word "null" is what a model writes when it copies
        # the format string instead of using JSON null.
        if response.lower() in ("", "null"):
            response = None
    else:
        response = None

    return FrontDoorDecision(intent_type, route_to, response)


def _looks_like_tool_command(text: str) -> bool:
    """
    True when an utterance reads as "do something" rather than "say
    something".

    The same two signals _classify_intent uses to keep these off the
    cheap tier - a transactional opener, or a word that means the user
    wants something run - because the front door has no tools and must
    not be the one to answer them.
    """
    lowered = " " + re.sub(r"[^\w\s]", " ", text.lower()).strip() + " "
    if any(lowered.lstrip().startswith(opener) for opener in _ALWAYS_FAST):
        return True
    if any(word in _TOOL_WORDS for word in lowered.split()):
        return True
    if any(fragment in text for fragment in _MEMORY_SUBSTRINGS + _REALTIME_SUBSTRINGS):
        return True
    # The Thai trip words are not separate words, so _TOOL_WORDS cannot hold them.
    return _mentions_trip(text) or _asks_system_status(text)


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
    head_count: int | None = None,
    days: int | None = None,
) -> str:
    """
    Plans a trip with the standalone Trip Planner app.

    Same shape as the other implementation-module tools: the work lives
    in plugins/trip_planner.py and is imported lazily, so a missing or
    broken module degrades to one spoken sentence rather than stopping
    the brain from starting.

    Unlike the tools around it, this one is registered background=False
    (see the ToolSpec below) even though the real planning work takes
    many seconds. For the voice assistant, plugins.trip_planner already
    backgrounds itself with its own thread and returns a tailored
    acknowledgement immediately, so routing it through task_dispatcher
    as well would background an already-backgrounded call and replace
    that acknowledgement with the dispatcher's generic "STARTED IN THE
    BACKGROUND" text. For the web chat it does the opposite on purpose:
    it WAITS and returns the link to the finished plan, because a chat
    reply that says "I'll message you on LINE" is not an answer.

    starting_location is optional (the model is no longer told to ask for
    it) and forwarded as-is, touched nowhere else in this function - see
    the privacy constraint documented at the top of plugins/trip_planner.py
    before changing that.
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


def _tool_get_system_status() -> str:
    """
    Shows the user how the machine is doing: CPU, memory, uptime (MOCK numbers - see
    system_status.py). On the web chat it asks the page to draw a System status
    widget and tells the model not to read it out; by voice, where there is no
    screen, it returns the numbers as a sentence to speak.
    """
    snapshot = system_status.collect()
    try:
        shown = request_context.add_widget(
            rich_reply.widget("system_status", system_status.widget_data(snapshot), sample=True)
        )
    except (TypeError, ValueError) as exc:  # a bug in widget_data, not the user's problem
        print(f"[Brain] get_system_status could not build its widget: {exc}")
        shown = False
    return system_status.web_result(snapshot) if shown else system_status.spoken_summary(snapshot)


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
    Locks a consensus poll and generates its final plan from the votes, by
    asking the Trip Planner app (POST /api/poll/<id>/finalize).

    Same shape as the other implementation-module tools: the work lives
    in plugins/trip_planner.py and is imported lazily, so a missing or
    broken module degrades to one spoken sentence rather than stopping
    the brain from starting.

    Registered background=False (see the ToolSpec below) for the same
    reason generate_trip_plan_async is. For the voice assistant,
    plugins.trip_planner already backgrounds itself with its own thread
    and returns a tailored acknowledgement immediately, so routing it
    through task_dispatcher as well would background an already-backgrounded
    call and replace that acknowledgement with the dispatcher's generic
    "started" text. For the web chat it WAITS and returns the link to the
    locked plan - the answer itself - or the app's reason for refusing.
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

# The MCP tool servers (mcp_client.py). A module global, like the vision
# provider, because list_capabilities is a plain function with no brain to ask.
_MCP: Optional[mcp_client.McpManager] = None


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
    if _MCP is not None and _MCP.configured:
        parts.append(_MCP.status())
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
            "Use this to plan a trip, but ONLY once the user has told you "
            "all three of: the destination, how many days (or dates you "
            "can count into days) and how many people are going. Details "
            "given earlier in the conversation count. NEVER assume or "
            "default a missing one - not 3 days, not 1 person. If any of "
            "the three is missing, do NOT call this tool: ask the user one "
            "short question for what is missing, wait for the answer, then "
            "call it. Do not ask where they are travelling from or about "
            "their budget.\n"
            "planning_type decides what happens:\n"
            "- 'solo_blank' (the default): plans the whole trip for them "
            "right now, personalized from their saved taste profile, and "
            "returns the link to the finished plan. Use it for 'plan a trip "
            "to Chiang Mai' and for any request that mentions neither a "
            "group nor places they already have in mind.\n"
            "- 'consensus': ONLY when the user says the trip is with a "
            "group (friends, family, colleagues) who should vote on dates "
            "and vibes first. Creates a voting poll and returns its link.\n"
            "- 'solo_draft': ONLY when the user says they already have "
            "specific places in mind and want to add them themselves. "
            "Creates a draft board and returns its link.\n"
            "When the tool returns a link, give the user that exact link. "
            "If it returns an error, say what went wrong and do not claim "
            "a plan exists."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "destination": {
                    "type": "string",
                    "description": (
                        "Where the trip is to, e.g. 'Chiang Mai' or "
                        "'Kyoto, Japan'. Just the place, as the user said it."
                    ),
                },
                "planning_type": {
                    "type": "string",
                    "enum": ["solo_blank", "solo_draft", "consensus"],
                    "description": (
                        "'solo_blank' (default) plans the whole trip now. "
                        "'consensus' for a group trip that needs a poll "
                        "first. 'solo_draft' for a solo trip where the user "
                        "already has specific places in mind they want to "
                        "add themselves."
                    ),
                },
                "head_count": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "How many people are going, including the user, "
                        "exactly as the user said. Never a guess: if they "
                        "have not said, ask them instead of calling."
                    ),
                },
                "days": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 14,
                    "description": (
                        "How many days the trip is for, 1 to 14, exactly as "
                        "the user said (count a date range into days). Never "
                        "a guess: if they have not said, ask them instead of "
                        "calling."
                    ),
                },
            },
            "required": ["destination", "days", "head_count"],
        },
        handler=_tool_generate_trip_plan_async,
        requires_confirmation=False,
        # Deliberately NOT backgrounded through task_dispatcher. For the
        # voice assistant the handler backgrounds itself (its own thread,
        # see plugins/trip_planner.py) and returns its own tailored
        # acknowledgement in well under a second - exactly the string this
        # tool needs the model to speak, which task_dispatcher's generic
        # "started" text would replace. For the web chat the handler WAITS
        # (~15-30 s) and returns the link to the finished plan, which is
        # the answer itself; backgrounding it would reply "I'm on it" and
        # lose the link.
        background=False,
        label="trip plan to {destination}",
    ),
    ToolSpec(
        name="get_system_status",
        description=(
            "Use this when the user asks how the system, the machine or "
            "Jarvis itself is doing: system status, health, CPU, memory, "
            "RAM or uptime. It takes no arguments. In the web chat it shows "
            "the user a System status card, so do not read the numbers out; "
            "by voice it returns them for you to say. The numbers are "
            "SAMPLE data, not live readings - say so."
        ),
        input_schema={"type": "object", "properties": {}},
        handler=_tool_get_system_status,
        requires_confirmation=False,
        # Foreground: the result IS the answer, and the handler reads the
        # request context (web or voice), which a background thread would not have.
        background=False,
        label="system status",
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
            "Use this to actually lock a poll and generate its final trip "
            "plan AFTER the user has confirmed they want to finalize it. "
            "It builds the plan from the votes (and the voters' saved taste "
            "profiles) in the Trip Planner app and returns the link to the "
            "finished plan; give the user that exact link. From the web chat "
            "only the person who created the poll in the chat can lock it - "
            "if the tool reports it is not allowed, say so and mention the "
            "organizer link that was sent to LINE. By voice the link is sent "
            "to LINE. Do NOT call this on its own for a finalization "
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
        # prompt here would ask the same question twice. For the voice
        # assistant the handler backgrounds itself (its own thread, see
        # plugins/trip_planner.py) and returns its own tailored
        # acknowledgement in well under a second - exactly the string this
        # tool needs the model to speak, which task_dispatcher's generic
        # "started" text would replace. For the web chat the handler WAITS
        # (~15-30 s) and returns the plan's link, which is the answer.
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
    # Gemini 3 models sign each function call ("thought signature") and refuse the
    # next request - 400 - unless the call goes back with its signature. Opaque
    # bytes, carried for Gemini only; None for calls from any other provider.
    signature: Optional[bytes] = None


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
        True when retrying this provider cannot help: a rejected key or
        model, no SDK, or an account out of credit.
        """
        return self.kind in ("auth", "permission", "not_found", "no_key", "no_sdk", "billing")

    @property
    def standby_can_help(self) -> bool:
        """
        True when the other provider may answer what this one could not:
        every fatal kind, plus the limits on this provider's side that pass
        by themselves (a rate limit, an overload, a 5xx). Not a bad request
        or a lost network - the standby would fail those the same way.
        """
        return self.is_fatal_for_provider or self.kind in ("rate_limit", "server")


def _out_of_credit(exc: Exception) -> bool:
    """
    True for an Anthropic error that means the account has no credit left.
    Anthropic sends that as a 400 invalid_request_error ("Your credit balance
    is too low ...", seen 2026-10-26), or as 402 / billing_error. Matched on
    those exact signals, not the word "billing": a tool schema with a
    `billing_cycle` field can appear in an ordinary 400's message.
    """
    body = getattr(exc, "body", None)
    error = body.get("error") if isinstance(body, dict) else None
    kind = error.get("type") if isinstance(error, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    return (
        getattr(exc, "status_code", None) == 402
        or kind == "billing_error"
        or "credit balance" in str(message or exc).lower()
    )


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
        context: str = "",
        json_output: bool = False,
    ) -> Turn:
        """
        One model call. `system` is the persona - the same text on every call, so a
        provider can cache it; `context` is what changes from turn to turn (the
        date, the place, rules that depend on this turn's tools - see turn_context),
        sent after it. `json_output` asks for one JSON object where the API has a
        mode for it (a specialist agent's structured answer, agents/); a provider
        without one ignores it and the prompt has to say so.
        """
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
        context: str = "",
        json_output: bool = False,  # no JSON mode here; only the agents ask, and none runs on Claude
    ) -> Turn:
        if self._client is None:
            raise ProviderError("no_key", "My Anthropic model isn't configured.")

        # `model`/`effort` come from the intent router, per question.
        # Nothing is stored on self: the next question re-decides.
        target = model or self._model
        request: dict[str, Any] = {
            "model": target,
            "max_tokens": self._max_tokens,
            # A cache_control breakpoint on the system prompt, not a
            # plain string - SYSTEM_PROMPT is identical on every call
            # this provider makes, so Anthropic can serve it from cache
            # (a fraction of the input-token cost) instead of the model
            # re-reading it from scratch each turn. Below Anthropic's
            # ~1024-token minimum this is simply a no-op, not an error,
            # so it costs nothing to always set.
            "system": [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ]
            # The per-turn block (date, place, ...) goes after the breakpoint, so the
            # cached prefix - tools and persona - stays byte-identical turn after turn.
            + ([{"type": "text", "text": context}] if context else []),
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
            schemas = anthropic_tool_schemas(tools)
            # Same reasoning as the system-prompt breakpoint above: the
            # tool list is rebuilt from the same static TOOLS registry
            # every call, so mark the LAST schema cacheable - Anthropic
            # caches everything up to and including a marked block, so
            # this one breakpoint covers the whole tools array.
            schemas[-1] = {**schemas[-1], "cache_control": {"type": "ephemeral"}}
            request["tools"] = schemas

        try:
            response = self._client.messages.create(**request)
        except Exception as exc:  # noqa: BLE001 - classified immediately below
            raise self._classify(exc) from exc

        usage = getattr(response, "usage", None)
        if usage is not None:
            llm_budget.record_usage(
                getattr(usage, "input_tokens", 0) or 0,
                getattr(usage, "output_tokens", 0) or 0,
            )

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
        if _out_of_credit(exc):
            # Before BadRequestError: Anthropic reports an empty balance as a 400.
            return ProviderError("billing", "My Anthropic account is out of credit.")
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
        fallthrough: bool = True,
    ) -> None:
        self._model = model or _env("GEMINI_MODEL", GEMINI_MODEL)
        self._max_tokens = max_tokens
        # True (Jarvis's standby): a busy, retired or quota-less model falls through to
        # the default and then GEMINI_MODEL_LAST_RESORT within one call. False (a
        # specialist agent's route, agents/base.py): exactly the model asked for, and a
        # failure is raised so the agent's own fallback - another provider - is used.
        self._fallthrough = fallthrough
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
            # The SDK's own retry backs off for minutes on a busy model (a single
            # "hi" took 218 s on 2026-10-26). A standby has to answer now: no
            # SDK retry, a bounded wait, and the next model is the retry (complete()).
            self._client = genai.Client(
                api_key=key,
                http_options=genai_types.HttpOptions(
                    timeout=GEMINI_TIMEOUT_MS,
                    retry_options=genai_types.HttpRetryOptions(attempts=1),
                ),
            )
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
                        ),
                        # Gemini 3 wants each call back with its signature. A call
                        # it did not make (Claude's, before a failover in the same
                        # question) gets the placeholder Google documents for that.
                        thought_signature=call.signature or GEMINI_FOREIGN_CALL_SIGNATURE,
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
        context: str = "",
        json_output: bool = False,
    ) -> Turn:
        if self._client is None:
            raise ProviderError("no_key", "My Gemini model isn't configured.")

        config_kwargs: dict[str, Any] = {
            "system_instruction": f"{system}\n\n{context}" if context else system,
            "max_output_tokens": self._max_tokens,
        }
        if json_output and not tools:
            config_kwargs["response_mime_type"] = "application/json"
        if tools:
            config_kwargs["tools"] = [gemini_tool_declarations(tools)]
            # The SDK will otherwise try to invoke Python callables itself.
            # This module runs tools by hand so the confirmation gate and
            # the console logging stay in one place.
            config_kwargs["automatic_function_calling"] = (
                genai_types.AutomaticFunctionCallingConfig(disable=True)
            )

        # The asked-for model first; if it is out of quota (a free key has no Pro),
        # withdrawn or overloaded (503 "high demand", 504), the default, then the
        # last resort - rather than no answer at all.
        response = None
        targets = list(dict.fromkeys([model or self._model, self._model, GEMINI_MODEL_LAST_RESORT]))
        if not self._fallthrough:
            targets = targets[:1]
        for target in targets:
            try:
                response = self._client.models.generate_content(
                    model=target,
                    contents=self._to_contents(transcript),
                    config=genai_types.GenerateContentConfig(**config_kwargs),
                )
                break
            except Exception as exc:  # noqa: BLE001 - classified immediately below
                error = self._classify(exc)
                if target == targets[-1] or error.kind not in ("rate_limit", "not_found", "server"):
                    raise error from exc
                following = targets[targets.index(target) + 1]
                print(f"[Brain] Gemini {target} unavailable ({error.kind}); trying {following}.")
        assert response is not None

        usage = getattr(response, "usage_metadata", None)
        if usage is not None:
            llm_budget.record_usage(
                getattr(usage, "prompt_token_count", 0) or 0,
                getattr(usage, "candidates_token_count", 0) or 0,
            )

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
                            signature=getattr(part, "thought_signature", None),
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

        # Feature 6: see _front_door. On unless JARVIS_FRONT_DOOR is set
        # to 0, which sends every question straight to the supervisor.
        self._front_door_enabled = _env("JARVIS_FRONT_DOOR", "1").lower() not in (
            "0", "false", "no", "off",
        )

        self._history: list[Turn] = []
        self._unavailable_reason = ""

        # Feature 3: which column of ROUTING_TABLE this brain reads.
        # Set once the provider is chosen and again on failover, because
        # sending "claude-opus-5" to Gemini is a 404.
        self._route_key = "anthropic"
        # When a failover that should pass (credit, rate limit - FAILBACK_SECONDS)
        # gives Anthropic another try: a time.monotonic() value, or None.
        self._failback_at: Optional[float] = None
        # Why the last failover happened (ProviderError.kind), for manager_status; "" when on the primary.
        self._failover_kind = ""

        # Feature 5: plugins are loaded now and re-scanned every turn.
        self._plugin_tools: list[ToolSpec] = []

        # Tools from external MCP servers (mcp_client.py): connected once now,
        # then re-read every turn like plugins so a server that restarts or
        # changes its tool list is picked up without restarting Jarvis.
        self._mcp_tools: list[ToolSpec] = []
        self._mcp = self._start_mcp()

        # The specialist agents (agents/): each owns some of the MCP servers' tools,
        # and the manager reaches them through a delegate_to_<agent> tool instead.
        # Empty when JARVIS_AGENTS=0 - one model with every tool, as before.
        self._team: dict[str, Any] = self._start_team()
        self._delegation_specs: dict[str, ToolSpec] = {}

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
        state = "on" if self._front_door_enabled else "off (JARVIS_FRONT_DOOR=0)"
        print(f"[Brain] Front door: {state}.")
        standby = self._secondary if self._provider is self._primary else self._primary
        if standby.available:
            print(f"[Brain] Standby provider: {standby.name} ({standby.model}).")
        else:
            print(f"[Brain] No standby provider ({standby.unavailable_reason}).")
        for agent in self._team.values():
            if agent.can_work(self._mcp_tools):
                print(f"[Brain] Team: {agent.tool_name} -> {agent.describe_routes()}.")
            else:
                why = "no model configured" if not agent.available else "its tool servers are not connected"
                print(f"[Brain] Team: {agent.name} agent off ({why}); its tools stay with the manager.")

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

    def mcp_servers(self) -> list[dict[str, Any]]:
        """The MCP tool servers and whether each is connected (McpManager.servers); [] when none are configured."""
        return self._mcp.servers() if self._mcp is not None else []

    @property
    def all_tool_names(self) -> list[str]:
        """The manager's tools and every working agent's, once each (for /health)."""
        names = self.tool_names
        for agent in self._working_agents():
            names += [tool.name for tool in agent.tools_from(self._mcp_tools)]
        return list(dict.fromkeys(names))

    def manager_status(self) -> dict[str, Any]:
        """
        The manager's own route, for /health: the provider and model answering now,
        whether that is the standby, and how the primary (Anthropic) is - "ready",
        "resting" (out of credit or rate limited; tried again at back_in seconds),
        "failed" (a rejected key or model: until restart) or "not configured".
        last_error is why it failed over (ProviderError.kind) while on the standby.
        """
        active = self._provider
        primary = self._primary
        on_standby = active is not None and active is not primary
        now = time.monotonic()
        if not primary.available:
            state = "not configured"
        elif not on_standby:
            state = "ready"
        elif self._failback_at is not None:
            state = "resting" if now < self._failback_at else "ready"  # tried again on the next question
        else:
            state = "failed"
        back_in = max(0, round(self._failback_at - now)) if on_standby and self._failback_at is not None else None
        return {
            "provider": active.name if active else "none",
            "model": active.model if active else "none",
            "on_standby": on_standby,
            "primary": {
                "provider": primary.name,
                "model": primary.model,
                "state": state,
                **({"last_error": self._failover_kind} if on_standby and self._failover_kind else {}),
                **({"back_in": back_in} if back_in is not None else {}),
            },
        }

    def team_status(self) -> list[dict[str, Any]]:
        """Each specialist agent: whether it is working, its routes (provider, model, state) and its tools."""
        return [agent.status(self._mcp_tools) for agent in self._team.values()]

    def _active_tools(self) -> list[ToolSpec]:
        """
        What the manager can call: the built-in tools, whatever is in plugins/, the MCP
        tools no working agent owns, and one delegate_to_<agent> tool per working agent.
        An agent that cannot work (no model, or its tool servers are down) is left out
        and its tools stay here, so a missing key never takes a capability away.
        """
        working = self._working_agents()
        mcp = [tool for tool in self._mcp_tools if not any(agent.owns(tool) for agent in working)]
        return list(self._tools) + list(self._plugin_tools) + mcp + [self._delegation_tool(a) for a in working]

    # -- the team (agents/) ----------------------------------------------

    def _start_team(self) -> dict[str, Any]:
        """The specialist agents, or {} when JARVIS_AGENTS=0 or they cannot be built. Never raises."""
        if _env("JARVIS_AGENTS", "1").lower() in ("0", "false", "no", "off"):
            print("[Brain] Agent team off (JARVIS_AGENTS=0): one model with every tool.")
            return {}
        try:
            # Imported here, not at the top: the agents import this module.
            import agents

            return agents.build_team()
        except Exception as exc:  # noqa: BLE001 - the team is an extra; the manager works without it
            print(f"[Brain] Agent team could not start ({type(exc).__name__}: {exc}); one model with every tool.")
            return {}

    def _working_agents(self) -> list[Any]:
        return [agent for agent in self._team.values() if agent.can_work(self._mcp_tools)]

    def _delegation_tool(self, agent: Any) -> ToolSpec:
        """The manager's delegate_to_<agent> tool: built once, the same object every turn (the tool list stays cacheable)."""
        spec = self._delegation_specs.get(agent.name)
        if spec is None:
            spec = ToolSpec(
                name=agent.tool_name,
                description=agent.delegate_description,
                input_schema={
                    "type": "object",
                    "properties": {"task": {"type": "string", "description": agent.task_description}},
                    "required": ["task"],
                },
                handler=lambda task="", _agent=agent: self._delegate(_agent, task),
                requires_confirmation=False,
                # Foreground: the report IS what the answer is made of.
                background=False,
                label=f"the {agent.title}",
                source="agent",
            )
            self._delegation_specs[agent.name] = spec
        return spec

    def _delegate(self, agent: Any, task: str = "") -> str:
        """
        Runs one task on `agent` with its own tools and returns its report for the
        manager. An agent none of whose models answers raises agents.AgentUnavailable,
        which _execute hands back as a tool error that says why, route by route.
        """
        task = str(task or "").strip()
        if not task:
            return "No task was given. Call again with the whole task in `task`."
        tools = agent.tools_from(self._mcp_tools)
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        shown = task[:160].encode(encoding, errors="backslashreplace").decode(encoding)
        print(f"[Brain] Delegating to the {agent.title}: {shown}")
        report = agent.run(task, tools, lambda call: self._execute(call, tools))
        if agent.json_output:
            header = (
                f"DRAFT FROM YOUR {agent.title.upper()} (JSON). It is NOT saved anywhere - there is no "
                "calendar or LINE integration yet. Present it to the user in words and say it is a draft:"
            )
        else:
            header = f"REPORT FROM YOUR {agent.title.upper()} (for you, not the user - answer the user yourself):"
        return f"{header}\n{report}"

    def _team_rule(self, tools: list[ToolSpec]) -> str:
        """TEAM_RULE naming the agents this call can delegate to, or "" when it can delegate to none."""
        members = [
            f"the {agent.title} ({agent.tool_name})"
            for agent in self._team.values()
            if any(tool.name == agent.tool_name for tool in tools)
        ]
        return TEAM_RULE.format(members="; ".join(members)) if members else ""

    def _start_mcp(self) -> Optional[mcp_client.McpManager]:
        """
        Connects to the servers in mcp_servers.json. Returns None when none are
        configured, and never raises: tool servers are an extra, and a broken
        one must not stop Jarvis from starting.
        """
        global _MCP
        try:
            manager = mcp_client.McpManager()
            if not manager.configured:
                return None
            manager.connect_all()
            self._mcp_tools = manager.tools()
        except Exception as exc:  # noqa: BLE001
            print(f"[Brain] MCP setup failed: {type(exc).__name__}: {exc}")
            return None
        _MCP = manager
        return manager

    def _refresh_mcp(self) -> None:
        """Takes this turn's snapshot of the MCP tools (cheap; repairs run in the background)."""
        if self._mcp is None:
            return
        try:
            self._mcp_tools = self._mcp.tools()
        except Exception as exc:  # noqa: BLE001
            print(f"[Brain] MCP refresh failed: {type(exc).__name__}: {exc}")

    def _mentions_mcp_tool(self, text: str) -> bool:
        """True when the utterance uses a word from an MCP tool's own name ("server time")."""
        if self._mcp is None:
            return False
        words = self._mcp.tool_words()
        tokens = re.sub(r"[^\w\s]", " ", text.lower()).split()
        return any(token.startswith(word) for token in tokens for word in words)

    def _front_door_prompt(self) -> str:
        """
        The router's instructions, plus what each connected tool server is for.

        In the server's own words (the first sentence of its MCP instructions),
        not a list of tool names: "recall on memory" does not tell a router that
        "what's my sister's name?" belongs to the assistant behind it, and
        "Long-term memory of the person you are talking to ..." does.
        """
        if not self._mcp_tools:
            return FRONT_DOOR_PROMPT
        notes = self._mcp.server_notes() if self._mcp is not None else []
        if not notes:
            names = ", ".join(t.label or t.name for t in self._mcp_tools[:12])
            notes = [("tools", names)]
        servers = "\n".join(f"  - {name}: {note}" for name, note in notes)
        return (
            FRONT_DOOR_PROMPT
            + "\n- External tool servers are also connected to the assistant behind you:\n"
            + servers
            + "\n  A request one of them could answer - reading a link, a question about the user's "
            + "own life, people or preferences that a memory could hold, asking to remember or forget "
            + "something, numbers in a data file - is route_to \"ORCHESTRATOR\" with response null."
            + (_FRONT_DOOR_REALTIME if any(t.name.endswith("__web_search") for t in self._mcp_tools) else "")
        )

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
        Keeps the most recent turns, within HISTORY_* characters.

        At most MAX_HISTORY_TURNS turns; each earlier message cut to
        HISTORY_MESSAGE_CHARS (the newest exchange to HISTORY_LAST_CHARS);
        then the oldest messages dropped until the whole is within
        HISTORY_CHAR_BUDGET. Both providers require the transcript to open
        on a user turn, so trimming ends on that boundary.
        """
        max_messages = self._max_history_turns * 2
        history = self._history[-max_messages:]
        newest = len(history) - 2
        history = [
            replace(turn, text=_shorten(turn.text, HISTORY_LAST_CHARS if index >= newest else HISTORY_MESSAGE_CHARS))
            for index, turn in enumerate(history)
        ]
        while len(history) > 2 and sum(len(turn.text) for turn in history) > HISTORY_CHAR_BUDGET:
            history.pop(0)
        while history and history[0].kind != "user":
            history.pop(0)
        self._history = history

    def reset(self) -> None:
        """Clears conversation memory - a fresh topic, same session."""
        self._history.clear()
        print("[Brain] Conversation history cleared.")

    def set_history(self, turns: list[tuple[str, str]]) -> None:
        """
        Replaces conversation memory with `turns`, oldest first, as
        (role, text) pairs where role is "user" or "assistant".

        For a caller that owns the transcript (the web chat keeps it in
        its database) and wants this brain stateless between requests.
        Providers need strictly alternating roles that open on a user
        turn, and a stored chat can break that (a failed reply leaves two
        user turns in a row), so same-role neighbours are merged and
        anything before the first user turn is dropped. The caller's next
        ask() adds its own user turn, so a trailing user turn is merged
        into that by being dropped here.
        """
        merged: list[Turn] = []
        for role, text in turns:
            kind = "user" if role == "user" else "assistant"
            text = (text or "").strip()
            if not text:
                continue
            if merged and merged[-1].kind == kind:
                merged[-1] = Turn(kind=kind, text=f"{merged[-1].text}\n{text}")
            elif merged or kind == "user":
                merged.append(Turn(kind=kind, text=text))
        if merged and merged[-1].kind == "user":
            merged.pop()
        self._history = merged
        self._trim_history()

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

    def _execute(self, call: ToolCall, tools: Optional[list[ToolSpec]] = None) -> ToolResult:
        """
        Runs one tool call. Never raises: a failure becomes a tool result
        the model can read and explain.

        `tools` is a specialist agent's own set (agents/): its calls are looked up
        there and nowhere else, so an agent can never reach the manager's tools
        (LINE, webhooks, delegation). The confirmation gate and the logging are the
        same for both.
        """
        if tools is not None:
            tool = next((t for t in tools if t.name == call.name), None)
        else:
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
        # A tool result can quote what the user typed (a Thai destination): a
        # console that cannot encode it must not fail the turn over a log line.
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        shown = text[:160].encode(encoding, errors="backslashreplace").decode(encoding)
        print(f"[Brain] Tool result: {call.name} -> {shown}")
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

    def _failover(self, kind: str = "") -> bool:
        """
        Switches to the standby provider. Returns False if there isn't one.

        `kind` (why the active provider failed) sets how long the switch
        lasts: an out-of-credit or rate-limited Anthropic is tried again
        after FAILBACK_SECONDS (see _maybe_fail_back); anything else holds
        until restart.
        """
        standby = self._secondary if self._provider is self._primary else self._primary
        if not standby.available:
            return False
        if standby is self._primary and self._failback_at is not None and time.monotonic() < self._failback_at:
            # Anthropic is in its timeout (no credit, rate limited) - sending this
            # back there only turns a busy Gemini into a certain failure.
            return False

        previous = self._provider.name if self._provider else "none"
        self._use(standby)
        self._failover_kind = kind if standby is self._secondary else ""
        wait = FAILBACK_SECONDS.get(kind) if standby is self._secondary else None
        self._failback_at = time.monotonic() + wait if wait else None
        later = f"; trying {previous} again in {wait // 60 or 1} min" if wait else ""
        print(f"[Brain] FAILOVER: {previous} -> {standby.name} ({standby.model}){later}.")
        return True

    def _use(self, provider: _Provider) -> None:
        """Makes `provider` the active one. Vision follows it, and so does the route table."""
        global _VISION_PROVIDER
        self._provider = provider
        _VISION_PROVIDER = provider
        self._route_key = "anthropic" if provider is self._primary else "gemini"

    def _maybe_fail_back(self) -> None:
        """
        Back to Anthropic once a temporary failover has run its time. Called
        once per question, before it is routed, so a question is never split
        between providers by the clock. If Anthropic still cannot answer,
        the next call simply fails over again.
        """
        if self._failback_at is None or time.monotonic() < self._failback_at:
            return
        self._failback_at = None
        if self._provider is not self._primary and self._primary.available:
            print(f"[Brain] Trying {self._primary.name} again after a temporary failover.")
            self._use(self._primary)
            self._failover_kind = ""

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

    # -- the front door ------------------------------------------------

    def _awaiting_answer(self) -> bool:
        """
        True when the last thing Jarvis said was a question.

        "Bangkok" and "yes" only mean something as answers to the
        parameter question or the finalize-this-poll confirmation that
        SYSTEM_PROMPT has the supervisor ask. A front door that read
        them as small talk would end that dialogue with a pleasantry
        instead of the tool call, so the supervisor keeps those turns.
        """
        if not self._history or self._history[-1].kind != "assistant":
            return False
        # Drop the note ask() appends for the model after a background
        # dispatch, or a question followed by it would not end in "?".
        last = self._history[-1].text.split(" [Already dispatched", 1)[0]
        return last.rstrip().endswith("?")

    def _front_door_skip_reason(self, user_text: str, intent: str) -> str:
        """Why this question should bypass the front door, or "" if it should not."""
        if intent == INTENT_COMPLEX:
            return "reasoning-tier question"
        if _looks_like_tool_command(user_text):
            return "tool command"
        if self._mentions_mcp_tool(user_text):
            return "mentions an MCP tool"
        if self._mcp_tools and _HAS_LINK.search(user_text):
            return "contains a link"  # the fetch tool reads it; the front door can only guess
        if self._awaiting_answer():
            return "answer to Jarvis's own question"
        return ""

    def _front_door(self, user_text: str, intent: str) -> Optional[str]:
        """
        Lets the cheap tier answer greetings and general conversation.

        Returns the sentence to speak, or None to carry on into the
        supervisor loop. None covers every way this can fail to produce
        an answer - switched off, skipped, provider trouble, unreadable
        JSON, a hand-off to the ORCHESTRATOR - because the front door is
        an optimisation and never a dependency: the supervisor can
        answer anything the front door can.
        """
        if not self._front_door_enabled:
            return None

        skip = self._front_door_skip_reason(user_text, intent)
        if skip:
            print(f"[Brain] Front door skipped ({skip}).")
            return None

        # The history rides along because the router has to see what it
        # is interrupting. The tool list does not: a router that can call
        # tools is a second supervisor.
        working: list[Turn] = list(self._history)
        working.append(Turn(kind="user", text=user_text.strip()))
        model, effort = self._route(INTENT_SIMPLE)

        try:
            turn = self._call_provider(
                working, tools=[], model=model, effort=effort, system=self._front_door_prompt()
            )
        except ProviderError as exc:
            # No speaking the error here: the supervisor is about to make
            # the same kind of call and will report it if it fails too.
            print(f"[Brain] Front door unavailable ({exc.kind}); using the supervisor.")
            return None

        decision = _parse_front_door(turn.text) if turn.kind == "assistant" else None
        if decision is None:
            print(
                f"[Brain] Front door gave no usable verdict ({turn.text[:80]!r}); "
                "using the supervisor."
            )
            return None

        reply = decision.spoken
        if reply is None:
            print(
                f"[Brain] Front door: {decision.intent_type} -> {decision.route_to}; "
                "handing to the supervisor."
            )
            return None

        # The router has no tools, so it can never have a link to give - only
        # one it remembers from earlier in the conversation (see link_guard.py).
        # Anything it says about a trip is the supervisor's to answer.
        remembered = link_guard.unverified_trip_references(reply, user_text)
        if remembered:
            print(
                f"[Brain] Front door answer held {len(remembered)} trip link/id no tool "
                "returned; handing to the supervisor."
            )
            return None

        print(f"[Brain] Front door: answered directly ({model or self._provider.model}).")
        return reply

    def ask(self, user_text: str) -> str:
        """
        Answers `user_text`, calling tools as needed, and returns one
        plain-text reply ready for the voice engine.

        Never raises: every error becomes a short spoken sentence.
        """
        if not user_text or not user_text.strip():
            return "I didn't catch a question."

        # Checked before any routing or provider work happens at all -
        # see llm_budget's module docstring: a no-op unless
        # JARVIS_DAILY_TOKEN_BUDGET is explicitly configured.
        if llm_budget.is_budget_exceeded():
            print("[Brain] Daily LLM token budget reached - not calling a provider.")
            return (
                "I've hit today's language-model budget, so I can't answer that "
                "right now. It resets tomorrow, or raise JARVIS_DAILY_TOKEN_BUDGET "
                "in the .env file."
            )

        if not self.available:
            return (
                "My language model isn't configured. Add an Anthropic or Gemini "
                "API key to the .env file and restart me. The console has the steps."
            )

        # A temporary failover (no credit, rate limited) that has run its time:
        # this question goes to Anthropic again.
        self._maybe_fail_back()

        # Feature 5: pick up any tool written during the previous turn.
        self._refresh_plugins()
        self._refresh_mcp()
        tools = self._active_tools()

        # Feature 3: route this question by intent, BEFORE the turn is
        # sent. Decided once per question and held for all of its tool
        # round trips, so a heavy request does not silently drop back to
        # a cheaper model halfway through answering itself.
        self._dispatched_this_turn = []

        intent, why = _classify_intent(_routing_text(user_text))
        model, effort = self._route(intent)
        print(
            f"[Brain] Routing: {intent.upper()} ({why}) "
            f"-> {model or self._provider.model}, effort {effort}."
        )

        # Feature 6: greetings and general conversation stop here, answered
        # by the cheap tier without the tool list. Anything else falls
        # through to the loop below on the model chosen above.
        spoken = self._front_door(user_text, intent)
        if spoken is not None:
            self._history.append(Turn(kind="user", text=user_text.strip()))
            self._history.append(Turn(kind="assistant", text=spoken))
            self._trim_history()
            return spoken

        # A working copy: only the question and the final answer are kept
        # in history. The tool round trips are scaffolding for this one
        # answer and would otherwise dominate the context window.
        working: list[Turn] = list(self._history)
        working.append(Turn(kind="user", text=user_text.strip()))

        # What this reply may quote a trip link or id from: what the user just
        # typed, plus whatever the tools return below. Never the earlier
        # conversation - that is where a stale link comes from (link_guard.py).
        evidence: list[str] = [user_text]
        corrected = False

        for step in range(1, self._max_steps + 1):
            try:
                turn = self._call_provider(working, tools, model, effort)
            except ProviderError as exc:
                return exc.spoken

            if turn.kind == "assistant":
                reply = turn.text.strip()
                if not reply:
                    return "I didn't get an answer back. Try asking again."

                # A trip link or id that no tool returned during this request
                # is a guess, however sure of itself. The model gets one
                # chance to fix it (usually by calling the tool it skipped);
                # after that the user gets the honest sentence, never the
                # link. The bad reply is not kept in the transcript either way.
                unverified = link_guard.unverified_trip_references(reply, "\n".join(evidence))
                if unverified:
                    print(
                        f"[Brain] Reply held {len(unverified)} trip link/id no tool returned "
                        f"this request: {ascii(', '.join(unverified[:3]))}"
                    )
                    if not corrected and step < self._max_steps:
                        corrected = True
                        working.append(turn)
                        working.append(
                            Turn(
                                kind="user",
                                text=LINK_CORRECTION.format(references=", ".join(unverified[:3])),
                            )
                        )
                        continue
                    reply = LINK_FALLBACK

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

                # Widgets a tool asked for during THIS request travel with the reply as one
                # JSON string (rich_reply.py) - after the link guard above, so it only ever
                # checks the sentence. The model remembers the sentence and a note, not the JSON.
                widgets = request_context.take_widgets()
                if widgets:
                    names = ", ".join(w["tool"].replace("_", " ") for w in widgets)
                    remembered += f" [A {names} widget was shown to the user.]"
                    reply = rich_reply.wrap_reply(reply, widgets)

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
            results = [self._execute(c) for c in turn.calls]
            evidence.extend(result.content for result in results)
            working.append(Turn(kind="tool_results", results=results))

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
        system: Optional[str] = None,
    ) -> Turn:
        """
        One provider round trip, with a single failover retry.

        Only errors that retrying cannot fix trigger the switch - see
        ProviderError.is_fatal_for_provider. `system` replaces the
        persona for this one call (the front door has its own prompt);
        `tools=[]` means no tools, where None means the active set.
        """
        assert self._provider is not None
        tools = self._active_tools() if tools is None else tools
        # When and where, on every call - the front door's too, which is what answers
        # "what day is it?" - plus, for the persona, the real-time rule when this
        # call can search the web.
        context = turn_context()
        if system is None:
            rule = realtime_rule(tools)
            if rule:
                context += "\n\n" + rule
            team = self._team_rule(tools)
            if team:
                context += "\n\n" + team
            prompt = self._system_prompt
            # A chat is read, not heard: lift the one voice rule that would
            # stop a tool's link from reaching the user (see WEB_CHAT_ADDENDUM).
            caller = request_context.current()
            if caller is not None and caller.is_web:
                prompt += WEB_CHAT_ADDENDUM
                if caller.voice_reply:
                    prompt += VOICE_REPLY_ADDENDUM
        else:
            prompt = system

        # The model was chosen when the question was routed (_route), maybe on the
        # other provider - a failover earlier in this same question. A name meant
        # for the other provider is dropped ("claude-opus-5" sent to Gemini is a 404),
        # which leaves this provider's default model.
        if model is not None and model.startswith("gemini") != (self._provider.name == "gemini"):
            model = None

        try:
            return self._provider.complete(prompt, working, tools, model, effort, context)
        except ProviderError as exc:
            if not exc.standby_can_help:
                raise
            print(f"[Brain] {self._provider.name} cannot answer ({exc.kind}): {exc.spoken}")
            if not self._failover(exc.kind):
                raise
            # The standby provider names its models differently, so the
            # override is dropped rather than sent somewhere it means nothing.
            return self._provider.complete(prompt, working, tools, None, effort, context)


if __name__ == "__main__":
    # Through the importable module, not this __main__ copy: the agents (agents/)
    # import llm_brain, and two copies of its classes would not recognise each other.
    from llm_brain import LLMBrain as _ImportedBrain

    brain = _ImportedBrain()
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
