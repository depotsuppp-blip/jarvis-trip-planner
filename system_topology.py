"""
The System Topology page's data (TripPlanner_Web /jarvis/system): what Jarvis can do right now,
which part of it does the work, and whether that part works - built from the brain's own live
status (LLMBrain.manager_status, team_status, heads_status, mcp_servers, capability_specs) and
the owner's Google sign-in file. Nothing here is a made-up number; the only written-in parts are
the words a person reads (names, descriptions, starter prompts).

    build(brain, owner=True) -> {
      "generated_at": "2026-11-19T10:00:00Z",
      "viewer": {"owner": true, "hidden": 0},
      "orchestrator": Provider,          # Jarvis itself: the executive manager
      "providers": [Provider, ...],      # agents (delegate_to_* tools), MCP tool servers, core tools
      "telemetry": {"status": "optimal" | "degraded", "active_providers": 12,
                    "total_providers": 14, "total_capabilities": 41}
    }

    Provider = {
      "id": "agent:data" | "head:creative" | "mcp:web_search" | "core:trip" | "jarvis",
      "name": "Data & Documents", "title": "Head of Data & Documents",
      "kind": "orchestrator" | "agent" | "mcp_server" | "core",
      "category": "orchestrator" | "agent" | "mcp" | "data" | "core",   # the page's colour family
      "vendor": "Google", "model": "gemini-pro-latest" | null,
      "access": "API" | "MCP" | "Internal",
      "status": "online" | "auth_required" | "offline",
      "status_note": "Ready",            # a few words for a badge's title
      "diagnostics": ["..."],            # sentences, this module's own words only
      "missing_scopes": ["https://www.googleapis.com/auth/..."],   # Google sign-in, owner only
      "owner_only": false,
      "description": "...",
      "routes": [{"role", "vendor", "model", "state"}],   # the models it tries, in order
      "uses": ["mcp:local_fs"],          # tool servers an agent works through
      "capabilities": [{"id": "cap:<tool>", "name": "<tool>", "label": "Read file",
                        "description": "...", "needs_approval": false,
                        "status": "online" | "auth_required" | "offline", "note": ""}],
      "workflows": [{"label": "...", "prompt": "..."}]   # starter prompts for the chat
    }

STATUS. online: Jarvis can use it now. auth_required: it is set up but a sign-in is missing or
was refused - the owner's Google token lacks a scope Classroom or Calendar needs, or a model
provider rejected the key. offline: not set up (no key, a package missing), switched off by its
JARVIS_* setting, or a tool server that is not connected. A provider is online while at least
one of its routes can answer, even when it is on a fallback (diagnostics say so). The telemetry
is "optimal" only while Jarvis answers on its primary model and every provider shown is online.

WHO SEES WHAT. The page serves everyone who signs in to the web app. Jarvis offers its owner-only
parts (the live database, the owner's calendar and Classroom) to the owner alone, so for anyone
else they are left out (viewer.hidden counts them), and a tool that needs the owner's approval is
shown as offline with the note "Owner only" - on the web it is declined for everyone else.

PRIVACY. No error text from a provider, a tool server or a file is passed on: it can name paths,
hosts and database logins. Diagnostics are this module's own sentences; the only names taken
from the configuration are model names, tool names and Google scope URLs.
"""

from __future__ import annotations

import datetime
import re
from typing import Any, Iterable, Optional

ONLINE = "online"
AUTH_REQUIRED = "auth_required"
OFFLINE = "offline"

VENDORS = {"anthropic": "Anthropic", "gemini": "Google", "google": "Google", "openai": "OpenAI", "groq": "Groq"}

# ProviderError.kind (llm_brain.py) in a few words.
FAILURES = {
    "billing": "out of credit",
    "quota": "quota used up",
    "rate_limit": "rate limited",
    "server": "provider error",
    "auth": "key rejected",
    "permission": "no permission",
    "not_found": "model not found",
    "no_key": "no key",
    "no_sdk": "SDK not installed",
    "timeout": "timed out",
    "bad_request": "request refused",
}
AUTH_FAILURES = {"auth", "permission"}

