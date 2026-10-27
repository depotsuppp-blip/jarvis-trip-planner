"""
The Head of Research: answers questions from the web for the manager.

    primary      Gemini Flash          JARVIS_RESEARCH_MODEL  (default gemini-flash-latest)
    last resort  Jarvis's Gemini model (agents/base.py)

Tools, taken off the manager: the web search server (web_search) and the web page
reader (fetch_url). The spec gave this agent web search; the page reader goes with it
because a search snippet is often not the answer and "read this page" is research too.
"""

from __future__ import annotations

from llm_brain import _env

from .base import Route, SpecialistAgent, gemini_route, last_resort_route

PRIMARY_MODEL = "gemini-flash-latest"
SERVERS = ("web_search", "fetch")

SYSTEM_PROMPT = """\
You are the Head of Research on Jarvis's team. Jarvis (the manager) gives you one question that needs the internet; you answer it with your tools and report back to Jarvis - not to the user, who never sees your words.

Your tools: web_search (titles, links and snippets) and fetch_url (reads one web page).

How to work:
- Search with a short, specific query that includes the place and dates the question depends on (the date and the user's location are below). Two searches at most.
- If a snippet does not settle it, read the most promising page with fetch_url - one or two pages, not more.
- If the search tool says it failed or found nothing and not to retry, do not retry: report that.
- Never invent facts, numbers or links.

Your report: the answer in a few sentences with the key facts, numbers and dates; say when the sources are thin, old or disagree. Then "Sources:" with up to three links you actually used. Plain text, no greeting."""

DELEGATE_DESCRIPTION = (
    "Hands a question that needs the internet to your Head of Research, who has the only web "
    "search and web-page reader: news, weather and forecasts, prices and exchange rates, opening "
    "hours, sports results, anything recent or that may have changed, or what a given web page "
    "says. It does not see this conversation: put the whole question in `task`, with the place, "
    "the dates and any link it depends on. It reports back with sources; answer the user yourself "
    "from its report, with a source link when that helps."
)


def build(last_resort: Route | None = None) -> SpecialistAgent:
    primary = _env("JARVIS_RESEARCH_MODEL", PRIMARY_MODEL)
    return SpecialistAgent(
        name="research",
        title="Head of Research",
        system_prompt=SYSTEM_PROMPT,
        routes=[gemini_route(primary, "primary"), last_resort or last_resort_route()],
        servers=SERVERS,
        max_steps=5,
        delegate_description=DELEGATE_DESCRIPTION,
        task_description="The whole question, self-contained: with the place, dates and any link it depends on.",
        skills="searches the web and reads web pages",
    )
