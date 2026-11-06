"""
Jarvis's team: specialist agents the manager (llm_brain.LLMBrain) delegates to.

    data          Head of Data & Documents   Gemini Pro -> Groq          files, PDFs, Word, data/SQL
    research      Head of Research           Gemini Flash                web search, web pages
    productivity  Head of Productivity       GPT-4o-mini -> Gemini Flash  today's Google Calendar agenda
                                                                          -> LINE, LINE messages,
                                                                          schedules/to-dos (JSON drafts)

Each ends with Jarvis's own Gemini model as a last resort (agents/base.py, ROUTES).
The manager no longer holds the tools these agents own; it holds delegate_to_data,
delegate_to_research and delegate_to_productivity instead, and answers the user from
the agents' reports. An agent with no usable model (no key for any of its routes)
is left out, and its server tools stay with the manager, so a missing key never
removes a capability. The productivity agent's calendar and LINE tools exist only on
it; while it works the manager also loses its own send_line_notification, and while
it does not, the manager keeps that one (it asks before sending). JARVIS_AGENTS=0
turns the team off: one model with every built-in tool, as before (no calendar).

Not in the team, though they live here: the Head of Creative (creative_agent.py), which
makes images, the Head of Publishing (publishing_agent.py), which builds PowerPoint
files, and the Head of Analytics (analytics_agent.py), which builds Excel spreadsheets.
None is a tool loop - one OpenAI Images API call per picture; one model call writing the
slides, then python-pptx laying them out (a Canva-ready PPTX when the user wants Canva);
pandas and openpyxl writing the table - and all three save
what they make in artifacts/, so the manager holds delegate_to_creative,
delegate_to_publishing and delegate_to_analytics itself (LLMBrain._creative_tool,
_publishing_tool, _analytics_tool), with or without the team: images whenever
OPENAI_API_KEY is set, presentations whenever python-pptx is installed, spreadsheets
whenever pandas and openpyxl are. The Head of Database (database_agent.py) is plain
Python too - one read-only SELECT per call on DATABASE_URL through asyncpg - and the
manager holds delegate_to_database itself, for the owner only (LLMBrain._database_tool).

Keys (.env): GEMINI_API_KEY (data, research, productivity fallback, last resort),
GROQ_API_KEY (data fallback), OPENAI_API_KEY (productivity primary, images);
LINE_CHANNEL_ACCESS_TOKEN + LINE_TARGET_USER_ID (LINE), GOOGLE_SERVICE_ACCOUNT_FILE +
GOOGLE_CALENDAR_ID (the calendar).
"""

from __future__ import annotations

from .base import AgentUnavailable, Route, SpecialistAgent, last_resort_route

__all__ = ["AgentUnavailable", "Route", "SpecialistAgent", "build_team"]


def build_team() -> dict[str, SpecialistAgent]:
    """The three agents, by name. Building one makes no network call."""
    from . import data_agent, productivity_agent, research_agent

    return {
        agent.name: agent
        for agent in (
            data_agent.build(last_resort_route()),
            research_agent.build(last_resort_route()),
            productivity_agent.build(last_resort_route()),
        )
    }