# Short names a person reads; the brain's titles stay in "title".
AGENT_NAMES = {"data": "Data & Documents", "research": "Research", "productivity": "Productivity"}
HEAD_NAMES = {
    "creative": "Creative",
    "publishing": "Publishing",
    "analytics": "Analytics",
    "database": "Database",
    "schedule": "Schedule",
    "classroom": "Google Classroom",
}
SERVER_NAMES = {
    "local_fs": "Local Files",
    "web_search": "Web Search",
    "fetch": "Web Reader",
    "memory": "Memory Graph",
    "data": "Data Analyst",
}
SERVER_VENDORS = {
    "local_fs": "Local",
    "web_search": "DuckDuckGo",
    "fetch": "Local",
    "memory": "PostgreSQL + OpenAI",
    "data": "Local SQLite",
}
# The emerald family: memory and data.
DATA_SERVERS = {"memory", "data"}
HEAD_PROFILE = {
    # name: (vendor, access, category)
    "creative": ("OpenAI", "API", "agent"),
    "publishing": ("OpenAI", "API", "agent"),
    "analytics": ("Local", "Internal", "agent"),
    "database": ("PostgreSQL", "Internal", "data"),
    "schedule": ("Google", "API", "agent"),
    "classroom": ("Google", "API", "agent"),
}
# Which Google service (utils/google_auth.SCOPES_BY_SERVICE) a head signs in to.
GOOGLE_SERVICE = {"schedule": "calendar", "classroom": "classroom"}

# Jarvis's own built-in tools, grouped by what they are for. A built-in tool that is not
# listed here (a new one, a plugin) lands in "system".
CORE_GROUPS: list[tuple[str, str, tuple[str, ...]]] = [
    ("trip", "Trip Planner", ("generate_trip_plan_async", "check_poll_status_async", "finalize_trip_plan_async")),
    ("automation", "Automations", ("trigger_n8n_webhook", "send_line_notification")),
    ("subtrack", "Subtrack", ("query_subtrack_database", "start_subtrack_env", "stop_subtrack_env")),
    ("system", "System", ("get_system_status", "analyze_screen", "create_new_tool", "list_capabilities")),
]

DESCRIPTIONS = {
    "jarvis": "The executive manager. It reads every request, answers what it can itself, and hands the rest to its agents and tool servers.",
    "agent:data": "Reads files, PDFs and Word documents, and loads CSV, TSV and JSON files into tables to answer with SQL.",
    "agent:research": "Searches the web and reads pages, then reports back with sources.",
    "agent:productivity": "Today's agenda, LINE messages to you, and to-do lists and schedules as drafts.",
    "head:creative": "Makes images from a description and saves them to Jarvis's artifacts.",
    "head:publishing": "Writes a slide deck and builds it as a 16:9 PowerPoint file, Canva-ready on request.",
    "head:analytics": "Turns a table into an Excel spreadsheet you can download.",
    "head:database": "Answers questions about the live app database with read-only SQL.",
    "head:schedule": "Reads your Google Calendar and adds events after you approve each one.",
    "head:classroom": "Reads your Google Classroom courses and assignments (read-only).",
    "mcp:local_fs": "Reads text, PDF and Word files inside the project folder, including files you attach.",
    "mcp:web_search": "Searches the web (DuckDuckGo) for current information.",
    "mcp:fetch": "Opens a public web page and reads its text, respecting robots.txt.",
    "mcp:memory": "Jarvis's long-term memory of you: a knowledge graph of people, places and projects.",
    "mcp:data": "Your own data workspace: loaded tables you can query with SQL.",
    "core:trip": "Plans group trips in the Trip Planner app: creates the poll, checks votes, locks the final plan.",
    "core:automation": "Runs your n8n workflows and sends LINE notifications.",
    "core:subtrack": "Reads Subtrack's ESG records and starts or stops its development environment.",
    "core:system": "Jarvis's own tools: how the machine is doing, what is on the screen, and new tools it writes itself.",
}

