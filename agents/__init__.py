"""
Jarvis's team: specialist agents the manager (llm_brain.LLMBrain) delegates to.

    data          Head of Data & Documents   Gemini Pro -> Groq          files, PDFs, Word, data/SQL
    research      Head of Research           Gemini Flash                web search, web pages
    productivity  Head of Productivity       GPT-4o-mini -> Gemini Flash  schedules, to-dos (JSON drafts)

Each ends with Jarvis's own Gemini model as a last resort (agents/base.py, ROUTES).
The manager no longer holds the tools these agents own; it holds delegate_to_data,
delegate_to_research and delegate_to_productivity instead, and answers the user from
the agents' reports. An agent with no usable model (no key for any of its routes)
is left out, and its tools stay with the manager, so a missing key never removes a
capability. JARVIS_AGENTS=0 turns the team off: one model with every tool, as before.

Keys (.env): GEMINI_API_KEY (data, research, productivity fallback, last resort),
GROQ_API_KEY (data fallback), OPENAI_API_KEY (productivity primary).
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
