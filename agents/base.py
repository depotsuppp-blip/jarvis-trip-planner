"""
What every specialist agent shares: its routes (which model, in which order) and
its working loop.

ROUTES. An agent has an ordered list of routes - a provider and a model - and each
model call goes to the first one that can take it:

    primary      the model the agent is built around
    fallback     a different provider, for when the primary is out of credit,
                 out of quota, rate limited or down
    last resort  Jarvis's own Gemini model (GEMINI_MODEL, default
                 gemini-flash-lite-latest), added to every agent so that a
                 missing key or an exhausted quota can never take a capability
                 away; it answers only when every model before it failed

A route whose provider has no key (or no package) is skipped without a call. A
route that fails is skipped for a while, by kind of failure (SKIP_SECONDS): out of
credit or out of quota 15 minutes, rate limited or a server error a minute, a
rejected key or an unknown model until restart. Within one task a failed route is
not tried again, so a task that started on the fallback finishes there. Every
switch is printed on the console as "[Agent:<name>] ...".

The routing is strict in the sense that matters: the order is fixed, and a
Gemini route asks for exactly its model - no quiet swap to another Gemini model
inside the call (GeminiProvider(fallthrough=False)); a failure moves on to the
next route instead, where the console shows it.

THE LOOP. An agent gets one task from the manager (LLMBrain, through its
delegate_to_<agent> tool), works on it with its own tools - and only those - for at
most `max_steps` model calls, and returns a report for the manager, never for the
user. It does not see the conversation: everything it needs is in the task.

ITS TOOLS. The tools of the MCP servers it owns (`servers`), plus `own_tools`: tools
that exist only on this agent (the productivity agent's calendar and LINE). While the
agent works, the manager has neither, nor the built-ins the agent `replaces`. A tool
that changes something outside Jarvis records what happened in `actions`, in the
tool's own words, and the manager is told that alongside the report - so "sent" in a
reply comes from the tool, not from the agent saying so.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from llm_brain import GEMINI_MODEL, GeminiProvider, ProviderError, ToolCall, ToolResult, Turn, _Provider, _env, turn_context
from tool_spec import ToolSpec

# Seconds a failed route is passed over, by ProviderError.kind. None = until restart.
SKIP_SECONDS: dict[str, Optional[float]] = {
    "billing": 15 * 60,
    "quota": 15 * 60,
    "rate_limit": 60,
    "server": 60,
    "auth": None,
    "permission": None,
    "not_found": None,
    "no_key": None,
    "no_sdk": None,
}
MAX_REPORT_CHARS = 6000
CEILING_NOTE = "Stop using tools now. Report what you have found so far, and say what is missing."


class AgentUnavailable(Exception):
    """No route could answer. The message says why, route by route, and is what the manager reads."""


@dataclass
class Route:
    provider: _Provider
    model: str
    role: str  # "primary" | "fallback" | "last resort"
    skip_until: float = 0.0  # time.monotonic(); float("inf") = until restart
    last_error: str = ""

    @property
    def label(self) -> str:
        return f"{self.provider.name}:{self.model}"

    @property
    def configured(self) -> bool:
        return self.provider.available

    def status(self, now: Optional[float] = None) -> dict[str, Any]:
        now = time.monotonic() if now is None else now
        state = "ready"
        if not self.configured:
            state = "not configured"
        elif self.skip_until > now:
            state = "resting" if self.skip_until != float("inf") else "failed"
        return {
            "role": self.role,
            "provider": self.provider.name,
            "model": self.model,
            "state": state,
            **({"why": getattr(self.provider, "unavailable_reason", "")} if not self.configured else {}),
            **({"last_error": self.last_error} if self.last_error and state != "ready" else {}),
        }


def gemini_route(model: str, role: str, provider: Optional[GeminiProvider] = None) -> Route:
    """A strict Gemini route: exactly `model`, nothing swapped in on failure."""
    return Route(provider or GeminiProvider(model=model, fallthrough=False), model, role)


_last_resort_provider: Optional[GeminiProvider] = None


def last_resort_route() -> Route:
    """Jarvis's own Gemini model, as each agent's final route (its own Route, one shared client)."""
    global _last_resort_provider
    model = _env("GEMINI_MODEL", GEMINI_MODEL)
    if _last_resort_provider is None:
        _last_resort_provider = GeminiProvider(model=model, fallthrough=False)
    return gemini_route(model, "last resort", _last_resort_provider)


def _skip_kind(exc: ProviderError) -> str:
    """A Gemini 429 that says the quota is used up lasts longer than a rate limit."""
    cause = str(exc.__cause__ or "").lower()
    if exc.kind == "rate_limit" and "quota" in cause:
        return "quota"
    return exc.kind


@dataclass
class SpecialistAgent:
    name: str  # "data", "research", "productivity": the tool is delegate_to_<name>
    title: str  # "Head of Data & Documents"
    system_prompt: str
    routes: list[Route]
    # The MCP servers whose tools belong to this agent (and are taken off the manager).
    servers: tuple[str, ...] = ()
    max_steps: int = 5
    # Ask the model for one JSON object (the provider's JSON mode where it has one);
    # `finish` then checks and tidies the final text, or raises ValueError to get one retry.
    json_output: bool = False
    finish: Optional[Callable[[str], str]] = None
    # The delegation tool the manager sees.
    delegate_description: str = ""
    task_description: str = "The whole task, self-contained: what to do and everything needed to do it."
    # Tools that exist only on this agent, not on an MCP server (see ITS TOOLS above).
    own_tools: tuple[ToolSpec, ...] = ()
    # The manager's built-in tools this agent takes over while it works: the manager loses them.
    replaces: tuple[str, ...] = ()
    # What only it can do, in a few words, for the manager's TEAM_RULE.
    skills: str = ""
    # Told to the manager after a task in which none of its tools recorded an action.
    no_actions_note: str = ""
    # What its tools changed outside Jarvis during the current task, in their own words (reset by run()).
    actions: list[str] = field(default_factory=list, repr=False, init=False)
    _failed_this_run: set[int] = field(default_factory=set, repr=False, init=False)

    # -- what the manager sees ---------------------------------------------

    @property
    def tool_name(self) -> str:
        return f"delegate_to_{self.name}"

    @property
    def available(self) -> bool:
        """True when at least one route has a key: whether it will answer is learnt by asking."""
        return any(route.configured for route in self.routes)

    def owns(self, tool: ToolSpec) -> bool:
        return tool.source.startswith("mcp:") and tool.source[4:] in self.servers

    def tools_from(self, mcp_tools: list[ToolSpec]) -> list[ToolSpec]:
        """The MCP tools this agent owns (its servers' tools)."""
        return [tool for tool in mcp_tools if self.owns(tool)]

    def toolkit(self, mcp_tools: list[ToolSpec]) -> list[ToolSpec]:
        """Everything it works with: its servers' tools and its own."""
        return self.tools_from(mcp_tools) + list(self.own_tools)

    def can_work(self, mcp_tools: list[ToolSpec]) -> bool:
        """Has a model, and - for an agent built around tool servers - at least one of their tools."""
        return self.available and (not self.servers or bool(self.tools_from(mcp_tools)))

    def status(self, mcp_tools: list[ToolSpec]) -> dict[str, Any]:
        now = time.monotonic()
        return {
            "name": self.name,
            "title": self.title,
            "tool": self.tool_name,
            "active": self.can_work(mcp_tools),
            "routes": [route.status(now) for route in self.routes],
            "tools": [tool.name for tool in self.toolkit(mcp_tools)],
        }

    def actions_note(self) -> str:
        """What its tools did during the last task, for the manager; "" for an agent whose tools change nothing."""
        if self.actions:
            return "\n".join(f"- {action}" for action in self.actions)
        return self.no_actions_note

    def describe_routes(self) -> str:
        parts = []
        for route in self.routes:
            mark = "" if route.configured else f" (off: {getattr(route.provider, 'unavailable_reason', '') or 'unavailable'})"
            parts.append(f"{route.role} {route.label}{mark}")
        return " -> ".join(parts)

    # -- the work ------------------------------------------------------------

    def run(self, task: str, tools: list[ToolSpec], execute: Callable[[ToolCall], ToolResult]) -> str:
        """Does `task` with `tools` and returns the report. Raises AgentUnavailable when no model answers."""
        self._failed_this_run = set()
        self.actions.clear()
        context = turn_context()
        transcript: list[Turn] = [Turn(kind="user", text=task.strip())]
        started = time.monotonic()

        for step in range(1, self.max_steps + 1):
            turn = self._complete(transcript, tools, context)
            if turn.kind == "assistant":
                report = self._final(turn.text, transcript, context)
                print(f"[Agent:{self.name}] Done in {step} step(s), {time.monotonic() - started:.1f} s.")
                return _cut(report)
            print(f"[Agent:{self.name}] Step {step}/{self.max_steps}: {', '.join(c.name for c in turn.calls)}")
            transcript.append(turn)
            transcript.append(Turn(kind="tool_results", results=[execute(call) for call in turn.calls]))

        print(f"[Agent:{self.name}] Hit its {self.max_steps}-step ceiling; asking for what it has.")
        transcript.append(Turn(kind="user", text=CEILING_NOTE))
        # A plain-words summary, even from an agent whose answers are JSON drafts.
        turn = self._complete(transcript, [], context, json_ok=False)
        return _cut(turn.text.strip() or "The agent ran out of steps without a result.")

    def _final(self, text: str, transcript: list[Turn], context: str) -> str:
        """The answer as the manager gets it: checked by `finish` when the agent has one, with one retry."""
        if self.finish is None:
            return text.strip() or "The agent finished without saying anything."
        try:
            return self.finish(text)
        except ValueError as problem:
            print(f"[Agent:{self.name}] Answer not usable ({problem}); asking once more.")
            retry = transcript + [
                Turn(kind="assistant", text=text or "(empty)"),
                Turn(kind="user", text=f"That could not be used: {problem}. Reply again, following your instructions exactly."),
            ]
            again = self._complete(retry, [], context)
            return self.finish(again.text)  # a second ValueError reaches the manager as a tool error

    def _complete(self, transcript: list[Turn], tools: list[ToolSpec], context: str, json_ok: bool = True) -> Turn:
        """One model call on the first route that can take it (see ROUTES at the top)."""
        now = time.monotonic()
        tried: list[str] = []
        for index, route in enumerate(self.routes):
            if not route.configured:
                continue
            if index in self._failed_this_run or route.skip_until > now:
                tried.append(f"{route.label}: resting after {route.last_error or 'a failure'}")
                continue
            try:
                turn = route.provider.complete(
                    self.system_prompt,
                    transcript,
                    tools,
                    route.model,
                    None,
                    context,
                    json_output=self.json_output and json_ok and not tools,
                )
            except ProviderError as exc:
                kind = _skip_kind(exc)
                wait = SKIP_SECONDS.get(kind, 0)
                route.skip_until = float("inf") if wait is None else (time.monotonic() + wait if wait else 0.0)
                route.last_error = kind
                self._failed_this_run.add(index)
                tried.append(f"{route.label}: {kind}")
                following = next((r for r in self.routes[index + 1 :] if r.configured), None)
                print(
                    f"[Agent:{self.name}] {route.role} {route.label} failed ({kind})"
                    + (f"; falling back to {following.role} {following.label}." if following else "; nothing left to try.")
                )
                continue
            if route.role != "primary":
                print(f"[Agent:{self.name}] Answered by the {route.role} {route.label}.")
            route.last_error = ""
            return turn

        off = [f"{r.label}: {getattr(r.provider, 'unavailable_reason', '') or 'not configured'}" for r in self.routes if not r.configured]
        raise AgentUnavailable(
            f"none of the {self.name} agent's models could answer ("
            + "; ".join(tried + off)
            + "). Tell the user this part could not be done right now."
        )


def _cut(text: str) -> str:
    return text if len(text) <= MAX_REPORT_CHARS else text[: MAX_REPORT_CHARS - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# JSON answers (the productivity agent)
# ---------------------------------------------------------------------------

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def parse_json_object(text: str) -> dict[str, Any]:
    """The one JSON object in a model's answer (a code fence around it is allowed). Raises ValueError otherwise."""
    raw = _FENCE.sub("", (text or "").strip())
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("the reply holds no JSON object")
    try:
        value = json.loads(raw[start : end + 1])
    except ValueError as exc:
        raise ValueError(f"the JSON does not parse ({exc})") from None
    if not isinstance(value, dict):
        raise ValueError("the JSON is not an object")
    return value