WORKFLOWS: dict[str, list[tuple[str, str]]] = {
    "jarvis": [
        ("What can you do?", "What can you do? List your agents and tools in a few lines."),
        ("System check", "Give me a short status report on all your systems."),
    ],
    "agent:data": [
        ("List my uploads", "List the files in the uploads folder and tell me what each one is."),
        ("Summarise a document", "Read PRODUCT.md and summarise it in five bullet points."),
    ],
    "agent:research": [
        ("Today's AI news", "Search the web for today's top AI news and give me three headlines with sources."),
        ("Weather now", "What's the weather in Bangkok right now?"),
    ],
    "agent:productivity": [
        ("Today's agenda", "What's on my agenda today?"),
        ("Draft a to-do list", "Draft a to-do list for preparing a weekend trip to Chiang Mai."),
    ],
    "head:creative": [
        ("Generate an image", "Generate an image of a neon cyberpunk Bangkok skyline at night."),
        ("Make a logo", "Make a minimalist logo of a paper plane on a white background."),
    ],
    "head:publishing": [
        ("Build a deck", "Create a 6-slide presentation about the history of AI."),
        ("Canva-ready pitch", "Make a Canva-ready deck pitching a weekend trip to Chiang Mai."),
    ],
    "head:analytics": [
        ("Budget spreadsheet", "Build a spreadsheet of a monthly budget: rent 12000, food 6000, transport 2500, savings 5000."),
        ("Table to Excel", "Turn this into an Excel file: Bangkok 5 days, Chiang Mai 3 days, Phuket 4 days."),
    ],
    "head:database": [
        ("Trips this week", "How many trips were created this week?"),
        ("Popular destinations", "Which destinations appear most often in trip polls?"),
    ],
    "head:schedule": [
        ("Next 7 days", "What's on my calendar for the next 7 days?"),
        ("Add an event", "Add lunch with Nok on Saturday at noon to my calendar."),
    ],
    "head:classroom": [
        ("My courses", "List my Google Classroom courses."),
        ("Homework due", "What homework is due this week?"),
    ],
    "mcp:local_fs": [
        ("Browse the project", "List the files in the project folder."),
        ("Read a file", "Read DESIGN.md and tell me what it covers."),
    ],
    "mcp:web_search": [
        ("Search the web", "Search the web for the latest Next.js release and what changed."),
    ],
    "mcp:fetch": [
        ("Read a page", "Read https://news.ycombinator.com and list the top five stories."),
    ],
    "mcp:memory": [
        ("What you remember", "What do you remember about me?"),
        ("Remember something", "Remember that my favourite food is khao soi."),
    ],
    "mcp:data": [
        ("My tables", "What tables are in my data workspace?"),
    ],
    "core:trip": [
        ("Plan a trip", "Plan a 3-day trip to Chiang Mai for 4 people."),
        ("Poll status", "How many people have voted in my latest trip poll?"),
    ],
    "core:automation": [
        ("LINE me", "Send me a LINE message saying the build is done."),
        ("My workflows", "Which n8n workflows can you run?"),
    ],
    "core:subtrack": [
        ("Latest ESG records", "Show me the latest Subtrack ESG records."),
    ],
    "core:system": [
        ("Machine health", "How is the system doing? CPU, memory and disk."),
        ("List capabilities", "List your capabilities."),
    ],
}

