"""
Tests for the knowledge-graph upgrade of Jarvis's memory (mcp_memory.py, mcp_memory.sql,
mcp_client.py, request_context.py, api_server.py):

    .venv\\Scripts\\python -m unittest discover -s tests -v

* temporal facts - the status / created_at / updated_at columns, statuses from the extractor,
  a fact that ended keeping its row with its new status, status + date in recall
* deduplication - a new entry at cosine >= 0.88 (compatible type) or spelled with the same
  letters becomes the stored one; below that, or of another type, it does not
* the fixed entity types - the extractor's schema, the prompt, every type normalised
* 2-hop recall - me -> project -> technology, within MAX_EDGES / MAX_CONNECTED
* memory events - remember's structured result -> mcp_client -> the request -> /chat's final event

No database and no OpenAI: mcp_memory's transaction is replaced by FakeKG, an in-memory
stand-in that runs each of memory's statements the way its SQL is written (cosine similarity
included), and the extractor and the embeddings by fixed answers.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import json
import math
import os
import re
import sys
import unittest
import uuid
from typing import Any
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import asyncpg  # noqa: E402,F401  (the module must import, as in production)
from fastapi.testclient import TestClient  # noqa: E402

import api_server  # noqa: E402
import mcp_client  # noqa: E402
import mcp_memory as M  # noqa: E402
import mcp_stdio  # noqa: E402
import request_context  # noqa: E402

SCOPE = "user:test"
# Stored entries use dimensions 0-11; a text a test gave no vector gets one of 12-23, so it is
# never accidentally identical to a stored entry.
DIMS = 24
SPARE = 12


def unit(index: int) -> list[float]:
    vector = [0.0] * DIMS
    vector[index] = 1.0
    return vector


def near(index: int, cosine: float, other: int) -> list[float]:
    """A vector at exactly `cosine` to unit(index), leaning towards unit(other)."""
    vector = [0.0] * DIMS
    vector[index] = cosine
    vector[other] = math.sqrt(1 - cosine * cosine)
    return vector


def parse(literal: str) -> list[float]:
    return [float(x) for x in literal.strip("[]").split(",")]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


# =====================================================================
# FakeKG: memory's two tables, and each statement as its SQL is written
# =====================================================================


class FakeKG:
    def __init__(self) -> None:
        self.entities: dict[uuid.UUID, dict[str, Any]] = {}
        self.relations: dict[uuid.UUID, dict[str, Any]] = {}
        self.clock = datetime.datetime(2026, 11, 1, tzinfo=datetime.timezone.utc)
        self.calls: list[str] = []

    def tick(self) -> datetime.datetime:
        self.clock += datetime.timedelta(minutes=1)
        return self.clock

    def entity(self, name: str, kind: str = "other", description: str = "", vector: list[float] | None = None,
               scope: str = SCOPE) -> uuid.UUID:
        key = uuid.uuid4()
        self.entities[key] = {
            "id": key, "scope": scope, "name": name, "name_key": M.key_of(name), "type": kind,
            "description": description, "embedding": M.vector_literal(vector) if vector else None,
            "updated_at": self.tick(),
        }
        return key

    def relation(self, source: uuid.UUID, verb: str, target: uuid.UUID, status: str = "active", weight: float = 1.0,
                 scope: str = SCOPE) -> uuid.UUID:
        key = uuid.uuid4()
        self.relations[key] = {
            "id": key, "scope": scope, "source_id": source, "target_id": target, "relation_type": verb,
            "weight": weight, "status": status, "created_at": self.clock, "updated_at": self.tick(),
        }
        return key

    def by_key(self, scope: str, name_key: str) -> dict[str, Any] | None:
        return next((e for e in self.entities.values() if e["scope"] == scope and e["name_key"] == name_key), None)

    def named(self, name: str) -> dict[str, Any] | None:
        return self.by_key(SCOPE, M.key_of(name))

    def facts(self) -> set[tuple[str, str, str, str]]:
        E = self.entities
        return {
            (E[r["source_id"]]["name"], r["relation_type"], E[r["target_id"]]["name"], r["status"])
            for r in self.relations.values()
        }

    def _sim(self, entity: dict[str, Any], literal: str) -> float | None:
        if entity["embedding"] is None:
            return None
        return cosine(parse(entity["embedding"]), parse(literal))

    def _edge(self, r: dict[str, Any]) -> dict[str, Any]:
        s, t = self.entities[r["source_id"]], self.entities[r["target_id"]]
        row = dict(r)
        for side, e in (("source", s), ("target", t)):
            row.update({f"{side}_name": e["name"], f"{side}_type": e["type"], f"{side}_description": e["description"]})
        return row

    def run(self, sql: str, args: tuple) -> list[dict[str, Any]]:
        self.calls.append(sql)
        E, R = self.entities, self.relations
        if sql == M._KNOWN_SQL:
            scope, limit = args
            rows = sorted((e for e in E.values() if e["scope"] == scope), key=lambda e: e["updated_at"], reverse=True)
            return [{"name": e["name"], "type": e["type"], "description": e["description"][: M.KNOWN_DESCRIPTION]} for e in rows[:limit]]
        if sql == M._KNOWN_RELATIONS_SQL:
            scope, limit = args
            rows = sorted((r for r in R.values() if r["scope"] == scope),
                          key=lambda r: (r["status"] != "active", -r["updated_at"].timestamp()))
            return [{"source": E[r["source_id"]]["name"], "relation_type": r["relation_type"],
                     "target": E[r["target_id"]]["name"], "status": r["status"]} for r in rows[:limit]]
        if sql == M._EXISTING_SQL:
            scope, keys = args
            return [{"name_key": e["name_key"], "type": e["type"], "description": e["description"],
                     "embedded": e["embedding"] is not None}
                    for e in E.values() if e["scope"] == scope and e["name_key"] in keys]
        if sql in (M._NEAREST_SQL, M._SAME_LETTERS_SQL):
            scope, literal = args[0], args[1]
            rows = [e for e in E.values() if e["scope"] == scope and e["embedding"] is not None and e["name_key"] != M.ME]
            if sql == M._SAME_LETTERS_SQL:
                letters, noise, limit = args[2], args[3], args[4]
                rows = [e for e in rows if e["name_key"].translate({ord(c): None for c in noise}) == letters]
            else:
                limit = args[2]
                rows.sort(key=lambda e: -self._sim(e, literal))
            return [{"name": e["name"], "name_key": e["name_key"], "type": e["type"], "description": e["description"],
                     "similarity": self._sim(e, literal)} for e in rows[:limit]]
        if sql == M._UPSERT_ENTITY_SQL:
            scope, name, key, kind, description, literal = args
            e = self.by_key(scope, key)
            if e is None:
                new = self.entity(name, kind, description, scope=scope)
                E[new]["embedding"] = literal
                return [{"id": new, "name": name, "type": kind, "created": True}]
            if e["type"] == "":
                e["type"] = kind
            if literal is not None and (e["embedding"] is None or e["description"] == ""):
                e["embedding"] = literal
            if e["description"] == "":
                e["description"] = description
            e["updated_at"] = self.tick()
            return [{"id": e["id"], "name": e["name"], "type": e["type"], "created": False}]
        if sql == M._REDESCRIBE_SQL:
            scope, key, description, literal = args
            e = self.by_key(scope, key)
            e.update(description=description, embedding=literal, updated_at=self.tick())
            return []
        if sql == M._UPSERT_RELATION_SQL:
            scope, source, target, verb, status = args
            r = next((r for r in R.values() if (r["source_id"], r["target_id"], r["relation_type"]) == (source, target, verb)), None)
            if r is None:
                self.relation(source, verb, target, status, scope=scope)
                return [{"created": True, "weight": 1.0, "status": status, "before": None}]
            before = r["status"]
            r["weight"] += 1 if before == status else 0
            r.update(status=status, updated_at=self.tick())
            return [{"created": False, "weight": r["weight"], "status": status, "before": before}]
        if sql == M._MATCH_SQL:
            scope, literal, limit, terms, folded = args
            mine = [e for e in E.values() if e["scope"] == scope]
            nearest = sorted((e for e in mine if e["embedding"]), key=lambda e: -self._sim(e, literal))[:limit]
            named = [e for e in mine if e["name_key"] in terms][: M.MAX_NAME_MATCHES]
            chosen = {e["id"]: e for e in nearest + named}
            rows = [{"id": e["id"], "name": e["name"], "type": e["type"], "description": e["description"],
                     "similarity": self._sim(e, literal), "by_name": e in named} for e in chosen.values()]
            return sorted(rows, key=lambda h: (not h["by_name"], -(h["similarity"] or -2)))
        if sql == M._EDGES_SQL:
            scope, ids, limit = args
            rows = [r for r in R.values() if r["scope"] == scope and (r["source_id"] in ids or r["target_id"] in ids)]
            rows.sort(key=lambda r: (r["status"] != "active", -r["weight"], -r["updated_at"].timestamp()))
            return [self._edge(r) for r in rows[:limit]]
        raise AssertionError(f"a statement FakeKG does not know: {sql[:80]!r}")


class FakeConn:
    def __init__(self, kg: FakeKG) -> None:
        self.kg = kg

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return self.kg.run(sql, args)

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        rows = self.kg.run(sql, args)
        return rows[0] if rows else None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.kg.calls.append(sql)
        scope = args[0]
        if sql == M._COUNT_SQL:
            return sum(1 for e in self.kg.entities.values() if e["scope"] == scope)
        if sql == M._COUNT_RELATIONS_SQL:
            return sum(1 for r in self.kg.relations.values() if r["scope"] == scope)
        raise AssertionError(f"fetchval: {sql[:80]!r}")

    async def execute(self, sql: str, *args: Any) -> str:
        self.kg.run(sql, args)
        return "OK"


class KGCase(unittest.TestCase):
    """mcp_memory with FakeKG for its tables, a fixed extractor and fixed embeddings."""

    def setUp(self) -> None:
        self.kg = kg = FakeKG()
        self.vectors: dict[str, list[float]] = {}   # entity_text / query -> vector
        self.extracted: dict[str, Any] = {"entities": [], "relations": []}
        self.extract_args: list[Any] = []
        self.embedded: list[str] = []
        self.spare = 0

        @contextlib.asynccontextmanager
        async def transaction(*, readonly: bool = False, create: bool = True):
            yield FakeConn(kg)

        async def extract(text: str, known: list, facts: list | None = None) -> M.Graph:
            self.extract_args.append((text, known, facts))
            return M.graph_from(self.extracted)

        async def embed(texts: list[str], doing: str = "save it") -> list[str]:
            self.embedded.extend(texts)
            out = []
            for text in texts:
                vector = self.vectors.get(text) or self.vectors.get(text.casefold())
                if vector is None:
                    self.spare += 1
                    vector = unit(SPARE + self.spare % (DIMS - SPARE))
                out.append(M.vector_literal(vector))
            return out

        for patcher in (
            mock.patch.object(M, "_transaction", transaction),
            mock.patch.object(M, "extract", extract),
            mock.patch.object(M, "embed", embed),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def remember(self) -> M.ToolOutput:
        return asyncio.run(M._remember(SCOPE, "(the text the extractor is given)"))

    def recall(self, query: str, limit: int = 5) -> dict[str, Any]:
        return json.loads(asyncio.run(M._recall(SCOPE, query, limit)))


# =====================================================================
# 1. Temporal facts
# =====================================================================


class TemporalTests(KGCase):
    def test_the_sql_adds_the_columns_idempotently(self) -> None:
        with open(M.SQL_FILE, encoding="utf-8") as handle:
            sql = handle.read()
        for column in ("status", "created_at", "updated_at"):
            self.assertRegex(sql, rf"ALTER TABLE jarvis_memory\.relations ADD COLUMN IF NOT EXISTS {column}\b")
        self.assertRegex(sql, r"status\s+varchar\(16\) NOT NULL DEFAULT 'active'")
        self.assertIn("CHECK (status IN ('active', 'completed', 'archived'))", sql)
        self.assertIn("IF NOT EXISTS", sql.split("relations_status_known")[0][-400:])

    def test_an_older_database_is_brought_up_to_date(self) -> None:
        ran: list[str] = []

        class Conn:
            def __init__(self) -> None:
                self.states = [
                    {"ready": True, "current": False, "vector_schema": "public"},  # tables from before statuses
                    {"ready": True, "current": True, "vector_schema": "public"},
                ]

            async def fetchrow(self, sql: str) -> dict[str, Any]:
                return self.states.pop(0)

            async def execute(self, sql: str) -> None:
                ran.append(sql)

        path = asyncio.run(M.ensure_schema(Conn(), create=False))
        self.assertEqual(path, 'jarvis_memory, "public"')
        self.assertEqual(len(ran), 1)
        self.assertIn("ADD COLUMN IF NOT EXISTS status", ran[0])
        self.assertIn("column_name = 'status'", M._STATE_SQL)

    def test_statuses_from_the_extractor(self) -> None:
        graph = M.graph_from({
            "entities": [{"name": "Jarvis", "type": "project", "description": "x"}],
            "relations": [
                {"subject": "me", "relation": "building", "object": "Jarvis", "status": "completed"},
                {"subject": "me", "relation": "likes", "object": "Jarvis", "status": "nonsense"},
                {"subject": "me", "relation": "uses", "object": "Jarvis"},
            ],
        })
        self.assertEqual([edge[3] for edge in graph.edges], ["completed", "active", "active"])
        props = M.GRAPH_SCHEMA["properties"]["relations"]["items"]
        self.assertEqual(props["properties"]["status"]["enum"], ["active", "completed", "archived"])
        self.assertIn("status", props["required"])

    def test_a_fact_that_ended_keeps_its_row_with_its_new_status(self) -> None:
        kg = self.kg
        me = kg.entity("me", "person", M.ME_DESCRIPTION, unit(0))
        jarvis = kg.entity("Jarvis", "project", "the user's voice assistant", unit(1))
        old = kg.relation(me, "building", jarvis, weight=3)
        self.extracted = {
            "entities": [
                {"name": "me", "type": "person", "description": "the user"},
                {"name": "Jarvis", "type": "project", "description": "the voice assistant the user finished"},
                {"name": "TripPlanner", "type": "project", "description": "the app the user builds now"},
            ],
            "relations": [
                {"subject": "me", "relation": "building", "object": "Jarvis", "status": "completed"},
                {"subject": "me", "relation": "building", "object": "TripPlanner", "status": "active"},
            ],
        }
        out = self.remember()
        # the extractor was shown the fact, so it could mark it
        self.assertIn(("me", "building", "Jarvis", "active"), self.extract_args[0][2])
        self.assertIn(old, kg.relations)  # not deleted
        self.assertEqual(kg.relations[old]["status"], "completed")
        self.assertEqual(kg.relations[old]["weight"], 3, "a status change is not a retelling")
        self.assertIn(("me", "building", "TripPlanner", "active"), kg.facts())
        self.assertIn("me building Jarvis: now completed (was active)", out.text)
        event = out.structured["memory_event"]
        self.assertEqual((event["action"], event["facts"], event["entries"]), ("saved", 2, 1))
        self.assertIn("me building Jarvis (completed)", event["summary"])

    def test_retelling_counts_and_is_confirmed(self) -> None:
        kg = self.kg
        me = kg.entity("me", "person", M.ME_DESCRIPTION, unit(0))
        nok = kg.entity("Nok", "person", "the user's sister", unit(2))
        rel = kg.relation(nok, "sister_of", me)
        self.extracted = {
            "entities": [{"name": "Nok", "type": "person", "description": "the user's sister"}],
            "relations": [{"subject": "Nok", "relation": "sister_of", "object": "me", "status": "active"}],
        }
        out = self.remember()
        self.assertEqual(kg.relations[rel]["weight"], 2)
        self.assertEqual(out.structured["memory_event"]["action"], "confirmed")
        self.assertEqual(out.structured["memory_event"]["facts"], 0)

    def test_recall_shows_status_and_date(self) -> None:
        kg = self.kg
        me = kg.entity("me", "person", M.ME_DESCRIPTION, unit(0))
        jarvis = kg.entity("Jarvis", "project", "the user's voice assistant", unit(1))
        trip = kg.entity("TripPlanner", "project", "the app the user builds now", unit(2))
        kg.relation(me, "building", jarvis, status="completed")
        kg.relation(me, "building", trip)
        self.vectors["what am i building"] = unit(11)
        reply = self.recall("what am I building")
        by_object = {r["object"]: r for r in reply["relations"]}
        self.assertEqual(by_object["Jarvis"]["status"], "completed")
        self.assertEqual(by_object["TripPlanner"]["status"], "active")
        for relation in reply["relations"]:
            self.assertRegex(relation["updated_at"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(reply["relations"][0]["object"], "TripPlanner", "the fact that holds now comes first")


# =====================================================================
# 2. Deduplication
# =====================================================================


class DedupTests(KGCase):
    def row(self, name: str, kind: str, similarity: float) -> dict[str, Any]:
        return {"name": name, "name_key": M.key_of(name), "type": kind, "description": "", "similarity": similarity}

    def test_the_threshold_is_088(self) -> None:
        self.assertEqual(M.DEDUP_SIMILARITY, 0.88)
        node = M.Node("Postgres", "postgres", "technology", "a database")
        self.assertIsNotNone(M.merge_candidate(node, [self.row("PostgreSQL", "technology", 0.88)]))
        self.assertIsNotNone(M.merge_candidate(node, [self.row("PostgreSQL", "technology", 0.93)]))
        self.assertIsNone(M.merge_candidate(node, [self.row("PostgreSQL", "technology", 0.879)]))

    def test_types_must_be_compatible(self) -> None:
        node = M.Node("Jordan", "jordan", "person", "the user's friend")
        self.assertIsNone(M.merge_candidate(node, [self.row("Jordan", "place", 0.95)]))
        self.assertIsNotNone(M.merge_candidate(node, [self.row("Jordan", "other", 0.95)]))
        # old free-form types count as their group
        tech = M.Node("FastAPI", "fastapi", "technology", "x")
        self.assertIsNotNone(M.merge_candidate(tech, [self.row("Fast API", "framework", 0.9)]))
        self.assertTrue(M.types_compatible("City", "place"))
        self.assertFalse(M.types_compatible("company", "person"))

    def test_never_me(self) -> None:
        self.assertIsNone(M.merge_candidate(M.Node("me", "me", "person", ""), [self.row("Alice", "person", 0.99)]))
        self.assertIsNone(M.merge_candidate(M.Node("Alice", "alice", "person", ""), [self.row("me", "person", 0.99)]))

    def test_same_letters_same_type(self) -> None:
        node = M.Node("NextJS", "nextjs", "technology", "")
        self.assertIsNotNone(M.merge_candidate(node, [self.row("Next.js", "technology", 0.76)]))
        self.assertIsNone(M.merge_candidate(node, [self.row("Next.js", "project", 0.76)]))
        self.assertIsNone(M.merge_candidate(node, [self.row("Next.js", "technology", 0.55)]))
        self.assertEqual(M.name_letters("Chiang Mai"), M.name_letters("Chiangmai"))
        self.assertNotEqual(M.name_letters("Ken"), M.name_letters("Kent"))

    def test_fuller_description(self) -> None:
        self.assertEqual(M.fuller_description("", "the user's sister"), "the user's sister")
        self.assertEqual(
            M.fuller_description("the user's sister", "the user's sister, a nurse in Chiang Mai"),
            "the user's sister, a nurse in Chiang Mai",
        )
        self.assertIsNone(M.fuller_description("the city where the user's sister Nok lives", "Nok's city"))
        self.assertIsNone(M.fuller_description("The user's sister", "the user's  sister"))

    def test_remember_merges_a_near_duplicate(self) -> None:
        kg = self.kg
        me = kg.entity("me", "person", M.ME_DESCRIPTION, unit(0))
        pg = kg.entity("PostgreSQL", "technology", "a database", unit(3))
        self.vectors["Postgres (technology): the database Jarvis's memory lives in"] = near(3, 0.9, 4)
        self.extracted = {
            "entities": [
                {"name": "Postgres", "type": "technology", "description": "the database Jarvis's memory lives in"},
                {"name": "me", "type": "person", "description": "the user"},
            ],
            "relations": [{"subject": "me", "relation": "uses", "object": "Postgres", "status": "active"}],
        }
        out = self.remember()
        self.assertIsNone(kg.named("Postgres"), "no second node")
        self.assertIn(("me", "uses", "PostgreSQL", "active"), kg.facts())
        # it said more than "a database": the stored entry takes the fuller description, embedded again
        self.assertEqual(kg.entities[pg]["description"], "the database Jarvis's memory lives in")
        self.assertIn("PostgreSQL (technology): the database Jarvis's memory lives in", self.embedded)
        self.assertIn("Postgres = PostgreSQL", out.text)
        event = out.structured["memory_event"]
        self.assertEqual((event["merged"], event["entries"], event["facts"]), (1, 1, 1))
        self.assertTrue(me)

    def test_remember_keeps_a_distinct_entry_below_088(self) -> None:
        kg = self.kg
        kg.entity("me", "person", M.ME_DESCRIPTION, unit(0))
        kg.entity("OpenAI", "organization", "the AI company whose models Jarvis uses", unit(5))
        self.vectors["Anthropic (organization): the AI company whose models Jarvis uses"] = near(5, 0.857, 6)
        self.extracted = {
            "entities": [{"name": "Anthropic", "type": "organization", "description": "the AI company whose models Jarvis uses"}],
            "relations": [{"subject": "me", "relation": "uses_models_from", "object": "Anthropic", "status": "active"}],
        }
        out = self.remember()
        self.assertIsNotNone(kg.named("Anthropic"))
        self.assertIsNotNone(kg.named("OpenAI"))
        self.assertEqual(out.structured["memory_event"]["merged"], 0)

    def test_remember_keeps_another_type_apart(self) -> None:
        kg = self.kg
        kg.entity("me", "person", M.ME_DESCRIPTION, unit(0))
        kg.entity("Jordan", "place", "a country the user visited", unit(7))
        self.vectors["Jordan (person): the user's friend"] = near(7, 0.95, 8)
        self.extracted = {
            "entities": [{"name": "Jordan", "type": "person", "description": "the user's friend"}],
            "relations": [{"subject": "Jordan", "relation": "friend_of", "object": "me", "status": "active"}],
        }
        self.remember()
        # the exact name exists, so it is that entry (the name is the key) - never a merge into "me"
        self.assertEqual(len([e for e in kg.entities.values() if e["name_key"] == "jordan"]), 1)

    def test_same_letters_merge_in_remember(self) -> None:
        kg = self.kg
        kg.entity("me", "person", M.ME_DESCRIPTION, unit(0))
        kg.entity("Next.js", "technology", "the web framework TripPlanner uses", unit(9))
        self.vectors["NextJS (technology): a React framework"] = near(9, 0.76, 10)
        self.extracted = {
            "entities": [{"name": "NextJS", "type": "technology", "description": "a React framework"}],
            "relations": [{"subject": "me", "relation": "uses", "object": "NextJS", "status": "active"}],
        }
        self.remember()
        self.assertIsNone(kg.named("NextJS"))
        self.assertIn(("me", "uses", "Next.js", "active"), kg.facts())

    def test_two_new_names_for_one_entry(self) -> None:
        graph = M.Graph()
        graph.add("me", "person")
        graph.add("Postgres", "technology", "db")
        graph.add("Postgre SQL", "technology", "db")
        graph.edges = [("me", "uses", "postgres", "active"), ("me", "uses", "postgre sql", "active"),
                       ("postgres", "same_as", "postgre sql", "active")]
        into = M.Node("PostgreSQL", "postgresql", "technology", "a database")
        graph.merge("postgres", into)
        graph.merge("postgre sql", into)
        self.assertEqual(graph.edges, [("me", "uses", "postgresql", "active")], "loops dropped, duplicates kept once")
        self.assertEqual(set(graph.nodes), {"me", "postgresql"})


# =====================================================================
# 3. The fixed entity types
# =====================================================================


class TypeTests(unittest.TestCase):
    def test_the_six(self) -> None:
        self.assertEqual(M.ENTITY_TYPES, ("person", "project", "technology", "organization", "place", "other"))
        schema = M.GRAPH_SCHEMA["properties"]["entities"]["items"]["properties"]["type"]
        self.assertEqual(schema["enum"], list(M.ENTITY_TYPES))

    def test_the_prompt_names_them_and_puts_tools_under_technology(self) -> None:
        prompt = M.EXTRACTION_PROMPT
        self.assertIn("type: exactly one of person, project, technology, organization, place, other", prompt)
        for tool in ("FastAPI", "Render", "PostgreSQL"):
            self.assertRegex(prompt, rf"technology - [^\n]*{tool}")
        self.assertIn("never other", prompt)

    def test_normalising(self) -> None:
        cases = {
            "FRAMEWORK": "technology", "hosting": "technology", "database": "technology", "programming language": "technology",
            "City": "place", "country": "place", "company": "organization", "University": "organization",
            "app": "project", "family member": "person", "pet": "other", "food": "other", "": "other", None: "other",
        }
        for given, wanted in cases.items():
            self.assertEqual(M.normalize_type(given), wanted, given)

    def test_every_extracted_entry_gets_one_of_them(self) -> None:
        graph = M.graph_from({
            "entities": [
                {"name": "Render", "type": "platform", "description": "hosting"},
                {"name": "Bobo", "type": "pet", "description": "the user's dog"},
                {"name": "Tokyo", "type": "place", "description": "a city"},
            ],
            "relations": [{"subject": "me", "relation": "uses", "object": "Render", "status": "active"},
                          {"subject": "me", "relation": "visits", "object": "Somewhere", "status": "active"}],
        })
        types = {node.name: node.type for node in graph.nodes.values()}
        # "Somewhere" is only named in a relation: typed when it is saved (see the next test)
        self.assertEqual(types, {"Render": "technology", "Bobo": "other", "Tokyo": "place", "me": "person", "Somewhere": ""})


class SavedTypeTests(KGCase):
    def test_an_entry_named_only_in_a_relation(self) -> None:
        kg = self.kg
        kg.entity("me", "person", M.ME_DESCRIPTION, unit(0))
        kg.entity("Render", "technology", "where the API is hosted", unit(1))
        self.extracted = {
            "entities": [],  # the model reused the stored name without listing it, and named a new one
            "relations": [{"subject": "me", "relation": "uses", "object": "Render", "status": "active"},
                          {"subject": "me", "relation": "visits", "object": "Somewhere", "status": "active"}],
        }
        self.remember()
        self.assertEqual(kg.named("Render")["type"], "technology", "a stored entry keeps its type")
        self.assertEqual(kg.named("Somewhere")["type"], "other", "a new one gets other, never an empty type")


# =====================================================================
# 4. 2-hop recall
# =====================================================================


class TwoHopTests(KGCase):
    def build(self) -> dict[str, uuid.UUID]:
        kg = self.kg
        ids = {
            "me": kg.entity("me", "person", M.ME_DESCRIPTION, unit(0)),
            "TripPlanner": kg.entity("TripPlanner", "project", "the app the user builds", unit(1)),
            "Jarvis": kg.entity("Jarvis", "project", "the voice assistant the user finished", unit(2)),
            "FastAPI": kg.entity("FastAPI", "technology", "the framework TripPlanner is built with", unit(3)),
            "Render": kg.entity("Render", "technology", "where TripPlanner's API was hosted", unit(4)),
            "Fly.io": kg.entity("Fly.io", "technology", "where TripPlanner's API is hosted", unit(5)),
            "Python": kg.entity("Python", "technology", "the language FastAPI is written in", unit(6)),
            "Nok": kg.entity("Nok", "person", "the user's sister", unit(7)),
        }
        kg.relation(ids["me"], "building", ids["TripPlanner"], weight=3)
        kg.relation(ids["me"], "building", ids["Jarvis"], status="completed")
        kg.relation(ids["Nok"], "sister_of", ids["me"])
        kg.relation(ids["TripPlanner"], "built_with", ids["FastAPI"])
        kg.relation(ids["TripPlanner"], "hosted_on", ids["Render"], status="archived")
        kg.relation(ids["TripPlanner"], "hosted_on", ids["Fly.io"])
        kg.relation(ids["FastAPI"], "written_in", ids["Python"])   # three hops from me
        return ids

    def test_me_project_technology(self) -> None:
        self.build()
        self.vectors["what am i building"] = unit(11)
        reply = self.recall("what am I building")
        self.assertEqual([m["name"] for m in reply["matches"] if m.get("named_in_query")], ["me"])
        relations = {(r["subject"], r["relation"], r["object"]): r for r in reply["relations"]}
        self.assertIn(("me", "building", "TripPlanner"), relations)
        self.assertNotIn("hop", relations[("me", "building", "TripPlanner")])
        for tech in ("FastAPI", "Fly.io", "Render"):
            key = next(k for k in relations if k[2] == tech)
            self.assertEqual(relations[key]["hop"], 2, tech)
        self.assertEqual(relations[("TripPlanner", "hosted_on", "Render")]["status"], "archived")
        self.assertNotIn(("FastAPI", "written_in", "Python"), relations, "two hops, not three")
        connected = {c["name"] for c in reply["connected"]}
        self.assertTrue({"TripPlanner", "FastAPI", "Fly.io", "Render", "Nok", "Jarvis"} <= connected)
        self.assertNotIn("Python", connected)
        hops = [r.get("hop", 1) for r in reply["relations"]]
        self.assertEqual(hops, sorted(hops), "first-hop facts come first (and are the last to be cut)")

    def test_the_caps(self) -> None:
        kg = self.kg
        me = kg.entity("me", "person", M.ME_DESCRIPTION, unit(0))
        hubs = []
        for i in range(30):
            hub = kg.entity(f"Project {i}", "project", "a project", unit(1 + i % 10))
            kg.relation(me, "building", hub, weight=30 - i)
            hubs.append(hub)
        for i, hub in enumerate(hubs[:5]):
            for j in range(8):
                leaf = kg.entity(f"Tool {i}-{j}", "technology", "a tool", unit(1 + j))
                kg.relation(hub, "uses", leaf)
        self.vectors["what am i working on"] = unit(11)
        reply = self.recall("what am I working on")
        self.assertLessEqual(len(reply["relations"]), M.MAX_EDGES)
        self.assertLessEqual(len(reply["connected"]), M.MAX_CONNECTED)
        self.assertEqual(len(reply["connected"]), M.MAX_CONNECTED)
        self.assertTrue(reply.get("truncated"))
        # the strongest first: Project 0 is in, Project 29 is not
        names = {c["name"] for c in reply["connected"]}
        self.assertIn("Project 0", names)
        self.assertNotIn("Project 29", names)
        self.assertEqual(M.MAX_EDGES, 40)
        self.assertEqual(M.MAX_CONNECTED, 25)

    def test_walk_prefers_the_top_match_and_active_facts(self) -> None:
        t = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)

        def row(rid: str, s: str, tt: str, status: str = "active", weight: float = 1) -> dict[str, Any]:
            return {"id": rid, "source_id": s, "target_id": tt, "status": status, "weight": weight, "updated_at": t}

        rows = [
            row("r1", "B", "x", weight=9),            # a strong fact of the second match
            row("r2", "A", "y", status="archived"),   # an ended fact of the first match
            row("r3", "A", "z"),                       # a current fact of the first match
        ]
        edges: list[Any] = []
        connected: dict[Any, Any] = {}
        left_out = M.walk(rows, {"A": 0, "B": 1}, {"A", "B"}, edges, connected, max_edges=2, max_connected=5)
        self.assertEqual([e[0]["id"] for e in edges], ["r3", "r2"])
        self.assertTrue(left_out)
        self.assertEqual(list(connected), ["z", "y"])


# =====================================================================
# 5. Memory events: remember -> mcp_client -> the request -> /chat
# =====================================================================


class MemoryEventTests(unittest.TestCase):
    def test_remember_sends_its_event_as_structured_content(self) -> None:
        server = mcp_stdio.Server(
            name="t", version="0", instructions="", log_prefix="t",
            tools=[{"name": "remember"}],
            handlers={"remember": lambda args: mcp_stdio.ToolOutput("Remembered 1 new fact.", {"memory_event": {"action": "saved"}})},
        )
        result = server.call("remember", {})
        self.assertEqual(result["content"], [{"type": "text", "text": "Remembered 1 new fact."}])
        self.assertEqual(result["structuredContent"], {"memory_event": {"action": "saved"}})
        self.assertNotIn("isError", result)
        # the model reads only the text
        self.assertEqual(mcp_client.result_text(result), "Remembered 1 new fact.")

    def manager(self, call_tool: Any) -> mcp_client.McpManager:
        manager = mcp_client.McpManager([mcp_client.ServerConfig("memory", "python", [], user_scoped=True)])
        entry = manager._entries["memory"]

        class Conn:
            alive = True

            def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
                return call_tool(name, arguments)

        entry.conn = Conn()
        entry.originals = {"mcp__memory__remember": "remember", "mcp__memory__recall": "recall"}
        return manager

    def web(self, user: str = "u1") -> request_context.RequestContext:
        return request_context.RequestContext(channel=request_context.CHANNEL_WEB, user_id=user)

    def test_a_remember_is_recorded_for_the_request(self) -> None:
        seen: list[Any] = []

        def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            seen.append((name, arguments))
            return {
                "content": [{"type": "text", "text": "Remembered 2 new facts."}],
                "structuredContent": {"memory_event": {
                    "action": "saved", "entries": 2, "facts": 2, "merged": 0, "summary": "Nok sister of me",
                    "extra": "dropped",
                }},
            }

        manager = self.manager(call_tool)
        context = self.web()
        with request_context.use(context):
            text = manager.call("mcp__memory__remember", {"text": "My sister Nok", "_scope": "owner"})
            manager.call("mcp__memory__recall", {"query": "Nok"})
        self.assertIn("Remembered 2 new facts.", text)
        self.assertEqual(seen[0][1]["_scope"], "user:u1", "the model's _scope is overwritten")
        self.assertEqual(context.memory_events, [
            {"action": "saved", "entries": 2, "facts": 2, "merged": 0, "summary": "Nok sister of me"},
        ], "recall records nothing; unknown fields are dropped")

    def test_a_failed_remember_is_recorded_without_its_reason(self) -> None:
        def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": "Memory could not save it: the database refused the login 'memory_admin'."}],
                    "isError": True}

        manager = self.manager(call_tool)
        context = self.web()
        with request_context.use(context), self.assertRaises(mcp_client.McpToolError):
            manager.call("mcp__memory__remember", {"text": "x"})
        self.assertEqual(context.memory_events, [{"action": "failed", "entries": 0, "facts": 0, "merged": 0, "summary": ""}])

    def test_a_crashed_server_and_a_missing_user_count_as_failed(self) -> None:
        def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            raise mcp_client.McpError("timed out")

        manager = self.manager(call_tool)
        context = self.web()
        with request_context.use(context), self.assertRaises(mcp_client.McpError):
            manager.call("mcp__memory__remember", {"text": "x"})
        nobody = self.web(user="")
        with request_context.use(nobody), self.assertRaises(mcp_client.McpToolError):
            manager.call("mcp__memory__remember", {"text": "x"})
        self.assertEqual([e["action"] for e in context.memory_events + nobody.memory_events], ["failed", "failed"])

    def test_garbage_from_a_server_is_cleaned(self) -> None:
        def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": "ok"}],
                    "structuredContent": {"memory_event": {"action": "<b>", "entries": "9", "facts": -3, "summary": "x " * 400}}}

        context = self.web()
        with request_context.use(context):
            self.manager(call_tool).call("mcp__memory__remember", {"text": "x"})
        event = context.memory_events[0]
        self.assertEqual((event["action"], event["entries"], event["facts"]), ("saved", 0, 0))
        self.assertLessEqual(len(event["summary"]), 240)

    def chat(self, events: list[dict[str, Any]] | None) -> dict[str, Any]:
        class Brain:
            available = True
            tool_names: list[str] = []

            def set_history(self, turns: Any) -> None:
                pass

            async def run(self, question: str, **kwargs: Any):
                for event in events or []:
                    request_context.add_memory_event(event)
                yield {"type": "final", "node": "Final_Response", "message": "Got it - I'll remember that."}

        async def ready() -> Any:
            return Brain()

        with mock.patch.object(api_server, "_ready_brain", ready):
            response = TestClient(api_server.app).post("/chat", json={"prompt": "Remember: my sister is Nok", "user_id": "u1"})
        self.assertEqual(response.status_code, 200)
        finals = [json.loads(m.group(1)) for m in re.finditer(r"event: final\ndata: (.+)\n", response.text)]
        self.assertEqual(len(finals), 1)
        return finals[0]

    def test_chat_final_event_carries_the_memory_events(self) -> None:
        final = self.chat([{"action": "saved", "entries": 2, "facts": 1, "merged": 0, "summary": "Nok sister of me"}])
        self.assertEqual(final["message"], "Got it - I'll remember that.")
        self.assertEqual(final["memory_events"], [{"action": "saved", "entries": 2, "facts": 1, "merged": 0, "summary": "Nok sister of me"}])

    def test_a_got_it_without_a_save_has_no_events(self) -> None:
        final = self.chat(None)
        self.assertEqual(final["memory_events"], [])


if __name__ == "__main__":
    unittest.main()
