"""
Tests for the System Topology page's endpoint (api_server.py GET /system/topology, system_topology.py).

    .venv\\Scripts\\python -m unittest discover -s tests -v

The real FastAPI route and the real system_topology.py, with the brain replaced by a stand-in
that answers the same public status calls the real LLMBrain does (manager_status, team_status,
heads_status, mcp_servers, capability_specs, mcp_server_notes, head_models) in the shapes they
have today. The owner's Google sign-in is a token.json written into a temporary folder
(GOOGLE_OAUTH_TOKEN_FILE), so the real utils/google_auth.py reads its scopes. Nothing here
starts a model, a tool server or a network call.

What is checked: who may ask (the header, the owner), auth_required from missing Google scopes,
online/offline from the live status, the telemetry, and that no error text from a provider, a
tool server or a file reaches the answer.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from dataclasses import dataclass
from typing import Any, Optional
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient  # noqa: E402

import api_server  # noqa: E402
import system_topology  # noqa: E402
from utils import google_auth  # noqa: E402

OWNER_ID = "owner-user-id-1"
ALICE = "alice-id"
CLASSROOM = google_auth.SCOPES_BY_SERVICE["classroom"]
CALENDAR = google_auth.SCOPES_BY_SERVICE["calendar"]
SECRET_PATH = r"C:\Users\someone\secret\mcp_servers.json"
SECRET_URL = "postgresql://test_role:not-a-real-password@db.invalid/test_db"


def headers(user: str) -> dict[str, str]:
    return {"X-Jarvis-User-Id": user}


@dataclass
class Spec:
    """The ToolSpec fields system_topology.py reads."""

    name: str
    description: str = ""
    source: str = "builtin"
    requires_confirmation: bool = False
    confirm_when: Any = None


def route(role: str, provider: str, model: str, state: str = "ready", **extra: Any) -> dict[str, Any]:
    return {"role": role, "provider": provider, "model": model, "state": state, **extra}


class FakeBrain:
    """Answers the brain's public status calls; every field is what the real LLMBrain sends today."""

    def __init__(self) -> None:
        self.available = True
        self.provider_name = "anthropic"
        self.model = "claude-sonnet-5"
        self.manager = {
            "provider": "anthropic",
            "model": "claude-sonnet-5",
            "on_standby": False,
            "primary": {"provider": "anthropic", "model": "claude-sonnet-5", "state": "ready"},
        }
        self.team = [
            {
                "name": "data", "title": "Head of Data & Documents", "tool": "delegate_to_data", "active": True,
                "routes": [route("primary", "gemini", "gemini-pro-latest"), route("fallback", "groq", "openai/gpt-oss-120b")],
                "tools": ["mcp__local_fs__read_file", "mcp__data__query"], "servers": ["local_fs", "data"],
            },
            {
                "name": "research", "title": "Head of Research", "tool": "delegate_to_research", "active": True,
                "routes": [route("primary", "gemini", "gemini-flash-latest")],
                "tools": ["mcp__web_search__web_search"], "servers": ["web_search", "fetch"],
            },
            {
                "name": "productivity", "title": "Head of Productivity", "tool": "delegate_to_productivity", "active": True,
                "routes": [route("primary", "openai", "gpt-4o-mini"), route("last resort", "gemini", "gemini-flash-lite-latest")],
                "tools": ["get_todays_agenda", "push_line_notification"], "servers": [],
            },
        ]
        self.heads = [
            {"name": "creative", "title": "Head of Creative", "tools": ["delegate_to_creative"], "owner_only": False, "state": "ready", "why": ""},
            {"name": "publishing", "title": "Head of Publishing", "tools": ["delegate_to_publishing"], "owner_only": False, "state": "ready", "why": ""},
            {"name": "analytics", "title": "Head of Analytics", "tools": ["delegate_to_analytics"], "owner_only": False, "state": "ready", "why": ""},
            {"name": "database", "title": "Head of Database", "tools": ["delegate_to_database"], "owner_only": True, "state": "ready", "why": ""},
            {"name": "schedule", "title": "Head of Schedule", "tools": ["delegate_to_calendar"], "owner_only": True, "state": "ready", "why": ""},
            {
                "name": "classroom", "title": "Google Classroom", "owner_only": True, "state": "ready", "why": "",
                "tools": ["delegate_to_classroom_courses", "delegate_to_classroom_assignments"],
            },
        ]
        self.servers = [
            {"name": "local_fs", "connected": True, "tools": ["read_file", "list_directory"], "error": ""},
            {"name": "web_search", "connected": True, "tools": ["web_search"], "error": ""},
            {"name": "fetch", "connected": True, "tools": ["fetch_url"], "error": ""},
            {"name": "memory", "connected": True, "tools": ["remember", "recall", "forget"], "error": ""},
            {"name": "data", "connected": True, "tools": ["query", "run_sql"], "error": ""},
        ]
        self.specs = [
            Spec("mcp__local_fs__read_file", "Reads a file. More words.", "mcp:local_fs"),
            Spec("mcp__local_fs__list_directory", "Lists a folder.", "mcp:local_fs"),
            Spec("mcp__web_search__web_search", "Searches the web.", "mcp:web_search"),
            Spec("mcp__fetch__fetch_url", "Reads a page.", "mcp:fetch"),
            Spec("mcp__memory__remember", "Saves.", "mcp:memory"),
            Spec("mcp__memory__recall", "Finds.", "mcp:memory"),
            Spec("mcp__memory__forget", "Deletes.", "mcp:memory", requires_confirmation=True),
            Spec("mcp__data__query", "SELECT.", "mcp:data"),
            Spec("mcp__data__run_sql", "Changes.", "mcp:data", requires_confirmation=True),
            Spec("get_todays_agenda", "Agenda.", "agent:productivity"),
            Spec("push_line_notification", "LINE.", "agent:productivity"),
            Spec("delegate_to_data", "Hands a task.", "agent"),
            Spec("delegate_to_creative"), Spec("delegate_to_publishing"), Spec("delegate_to_analytics"),
            Spec("delegate_to_database"), Spec("delegate_to_calendar", confirm_when=lambda args: True),
            Spec("delegate_to_classroom_courses"), Spec("delegate_to_classroom_assignments"),
            Spec("generate_trip_plan_async", "Plans."), Spec("check_poll_status_async"), Spec("finalize_trip_plan_async"),
            Spec("trigger_n8n_webhook", requires_confirmation=True),
            Spec("get_system_status", "Status."), Spec("create_new_tool", requires_confirmation=True),
        ]
        self.calls: list[str] = []

    def manager_status(self) -> dict[str, Any]:
        return self.manager

    def team_status(self) -> list[dict[str, Any]]:
        return self.team

    def heads_status(self) -> list[dict[str, Any]]:
        return self.heads

    def mcp_servers(self) -> list[dict[str, Any]]:
        return self.servers

    def capability_specs(self) -> list[Spec]:
        self.calls.append("capability_specs")
        return self.specs

    def mcp_server_notes(self) -> dict[str, str]:
        return {"web_search": "Search the web."}

    def head_models(self) -> dict[str, list[dict[str, str]]]:
        return {"creative": [{"provider": "openai", "model": "gpt-image-2"}]}

    def head(self, name: str) -> dict[str, Any]:
        return next(row for row in self.heads if row["name"] == name)


class TopologyApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.brain = FakeBrain()
        self.folder = tempfile.mkdtemp(prefix="jarvis-topology-")
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.token = os.path.join(self.folder, "token.json")
        self.write_token(CLASSROOM + CALENDAR)
        # The set-up checks would read this machine's .env: every tool counts as set up here.
        all_set_up = {name: ((lambda: True), note) for name, (_, note) in system_topology.SETUP_CHECKS.items()}
        for patcher in (
            mock.patch.object(api_server, "_get_brain", lambda: self.brain),
            mock.patch.dict(os.environ, {"JARVIS_OWNER_USER_ID": OWNER_ID, "GOOGLE_OAUTH_TOKEN_FILE": self.token}),
            mock.patch.dict(system_topology.SETUP_CHECKS, all_set_up),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        # Not entered as a context manager: the app's start-up (the real brain, speech) never runs.
        self.client = TestClient(api_server.app)

    # ---- helpers ------------------------------------------------------------

    def write_token(self, scopes: Optional[list[str]]) -> None:
        if scopes is None:
            if os.path.exists(self.token):
                os.remove(self.token)
            return
        with open(self.token, "w", encoding="utf-8") as handle:
            json.dump({"token": "x", "refresh_token": "y", "scopes": scopes}, handle)

    def topology(self, user: str = OWNER_ID) -> dict[str, Any]:
        response = self.client.get("/system/topology", headers=headers(user))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def provider(self, data: dict[str, Any], pid: str) -> dict[str, Any]:
        return next(p for p in data["providers"] if p["id"] == pid)

    def ids(self, data: dict[str, Any]) -> set[str]:
        return {p["id"] for p in data["providers"]}

    # ---- who may ask ------------------------------------------------------------

    def test_the_user_header_is_required(self) -> None:
        for sent in ({}, headers(""), headers("   ")):
            response = self.client.get("/system/topology", headers=sent)
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json()["code"], "no_user")
        self.assertEqual(self.brain.calls, [], "nothing is read for an anonymous request")

    def test_only_get_is_served(self) -> None:
        for method in ("post", "patch", "delete"):
            response = getattr(self.client, method)("/system/topology", headers=headers(OWNER_ID))
            self.assertEqual(response.status_code, 405, method)

    def test_owner_sees_the_owner_only_parts(self) -> None:
        data = self.topology(OWNER_ID)
        self.assertTrue(data["viewer"]["owner"])
        self.assertEqual(data["viewer"]["hidden"], 0)
        self.assertTrue({"head:database", "head:schedule", "head:classroom", "channel:line"} <= self.ids(data))
        self.assertTrue(self.provider(data, "head:database")["owner_only"])
        self.assertTrue(self.provider(data, "channel:line")["owner_only"])

    def test_anyone_else_never_sees_them(self) -> None:
        data = self.topology(ALICE)
        self.assertFalse(data["viewer"]["owner"])
        self.assertEqual(data["viewer"]["hidden"], 4)
        self.assertFalse({"head:database", "head:schedule", "head:classroom", "channel:line"} & self.ids(data))
        text = json.dumps(data)
        self.assertNotIn("delegate_to_database", text)
        self.assertNotIn("classroom", text.lower())
        self.assertNotIn("googleapis.com", text)

    def test_owner_is_decided_by_the_server_setting_only(self) -> None:
        with mock.patch.dict(os.environ, {"JARVIS_OWNER_USER_ID": ""}):
            self.assertFalse(self.topology(OWNER_ID)["viewer"]["owner"])
        self.assertFalse(self.topology("owner")["viewer"]["owner"])  # the word is not an id

    def test_approval_tools_are_marked_for_whoever_asks(self) -> None:
        owner = self.topology(OWNER_ID)
        forget = next(c for c in self.provider(owner, "mcp:memory")["capabilities"] if c["name"] == "forget")
        self.assertTrue(forget["needs_approval"])
        self.assertEqual(forget["status"], "online")
        self.assertIn("Allow", forget["note"])
        calendar = self.provider(owner, "head:schedule")["capabilities"][0]
        self.assertTrue(calendar["needs_approval"], "confirm_when counts as asking first")

        alice = self.topology(ALICE)
        forget = next(c for c in self.provider(alice, "mcp:memory")["capabilities"] if c["name"] == "forget")
        self.assertEqual(forget["status"], "offline")
        self.assertIn("Owner only", forget["note"])
        recall = next(c for c in self.provider(alice, "mcp:memory")["capabilities"] if c["name"] == "recall")
        self.assertEqual(recall["status"], "online")

    # ---- auth_required: the owner's Google sign-in ----------------------------------

    def test_missing_classroom_scope_is_auth_required(self) -> None:
        # The saved sign-in holds Calendar and one of Classroom's two scopes (what this PC's token.json had).
        self.write_token(CALENDAR + [CLASSROOM[0], "https://www.googleapis.com/auth/classroom.student-submissions.me.readonly"])
        self.brain.head("classroom").update(state="not configured", why="not signed in (run utils/google_auth.py)")
        data = self.topology()
        classroom = self.provider(data, "head:classroom")
        self.assertEqual(classroom["status"], "auth_required")
        self.assertEqual(classroom["missing_scopes"], [CLASSROOM[1]])
        self.assertEqual(classroom["status_note"], "Missing Google scopes")
        self.assertTrue(any("google_auth.py" in line for line in classroom["diagnostics"]))
        self.assertTrue(all(c["status"] == "auth_required" for c in classroom["capabilities"]))
        self.assertEqual(self.provider(data, "head:schedule")["status"], "online")
        self.assertEqual(data["telemetry"]["status"], "degraded")

    def test_no_token_at_all_is_auth_required_with_every_scope(self) -> None:
        self.write_token(None)
        for name in ("classroom", "schedule"):
            self.brain.head(name).update(state="not configured", why="not signed in (run utils/google_auth.py)")
        data = self.topology()
        self.assertEqual(self.provider(data, "head:classroom")["missing_scopes"], CLASSROOM)
        self.assertEqual(self.provider(data, "head:schedule")["missing_scopes"], CALENDAR)
        self.assertEqual(self.provider(data, "head:schedule")["status"], "auth_required")

    def test_scope_details_are_owner_only(self) -> None:
        self.write_token([])
        self.brain.head("classroom").update(state="not configured", why="not signed in")
        self.assertNotIn("googleapis.com", json.dumps(self.topology(ALICE)))

    def test_switched_off_and_not_set_up_are_offline(self) -> None:
        self.brain.head("schedule").update(state="off", why="switched off (JARVIS_CALENDAR=0)")
        self.brain.head("creative").update(state="not configured", why="needs OPENAI_API_KEY")
        data = self.topology()
        self.assertEqual(self.provider(data, "head:schedule")["status"], "offline")
        self.assertEqual(self.provider(data, "head:schedule")["status_note"], "Switched off")
        creative = self.provider(data, "head:creative")
        self.assertEqual(creative["status"], "offline")
        self.assertIn("needs OPENAI_API_KEY", creative["diagnostics"][0])

    def test_a_rejected_primary_key_is_auth_required(self) -> None:
        self.brain.manager = {
            "provider": "gemini", "model": "gemini-flash-lite-latest", "on_standby": True,
            "primary": {"provider": "anthropic", "model": "claude-sonnet-5", "state": "failed", "last_error": "auth"},
        }
        jarvis = self.topology()["orchestrator"]
        self.assertEqual(jarvis["status"], "auth_required")
        self.assertIn("key rejected", jarvis["diagnostics"][0])

    # ---- online / offline from the live status ----------------------------------------

    def test_everything_up_is_optimal(self) -> None:
        data = self.topology()
        self.assertEqual(data["telemetry"]["status"], "optimal", [p["id"] for p in data["providers"] if p["status"] != "online"])
        self.assertEqual(data["telemetry"]["active_providers"], data["telemetry"]["total_providers"])
        self.assertEqual(
            data["telemetry"]["total_capabilities"], sum(len(p["capabilities"]) for p in data["providers"])
        )
        jarvis = data["orchestrator"]
        self.assertEqual((jarvis["vendor"], jarvis["model"], jarvis["status"]), ("Anthropic", "claude-sonnet-5", "online"))

    def test_shape_kinds_and_links(self) -> None:
        data = self.topology()
        kinds = {p["id"]: (p["kind"], p["category"], p["access"]) for p in data["providers"]}
        self.assertEqual(kinds["agent:data"], ("agent", "agent", "API"))
        self.assertEqual(kinds["mcp:web_search"], ("mcp_server", "mcp", "MCP"))
        self.assertEqual(kinds["mcp:memory"], ("mcp_server", "data", "MCP"))
        self.assertEqual(kinds["head:database"], ("agent", "data", "Internal"))
        self.assertEqual(kinds["core:trip"], ("core", "core", "Internal"))
        self.assertEqual(self.provider(data, "agent:data")["uses"], ["mcp:local_fs", "mcp:data"])
        # An agent's server tools hang on the server; its own tools on the agent; nothing twice.
        caps = [c["id"] for p in data["providers"] for c in p["capabilities"]]
        self.assertEqual(len(caps), len(set(caps)))
        self.assertIn("cap:get_todays_agenda", [c["id"] for c in self.provider(data, "agent:productivity")["capabilities"]])
        self.assertNotIn("cap:delegate_to_data", caps)
        self.assertEqual(
            [c["label"] for c in self.provider(data, "mcp:local_fs")["capabilities"]], ["Read a file", "List a folder"]
        )
        for p in data["providers"] + [data["orchestrator"]]:
            self.assertTrue(p["workflows"], p["id"])
            for flow in p["workflows"]:
                self.assertTrue(flow["label"] and flow["prompt"])

    def test_an_agent_on_its_fallback_stays_online_but_says_so(self) -> None:
        self.brain.team[0]["routes"][0].update(state="resting", last_error="quota")
        data = self.topology()
        agent = self.provider(data, "agent:data")
        self.assertEqual(agent["status"], "online")
        self.assertEqual(agent["status_note"], "On fallback")
        self.assertEqual((agent["vendor"], agent["model"]), ("Groq", "openai/gpt-oss-120b"))
        self.assertIn("quota used up", agent["diagnostics"][0])

    def test_an_agent_with_no_key_is_offline(self) -> None:
        self.brain.team[1].update(
            active=False,
            routes=[route("primary", "gemini", "gemini-flash-latest", "not configured", why=f"GEMINI_API_KEY is not set ({SECRET_PATH})")],
        )
        data = self.topology()
        research = self.provider(data, "agent:research")
        self.assertEqual((research["status"], research["status_note"]), ("offline", "No model key"))
        self.assertNotIn(SECRET_PATH, json.dumps(data))
        self.assertEqual(data["telemetry"]["status"], "degraded")
        self.assertEqual(data["telemetry"]["active_providers"], data["telemetry"]["total_providers"] - 1)

    def test_every_route_refusing_its_key_is_auth_required(self) -> None:
        self.brain.team[2]["routes"] = [
            route("primary", "openai", "gpt-4o-mini", "failed", last_error="auth"),
            route("last resort", "gemini", "gemini-flash-lite-latest", "failed", last_error="permission"),
        ]
        agent = self.provider(self.topology(), "agent:productivity")
        self.assertEqual(agent["status"], "auth_required")

    def test_a_disconnected_server_is_offline_and_its_error_stays_here(self) -> None:
        self.brain.servers[3].update(connected=False, tools=[], error=f"could not connect to {SECRET_URL} from {SECRET_PATH}")
        data = self.topology()
        memory = self.provider(data, "mcp:memory")
        self.assertEqual((memory["status"], memory["status_note"]), ("offline", "Not connected"))
        self.assertEqual(memory["capabilities"], [])
        text = json.dumps(data)
        for secret in (SECRET_URL, SECRET_PATH, "not-a-real-password", "db.invalid"):
            self.assertNotIn(secret, text)

    def test_a_starting_server_says_so(self) -> None:
        self.brain.servers[1].update(connected=False, tools=[], error="starting")
        self.assertEqual(self.provider(self.topology(), "mcp:web_search")["status_note"], "Starting")

    def test_a_tool_that_is_not_set_up_is_offline(self) -> None:
        with mock.patch.dict(system_topology.SETUP_CHECKS, {"get_todays_agenda": (lambda: False, "Not set up: agenda.")}):
            agent = self.provider(self.topology(), "agent:productivity")
        agenda = next(c for c in agent["capabilities"] if c["name"] == "get_todays_agenda")
        self.assertEqual(agenda["status"], "offline")
        self.assertEqual(agent["status"], "online", "the agent still works for its other tools")

    def test_standby_is_degraded_and_explained(self) -> None:
        self.brain.manager = {
            "provider": "gemini", "model": "gemini-flash-lite-latest", "on_standby": True,
            "primary": {"provider": "anthropic", "model": "claude-sonnet-5", "state": "resting", "last_error": "billing", "back_in": 512},
        }
        self.brain.provider_name, self.brain.model = "gemini", "gemini-flash-lite-latest"
        data = self.topology()
        jarvis = data["orchestrator"]
        self.assertEqual((jarvis["status"], jarvis["status_note"]), ("online", "On standby"))
        self.assertEqual((jarvis["vendor"], jarvis["model"]), ("Google", "gemini-flash-lite-latest"))
        self.assertIn("out of credit", jarvis["diagnostics"][0])
        self.assertIn("9 min", jarvis["diagnostics"][0])
        self.assertEqual([r["role"] for r in jarvis["routes"]], ["primary", "standby"])
        self.assertEqual(data["telemetry"]["status"], "degraded")

    def test_no_model_at_all_is_offline_everywhere(self) -> None:
        self.brain.available = False
        data = self.topology()
        self.assertEqual(data["orchestrator"]["status"], "offline")
        self.assertTrue(all(p["status"] == "offline" for p in data["providers"]))
        self.assertEqual(data["telemetry"], {**data["telemetry"], "status": "degraded", "active_providers": 0})

    def test_a_failing_status_read_does_not_fail_the_page(self) -> None:
        def broken() -> list[dict[str, Any]]:
            raise RuntimeError(f"boom {SECRET_PATH}")

        self.brain.team_status = broken  # type: ignore[method-assign]
        data = self.topology()
        self.assertFalse({"agent:data", "agent:research", "agent:productivity"} & self.ids(data))
        self.assertIn("mcp:web_search", self.ids(data), "the rest is still shown")
        self.assertNotIn(SECRET_PATH, json.dumps(data))

    def test_a_brain_that_cannot_start_is_503(self) -> None:
        def no_brain() -> Any:
            raise RuntimeError(f"cannot start: {SECRET_URL}")

        with mock.patch.object(api_server, "_get_brain", no_brain):
            response = self.client.get("/system/topology", headers=headers(OWNER_ID))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "unavailable")
        self.assertNotIn("db.invalid", response.text)

    def test_reading_the_page_changes_nothing(self) -> None:
        before = json.dumps([self.brain.team, self.brain.heads, self.brain.servers], sort_keys=True)
        self.topology(OWNER_ID)
        self.topology(ALICE)
        self.assertEqual(before, json.dumps([self.brain.team, self.brain.heads, self.brain.servers], sort_keys=True))


if __name__ == "__main__":
    unittest.main()