# Capability names and one-line notes for the tools whose own description is written for the model.
CAPABILITY_LABELS = {
    "delegate_to_creative": ("Generate an image", "One image from a description, saved to artifacts/."),
    "delegate_to_publishing": ("Build a presentation", "A 16:9 PowerPoint file written from a brief."),
    "delegate_to_analytics": ("Build a spreadsheet", "An Excel file from a table."),
    "delegate_to_database": ("Read-only SQL", "One SELECT on the live app database, in a read-only transaction."),
    "delegate_to_calendar": ("Calendar events", "Lists upcoming events; creates one after you approve it."),
    "delegate_to_classroom_courses": ("List courses", "Your active Google Classroom courses."),
    "delegate_to_classroom_assignments": ("List assignments", "The published assignments of one course, with due dates."),
    "generate_trip_plan_async": ("Plan a trip", "Creates a trip poll in the Trip Planner app and answers with its link."),
    "check_poll_status_async": ("Check a poll", "How many people have voted so far."),
    "finalize_trip_plan_async": ("Finalize a trip", "Locks a poll and generates the final itinerary."),
    "trigger_n8n_webhook": ("Run an n8n workflow", "Calls one of your n8n workflows' webhooks."),
    "send_line_notification": ("Send a LINE message", "A push message to your LINE."),
    "push_line_notification": ("Send a LINE message", "A push message to the owner's LINE, at most two per task."),
    "get_todays_agenda": ("Today's agenda", "Today's events from the configured Google Calendar."),
    "get_system_status": ("System status", "CPU, memory, disk and what Jarvis is running."),
    "analyze_screen": ("Look at the screen", "A fresh screenshot of this computer's screen, described."),
    "create_new_tool": ("Write a new tool", "Writes a Python plugin for a capability Jarvis does not have yet."),
    "list_capabilities": ("List capabilities", "What Jarvis can do right now."),
    "query_subtrack_database": ("Latest ESG records", "The five most recent Subtrack carbon and expense records."),
    "start_subtrack_env": ("Start Subtrack", "Wakes Subtrack's Docker containers and dev servers."),
    "stop_subtrack_env": ("Stop Subtrack", "Stops Subtrack's servers and containers."),
}

# Names for the bundled tool servers' tools; their notes come from the servers' own descriptions.
MCP_LABELS = {
    "read_file": "Read a file",
    "list_directory": "List a folder",
    "web_search": "Search the web",
    "fetch_url": "Read a web page",
    "remember": "Remember",
    "recall": "Recall",
    "forget": "Forget",
    "list_tables": "List tables",
    "describe_table": "Describe a table",
    "load_file": "Load a data file",
    "query": "SQL query",
    "run_sql": "Change data with SQL",
}

MAX_NOTE = 220


def _vendor(provider: Any) -> str:
    name = str(provider or "").strip().lower()
    return VENDORS.get(name, name.title() if name else "")


def _words(kind: Any) -> str:
    return FAILURES.get(str(kind or ""), "failed")


def _humanize(name: str) -> str:
    words = re.sub(r"[_\-]+", " ", name).strip()
    return (words[:1].upper() + words[1:]) if words else name


def _first_sentence(text: str) -> str:
    """The first sentence of a tool's own description, cut to MAX_NOTE characters."""
    flat = " ".join(str(text or "").split())
    first = re.split(r"(?<=[.!?])\s", flat, maxsplit=1)[0]
    return first if len(first) <= MAX_NOTE else first[: MAX_NOTE - 1].rstrip() + "…"


def _needs_approval(spec: Any) -> bool:
    return bool(getattr(spec, "requires_confirmation", False) or getattr(spec, "confirm_when", None) is not None)


def _capability(name: str, spec: Any = None, *, label: Optional[str] = None) -> dict[str, Any]:
    known = CAPABILITY_LABELS.get(name)
    short = name.split("__", 2)[-1] if name.startswith("mcp__") else name
    description = known[1] if known else _first_sentence(getattr(spec, "description", "") if spec is not None else "")
    return {
        "id": f"cap:{name}",
        "name": short,
        "label": label or (known[0] if known else MCP_LABELS.get(short) if name.startswith("mcp__") else None) or _humanize(short),
        "description": description,
        "needs_approval": _needs_approval(spec) if spec is not None else False,
        "status": ONLINE,
        "note": "",
    }


def _provider(pid: str, **fields: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": pid,
        "name": "",
        "title": "",
        "kind": "agent",
        "category": "agent",
        "vendor": "",
        "model": None,
        "access": "API",
        "status": ONLINE,
        "status_note": "Ready",
        "diagnostics": [],
        "missing_scopes": [],
        "owner_only": False,
        "description": DESCRIPTIONS.get(pid, ""),
        "routes": [],
        "uses": [],
        "capabilities": [],
        "workflows": [{"label": label, "prompt": prompt} for label, prompt in WORKFLOWS.get(pid, [])],
    }
    row.update(fields)
    return row


# ---- the orchestrator ---------------------------------------------------------------------


def _orchestrator(brain: Any) -> dict[str, Any]:
    status = brain.manager_status() if brain.available else {}
    primary = status.get("primary") or {}
    on_standby = bool(status.get("on_standby"))
    routes = []
    if primary:
        routes.append({
            "role": "primary",
            "vendor": _vendor(primary.get("provider")),
            "model": str(primary.get("model") or ""),
            "state": str(primary.get("state") or ""),
        })
    if on_standby:
        routes.append({
            "role": "standby",
            "vendor": _vendor(status.get("provider")),
            "model": str(status.get("model") or ""),
            "state": "ready",
        })
    row = _provider(
        "jarvis",
        name="Jarvis",
        title="Executive manager",
        kind="orchestrator",
        category="orchestrator",
        vendor=_vendor(brain.provider_name) if brain.available else "",
        model=str(brain.model) if brain.available else None,
        access="API",
        routes=routes,
    )
    if not brain.available:
        row.update(status=OFFLINE, status_note="No model", model=None)
        row["diagnostics"].append("No language model is configured: set ANTHROPIC_API_KEY or GEMINI_API_KEY in .env and restart.")
        return row
    if on_standby:
        why = _words(primary.get("last_error")) if primary.get("last_error") else str(primary.get("state") or "unavailable")
        sentence = (
            f"Answering on the standby, {row['vendor']} {row['model']}: the primary "
            f"{_vendor(primary.get('provider'))} {primary.get('model')} is {primary.get('state')} ({why})"
        )
        back_in = primary.get("back_in")
        if isinstance(back_in, (int, float)) and back_in > 0:
            sentence += f"; tried again in {max(1, round(back_in / 60))} min"
        row["diagnostics"].append(sentence + ".")
        row["status_note"] = "On standby"
        if primary.get("last_error") in AUTH_FAILURES:
            row["status"] = AUTH_REQUIRED
            row["status_note"] = "Primary key rejected"
    elif primary.get("state") == "not configured":
        row["diagnostics"].append(f"Answering on {row['vendor']} {row['model']}; the Anthropic primary has no key.")
    return row


# ---- the specialist agents (agents/) --------------------------------------------------------------


def _routes(rows: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {
            "role": str(r.get("role") or ""),
            "vendor": _vendor(r.get("provider")),
            "model": str(r.get("model") or ""),
            "state": str(r.get("state") or ""),
            **({"error": _words(r.get("last_error"))} if r.get("last_error") else {}),
        }
        for r in rows
    ]


def _agent(row: dict[str, Any], specs: dict[str, Any], servers: set[str]) -> dict[str, Any]:
    name = str(row.get("name") or "")
    raw_routes = list(row.get("routes") or [])
    routes = _routes(raw_routes)
    answering = next((r for r in routes if r["state"] == "ready"), None)
    shown = answering or (routes[0] if routes else {"vendor": "", "model": ""})
    tools = [str(t) for t in row.get("tools") or []]
    own = [t for t in tools if not t.startswith("mcp__")]
    provider = _provider(
        f"agent:{name}",
        name=AGENT_NAMES.get(name, _humanize(name)),
        title=str(row.get("title") or ""),
        vendor=shown["vendor"],
        model=shown["model"] or None,
        routes=routes,
        uses=[f"mcp:{s}" for s in row.get("servers") or [] if s in servers],
        capabilities=[_capability(t, specs.get(t)) for t in own],
    )
    for cap in provider["capabilities"]:
        _mark_setup(cap)
    configured = [r for r in raw_routes if r.get("state") != "not configured"]
    if row.get("active"):
        if routes and answering is not routes[0]:
            first = raw_routes[0]
            provider["diagnostics"].append(
                f"Its primary, {_vendor(first.get('provider'))} {first.get('model')}, is {first.get('state')}"
                + (f" ({_words(first.get('last_error'))})" if first.get("last_error") else "")
                + (f": working on {shown['vendor']} {shown['model']}." if answering else ".")
            )
            provider["status_note"] = "On fallback"
        if not answering and configured and all(r.get("last_error") in AUTH_FAILURES for r in configured):
            provider.update(status=AUTH_REQUIRED, status_note="Key rejected")
            provider["diagnostics"].append("Every model it can use refused its key: check the API keys in .env.")
        return provider
    if not configured:
        provider.update(status=OFFLINE, status_note="No model key")
        provider["diagnostics"].append("None of its models has a key: add one of their API keys to .env and restart.")
    else:
        provider.update(status=OFFLINE, status_note="Servers down")
        provider["diagnostics"].append("Its tool servers are not connected, so its tools stay with Jarvis.")
    return provider


# ---- the heads (plain Python behind a delegate_to_* tool) ------------------------------------------


def _google_missing(service: str) -> Optional[list[str]]:
    """The scopes the owner's Google token lacks for `service` (utils/google_auth.py); None if that cannot be read."""
    try:
        from utils import google_auth

        scopes = google_auth.SCOPES_BY_SERVICE[service]
        if not google_auth.token_file().is_file():
            return list(scopes)
        return list(google_auth.missing_scopes(scopes))
    except Exception:  # noqa: BLE001 - a status page must never fail on a sign-in file
        return None


def _head(row: dict[str, Any], specs: dict[str, Any], models: dict[str, list[dict[str, str]]], owner: bool) -> dict[str, Any]:
    name = str(row.get("name") or "")
    vendor, access, category = HEAD_PROFILE.get(name, ("Local", "Internal", "agent"))
    tried = models.get(name) or []
    provider = _provider(
        f"head:{name}",
        name=HEAD_NAMES.get(name, _humanize(name)),
        title=str(row.get("title") or ""),
        vendor=vendor,
        model=tried[0]["model"] if tried else None,
        access=access,
        category=category,
        owner_only=bool(row.get("owner_only")),
        routes=[
            {"role": "primary" if i == 0 else "fallback", "vendor": _vendor(m.get("provider")), "model": m.get("model", ""), "state": ""}
            for i, m in enumerate(tried)
        ],
        capabilities=[_capability(str(t), specs.get(str(t))) for t in row.get("tools") or []],
    )
    state = str(row.get("state") or "")
    why = str(row.get("why") or "")
    service = GOOGLE_SERVICE.get(name)
    if state == "ready":
        pass
    elif state == "off":
        provider.update(status=OFFLINE, status_note="Switched off")
        provider["diagnostics"].append(f"Switched off by its setting ({why})." if why.startswith("switched off") else "It could not start.")
    elif service is not None:
        missing = _google_missing(service)
        provider.update(status=AUTH_REQUIRED, status_note="Sign-in needed")
        if missing:
            provider["missing_scopes"] = missing
            provider["status_note"] = "Missing Google scopes"
            provider["diagnostics"].append(
                f"The saved Google sign-in (token.json) is missing {len(missing)} scope{'s' if len(missing) != 1 else ''} "
                "this needs. Sign in again with `python utils/google_auth.py` and tick every box, then restart api_server."
            )
        else:
            provider["diagnostics"].append("Not signed in to Google: run `python utils/google_auth.py`, then restart api_server.")
    else:
        provider.update(status=OFFLINE, status_note="Not set up")
        provider["diagnostics"].append(f"Not set up: {why}." if why else "Not set up.")
    if provider["status"] != ONLINE:
        for cap in provider["capabilities"]:
            cap["status"] = provider["status"]
    if provider["owner_only"] and owner:
        provider["diagnostics"].append("Offered to you only: other people signed in to the web app never get this tool.")
    return provider


# ---- the MCP tool servers -------------------------------------------------------------------------


def _server(row: dict[str, Any], specs: dict[str, Any], notes: dict[str, str]) -> dict[str, Any]:
    name = str(row.get("name") or "")
    connected = bool(row.get("connected"))
    by_short = {
        spec.name.split("__", 2)[-1]: spec
        for spec in specs.values()
        if str(getattr(spec, "source", "")) == f"mcp:{name}"
    }
    tools = [str(t) for t in row.get("tools") or []] if connected else []
    caps = []
    for tool in tools:
        spec = by_short.get(tool)
        cap = _capability(spec.name if spec is not None else f"mcp__{name}__{tool}", spec)
        caps.append(cap)
    provider = _provider(
        f"mcp:{name}",
        name=SERVER_NAMES.get(name, _humanize(name)),
        title=f"{name} tool server",
        kind="mcp_server",
        category="data" if name in DATA_SERVERS else "mcp",
        vendor=SERVER_VENDORS.get(name, "Local"),
        access="MCP",
        capabilities=caps,
    )
    if not provider["description"]:
        note = notes.get(name, "")
        provider["description"] = _first_sentence(note) if note and not note.startswith("tools:") else "A tool server from mcp_servers.json."
    if not provider["workflows"]:
        provider["workflows"] = [{"label": "What can it do?", "prompt": f"What can your {provider['name']} tools do?"}]
    if not connected:
        starting = str(row.get("error") or "") == "starting"
        provider.update(status=OFFLINE, status_note="Starting" if starting else "Not connected")
        provider["diagnostics"].append(
            "Starting up: its tools appear once it is connected."
            if starting
            else "Not connected: Jarvis tries again on the next message. Check api_server's console for why."
        )
    elif not caps:
        provider["diagnostics"].append("Connected, but it offers no tools.")
    return provider


# ---- Jarvis's own built-in tools --------------------------------------------------------------------


def _line_ready() -> bool:
    try:
        from utils import line_notifier

        return line_notifier.configured()
    except Exception:  # noqa: BLE001
        return False


def _n8n_ready() -> bool:
    try:
        import n8n_trigger

        return any(url for url in n8n_trigger._discover_webhooks().values())
    except Exception:  # noqa: BLE001
        return False


def _agenda_ready() -> bool:
    try:
        from utils import calendar_reader

        return calendar_reader.configured()
    except Exception:  # noqa: BLE001
        return False


# Tools that need something set up in .env, checked by the tool's own module (no network call).
SETUP_CHECKS = {
    "send_line_notification": (_line_ready, "Not set up: needs LINE_CHANNEL_ACCESS_TOKEN and LINE_TARGET_USER_ID."),
    "push_line_notification": (_line_ready, "Not set up: needs LINE_CHANNEL_ACCESS_TOKEN and LINE_TARGET_USER_ID."),
    "trigger_n8n_webhook": (_n8n_ready, "No n8n webhook is set up (N8N_WEBHOOK_* in .env)."),
    "get_todays_agenda": (_agenda_ready, "Not set up: needs GOOGLE_SERVICE_ACCOUNT_FILE and GOOGLE_CALENDAR_ID."),
}


def _mark_setup(cap: dict[str, Any]) -> None:
    check = SETUP_CHECKS.get(cap["name"])
    if check is not None and not check[0]():
        cap.update(status=OFFLINE, note=check[1])


def _core(specs: dict[str, Any], taken: set[str]) -> list[dict[str, Any]]:
    builtins = [
        spec
        for spec in specs.values()
        if spec.name not in taken
        and not spec.name.startswith(("mcp__", "delegate_to_"))
        and not str(getattr(spec, "source", "")).startswith(("mcp:", "agent"))
    ]
    grouped: dict[str, list[Any]] = {key: [] for key, _, _ in CORE_GROUPS}
    for spec in builtins:
        key = next((k for k, _, names in CORE_GROUPS if spec.name in names), "system")
        grouped[key].append(spec)
    providers = []
    for key, title, _ in CORE_GROUPS:
        group = grouped[key]
        if not group:
            continue
        caps = [_capability(spec.name, spec) for spec in group]
        for cap in caps:
            _mark_setup(cap)
        provider = _provider(
            f"core:{key}",
            name=title,
            title="Built-in tools",
            kind="core",
            category="core",
            vendor="Local",
            access="Internal",
            capabilities=caps,
        )
        if all(cap["status"] != ONLINE for cap in caps):
            provider.update(status=OFFLINE, status_note="Not set up")
            provider["diagnostics"].append("None of its tools is set up yet.")
        providers.append(provider)
    return providers


# ---- the whole picture ------------------------------------------------------------------------------


def build(brain: Any, owner: bool = True) -> dict[str, Any]:
    """The topology as the person asking sees it (`owner`: they are JARVIS_OWNER_USER_ID). Never raises on a status read."""
    orchestrator = _orchestrator(brain)
    specs = {spec.name: spec for spec in _safe(brain.capability_specs, [])} if brain.available else {}
    servers = list(_safe(brain.mcp_servers, []))
    server_names = {str(s.get("name") or "") for s in servers}
    notes = dict(_safe(brain.mcp_server_notes, {}))
    models = dict(_safe(brain.head_models, {}))

    head_rows = list(_safe(brain.heads_status, []))
    agents = [_agent(row, specs, server_names) for row in _safe(brain.team_status, [])]
    heads = [_head(row, specs, models, owner) for row in head_rows]
    # Tools an agent or a head already shows are not shown again among the built-ins.
    taken = {cap["name"] for provider in agents + heads for cap in provider["capabilities"]}
    taken |= {str(t) for row in head_rows for t in row.get("tools") or []}
    servers_rows = [_server(row, specs, notes) for row in servers]
    core = _core(specs, taken)

    providers = agents + heads + servers_rows + core
    # A provider that cannot work cannot do any of its tools either.
    for provider in providers:
        if provider["status"] != ONLINE:
            for cap in provider["capabilities"]:
                if cap["status"] == ONLINE:
                    cap["status"] = provider["status"]
    hidden = 0
    if not owner:
        hidden = sum(1 for p in providers if p["owner_only"])
        providers = [p for p in providers if not p["owner_only"]]
        for provider in providers:
            for cap in provider["capabilities"]:
                if cap["needs_approval"] and cap["status"] == ONLINE:
                    cap.update(status=OFFLINE, note="Owner only: on the web it asks Jarvis's owner, so it is declined for you.")
    else:
        for provider in providers:
            for cap in provider["capabilities"]:
                if cap["needs_approval"] and not cap["note"]:
                    cap["note"] = "Asks you first: it runs only after you press Allow."

    if not brain.available:
        for provider in providers:
            provider.update(status=OFFLINE, status_note="Jarvis is off")
            provider["diagnostics"].insert(0, "Jarvis has no language model, so nothing can call this.")
            for cap in provider["capabilities"]:
                cap["status"] = OFFLINE

    active = sum(1 for p in providers if p["status"] == ONLINE)
    optimal = orchestrator["status"] == ONLINE and orchestrator["status_note"] == "Ready" and active == len(providers)
    return {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "viewer": {"owner": owner, "hidden": hidden},
        "orchestrator": orchestrator,
        "providers": providers,
        "telemetry": {
            "status": "optimal" if optimal else "degraded",
            "active_providers": active,
            "total_providers": len(providers),
            "total_capabilities": sum(len(p["capabilities"]) for p in providers),
        },
    }


def _safe(read: Any, fallback: Any) -> Any:
    """One status read; its fallback when it fails (a status page shows what it can)."""
    try:
        value = read()
        return fallback if value is None else value
    except Exception as exc:  # noqa: BLE001
        print(f"[Topology] A status read failed: {type(exc).__name__}")
        return fallback


if __name__ == "__main__":  # python system_topology.py: the owner's view, as JSON
    import json

    from llm_brain import LLMBrain

    print(json.dumps(build(LLMBrain()), indent=2, ensure_ascii=False))
