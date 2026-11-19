"""
Tests for the Memory page's endpoints (api_server.py /memory/*, memory_graph.py).

    .venv\\Scripts\\python -m unittest discover -s tests -v

Two layers:

* MemoryApiTests (always run, no database, no OpenAI): the real FastAPI routes and the
  real memory_graph.py and mcp_memory._db (its retry), with mcp_memory's transaction
  replaced by an in-memory stand-in that executes each of memory_graph's statements the
  way its SQL is written (scope = $n included) and refuses any statement it does not
  know. Most of these are about scope isolation: one person can neither see nor change
  nor delete another person's graph, whatever they send.

* RealDatabaseTests (skipped unless JARVIS_MEMORY_TEST_DATABASE_URL is set): the same
  isolation against a real PostgreSQL with pgvector. They write rows under two random
  test scopes in jarvis_memory and delete them afterwards - point the variable at a
  local or throwaway database, never at production. Embeddings are faked (no OpenAI).
"""

from __future__ import annotations

import asyncio
import contextlib
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

import asyncpg  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import api_server  # noqa: E402
import mcp_client  # noqa: E402
import mcp_memory  # noqa: E402
import memory_graph as G  # noqa: E402

OWNER_ID = "owner-user-id-1"
ALICE = "alice-id"
BOB = "bob-id"
FAKE_VECTOR = "[" + ",".join(["0.01"] * mcp_memory.EMBEDDING_DIMENSIONS) + "]"


def headers(user: str) -> dict[str, str]:
    return {"X-Jarvis-User-Id": user}


# =====================================================================
# An in-memory stand-in for memory's two tables
# =====================================================================

_WRITES = {G._UPDATE_ENTITY_SQL, G._DELETE_LINKS_OF_SQL, G._DELETE_ENTITY_SQL, G._DELETE_RELATION_SQL}


class FakeStore:
    def __init__(self) -> None:
        self.entities: dict[uuid.UUID, dict[str, Any]] = {}
        self.relations: dict[uuid.UUID, dict[str, Any]] = {}
        self.ready = True                  # memory's tables exist
        self.calls: list[tuple[str, tuple]] = []
        self.transactions: list[dict[str, Any]] = []
        self.fail_next: list[BaseException] = []

    def entity(self, scope: str, name: str, kind: str = "", description: str = "") -> uuid.UUID:
        key = uuid.uuid4()
        self.entities[key] = {
            "id": key, "scope": scope, "name": name, "name_key": mcp_memory.key_of(name),
            "type": kind, "description": description, "embedding": "old",
        }
        return key

    def relation(self, scope: str, source: uuid.UUID, target: uuid.UUID, verb: str, weight: float = 1.0) -> uuid.UUID:
        key = uuid.uuid4()
        self.relations[key] = {
            "id": key, "scope": scope, "source_id": source, "target_id": target,
            "relation_type": verb, "weight": weight, "order": len(self.relations),
        }
        return key

    def of(self, scope: str) -> tuple[set[str], set[str]]:
        return (
            {e["name"] for e in self.entities.values() if e["scope"] == scope},
            {r["relation_type"] for r in self.relations.values() if r["scope"] == scope},
        )

    # ---- each statement, as its SQL says ---------------------------------

    def run(self, sql: str, args: tuple, readonly: bool) -> list[dict[str, Any]]:
        self.calls.append((sql, args))
        if self.fail_next:
            raise self.fail_next.pop(0)
        if readonly and sql in _WRITES:
            raise asyncpg.exceptions.ReadOnlySQLTransactionError("cannot execute in a read-only transaction")
        E, R = self.entities, self.relations

        def entity_row(e: dict[str, Any]) -> dict[str, Any]:
            return {**e, "embedded": e["embedding"] is not None}

        if sql == G._NODES_SQL:
            (scope,) = args
            return sorted((e for e in E.values() if e["scope"] == scope), key=lambda e: e["name_key"])
        if sql == G._LINKS_SQL:
            (scope,) = args
            return sorted((r for r in R.values() if r["scope"] == scope), key=lambda r: (-r["weight"], r["order"]))
        if sql in (G._ENTITY_SQL, G._LOCK_ENTITY_SQL):
            key, scope = args
            e = E.get(key)
            return [entity_row(e)] if e and e["scope"] == scope else []
        if sql == G._UPDATE_ENTITY_SQL:
            key, scope, name, name_key, kind, description, vector = args
            e = E.get(key)
            if not e or e["scope"] != scope:
                return []
            # UNIQUE (scope, name_key)
            if any(o["scope"] == scope and o["name_key"] == name_key and o["id"] != key for o in E.values()):
                raise asyncpg.exceptions.UniqueViolationError("duplicate key value violates unique constraint")
            e.update(name=name, name_key=name_key, type=kind, description=description)
            if vector is not None:
                e["embedding"] = vector
            return [e]
        if sql == G._DELETE_LINKS_OF_SQL:
            key, scope = args
            gone = [r for r in R.values() if r["scope"] == scope and key in (r["source_id"], r["target_id"])]
            for r in gone:
                del R[r["id"]]
            return [{"id": r["id"]} for r in gone]
        if sql == G._DELETE_ENTITY_SQL:
            key, scope = args
            e = E.get(key)
            if not e or e["scope"] != scope:
                return []
            del E[key]
            for r in [r for r in R.values() if key in (r["source_id"], r["target_id"])]:  # ON DELETE CASCADE
                del R[r["id"]]
            return [{"id": key}]
        if sql == G._DELETE_RELATION_SQL:
            key, scope = args
            r = R.get(key)
            if not r or r["scope"] != scope:
                return []
            del R[key]
            return [{"id": key}]
        raise AssertionError(f"a statement the stand-in does not know (add it, scoped): {sql!r}")


class FakeConn:
    def __init__(self, store: FakeStore, readonly: bool) -> None:
        self.store, self.readonly = store, readonly

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return self.store.run(sql, args, self.readonly)

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        rows = self.store.run(sql, args, self.readonly)
        return rows[0] if rows else None


class MemoryApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = store = FakeStore()
        self.embedded: list[str] = []

        @contextlib.asynccontextmanager
        async def transaction(*, readonly: bool = False, create: bool = True):
            store.transactions.append({"readonly": readonly, "create": create})
            if not store.ready:
                if create:
                    raise AssertionError("the Memory page must never create memory's tables")
                raise mcp_memory.NoMemoryYet("no tables")
            yield FakeConn(store, readonly)

        async def embed(texts: list[str], doing: str = "save it") -> list[str]:
            self.embedded.extend(texts)
            return [FAKE_VECTOR for _ in texts]

        for patcher in (
            mock.patch.object(mcp_memory, "_transaction", transaction),
            mock.patch.object(mcp_memory, "embed", embed),
            mock.patch.object(mcp_memory, "db_settings", lambda: object()),
            mock.patch.dict(os.environ, {"JARVIS_OWNER_USER_ID": OWNER_ID}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        # Not entered as a context manager: the app's start-up (the brain, speech) never runs.
        self.client = TestClient(api_server.app)

        s = store
        self.a_me = s.entity("user:" + ALICE, "me", "person", mcp_memory.ME_DESCRIPTION)
        self.a_nok = s.entity("user:" + ALICE, "Nok", "person", "the user's sister")
        self.a_cm = s.entity("user:" + ALICE, "Chiang Mai", "place", "the city where Nok lives")
        self.a_sister = s.relation("user:" + ALICE, self.a_nok, self.a_me, "sister_of", 2.0)
        self.a_lives = s.relation("user:" + ALICE, self.a_nok, self.a_cm, "lives_in")
        self.b_me = s.entity("user:" + BOB, "me", "person", mcp_memory.ME_DESCRIPTION)
        self.b_acme = s.entity("user:" + BOB, "Acme", "organization", "where the user works")
        self.b_works = s.relation("user:" + BOB, self.b_me, self.b_acme, "works_at")
        self.o_me = s.entity("owner", "me", "person", mcp_memory.ME_DESCRIPTION)
        self.o_jarvis = s.entity("owner", "Jarvis", "project", "the user's AI assistant project")
        self.o_builds = s.relation("owner", self.o_me, self.o_jarvis, "building")

    # ---- helpers ------------------------------------------------------------

    def graph(self, user: str) -> dict[str, Any]:
        response = self.client.get("/memory/graph", headers=headers(user))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def names(self, user: str) -> set[str]:
        return {node["name"] for node in self.graph(user)["nodes"]}

    def scopes_used(self) -> set[Any]:
        """Every scope argument the statements were given."""
        found = set()
        for sql, args in self.store.calls:
            position = int(re.search(r"scope = \$(\d)", sql).group(1)) - 1
            found.add(args[position])
        return found

    # ---- who is asking --------------------------------------------------------

    def test_every_endpoint_needs_the_user_header(self) -> None:
        some = str(self.a_nok)
        for method, path, body in (
            ("GET", "/memory/graph", None),
            ("PATCH", f"/memory/entities/{some}", {"name": "X"}),
            ("DELETE", f"/memory/entities/{some}", None),
            ("DELETE", f"/memory/relations/{self.a_sister}", None),
        ):
            for sent in ({}, headers(""), headers("   ")):
                response = self.client.request(method, path, json=body, headers=sent)
                self.assertEqual(response.status_code, 401, (method, path, sent))
                self.assertEqual(response.json()["code"], "no_user")
        self.assertEqual(self.store.calls, [], "nothing may reach the database without a user")
        self.assertIn("Nok", self.store.of("user:" + ALICE)[0])

    def test_scope_comes_from_the_header_only(self) -> None:
        # A query parameter or a body field naming another scope changes nothing.
        response = self.client.get("/memory/graph?user_id=bob-id&scope=owner&_scope=owner", headers=headers(ALICE))
        self.assertEqual({n["name"] for n in response.json()["nodes"]}, {"me", "Nok", "Chiang Mai"})
        response = self.client.patch(
            f"/memory/entities/{self.b_acme}", json={"name": "Hacked", "scope": "user:" + BOB}, headers=headers(ALICE)
        )
        self.assertEqual(response.status_code, 422, "unknown fields are refused, so a body cannot pick a scope")
        response = self.client.patch(f"/memory/entities/{self.b_acme}", json={"_scope": "user:bob-id"}, headers=headers(ALICE))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.scopes_used(), {"user:" + ALICE})
        self.assertIn("Acme", self.store.of("user:" + BOB)[0])

    def test_the_owner_sees_the_owner_memory(self) -> None:
        self.assertEqual(self.names(OWNER_ID), {"me", "Jarvis"})
        self.assertEqual(self.scopes_used(), {"owner"})
        self.assertEqual(mcp_client.web_scope(OWNER_ID), "owner")
        self.assertEqual(mcp_client.web_scope(ALICE), "user:" + ALICE)
        odd = mcp_client.web_scope("name with spaces/../")
        self.assertRegex(odd, r"^user:h-[0-9a-f]{32}$")

    # ---- reading ------------------------------------------------------------------

    def test_graph_shows_only_the_callers_nodes_and_links(self) -> None:
        a = self.graph(ALICE)
        self.assertEqual({n["name"] for n in a["nodes"]}, {"me", "Nok", "Chiang Mai"})
        self.assertEqual({l["id"] for l in a["links"]}, {str(self.a_sister), str(self.a_lives)})
        b = self.graph(BOB)
        self.assertEqual({n["name"] for n in b["nodes"]}, {"me", "Acme"})
        self.assertEqual({l["relation_type"] for l in b["links"]}, {"works_at"})
        node_ids = {n["id"] for n in a["nodes"]}
        for link in a["links"]:
            self.assertIn(link["source"], node_ids)
            self.assertIn(link["target"], node_ids)
        self.assertEqual(self.graph("nobody-id"), {"nodes": [], "links": []})

    def test_graph_shape(self) -> None:
        a = self.graph(ALICE)
        nok = next(n for n in a["nodes"] if n["name"] == "Nok")
        self.assertEqual(set(nok), {"id", "name", "type", "description"})
        self.assertEqual(nok, {"id": str(self.a_nok), "name": "Nok", "type": "person", "description": "the user's sister"})
        sister = next(l for l in a["links"] if l["relation_type"] == "sister_of")
        self.assertEqual(sister, {
            "id": str(self.a_sister), "source": str(self.a_nok), "target": str(self.a_me),
            "relation_type": "sister_of", "weight": 2.0,
        })

    def test_reads_are_read_only_and_nothing_is_ever_created(self) -> None:
        self.graph(ALICE)
        self.assertTrue(all(t["readonly"] for t in self.store.transactions))
        self.client.patch(f"/memory/entities/{self.a_nok}", json={"description": "older sister"}, headers=headers(ALICE))
        self.client.delete(f"/memory/relations/{self.a_lives}", headers=headers(ALICE))
        self.assertFalse(any(t["create"] for t in self.store.transactions))

    def test_no_tables_yet_is_an_empty_graph(self) -> None:
        self.store.ready = False
        self.assertEqual(self.graph(ALICE), {"nodes": [], "links": []})
        response = self.client.patch(f"/memory/entities/{uuid.uuid4()}", json={"name": "X"}, headers=headers(ALICE))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.client.delete(f"/memory/relations/{uuid.uuid4()}", headers=headers(ALICE)).status_code, 404)

    def test_not_configured_and_unreachable(self) -> None:
        def no_database() -> Any:
            raise mcp_memory.ToolError("Memory is not set up: it has no database.")

        with mock.patch.object(mcp_memory, "db_settings", no_database):
            response = self.client.get("/memory/graph", headers=headers(ALICE))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "not_configured")

        self.store.fail_next = [asyncpg.exceptions.InvalidPasswordError("password authentication failed")]
        response = self.client.get("/memory/graph", headers=headers(ALICE))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "unavailable")

    def test_a_dropped_connection_is_retried_once(self) -> None:
        # mcp_memory._db's recovery: a connection the server closed while idle gets one more try.
        self.store.fail_next = [asyncpg.exceptions.ConnectionDoesNotExistError("connection was closed")]
        self.assertEqual(self.names(ALICE), {"me", "Nok", "Chiang Mai"})

    # ---- isolation of changes ---------------------------------------------------------

    def test_another_persons_entry_cannot_be_edited(self) -> None:
        response = self.client.patch(f"/memory/entities/{self.a_nok}", json={"name": "Stolen"}, headers=headers(BOB))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "not_found")
        self.assertEqual(self.store.entities[self.a_nok]["name"], "Nok")
        self.assertEqual(self.embedded, [], "nothing is embedded for an entry that is not the caller's")

    def test_another_persons_entry_cannot_be_deleted(self) -> None:
        for user in (BOB, OWNER_ID):
            response = self.client.delete(f"/memory/entities/{self.a_nok}", headers=headers(user))
            self.assertEqual(response.status_code, 404, user)
        self.assertIn(self.a_nok, self.store.entities)
        self.assertIn(self.a_sister, self.store.relations)
        self.assertIn(self.a_lives, self.store.relations)

    def test_another_persons_relation_cannot_be_deleted(self) -> None:
        response = self.client.delete(f"/memory/relations/{self.a_sister}", headers=headers(BOB))
        self.assertEqual(response.status_code, 404)
        self.assertIn(self.a_sister, self.store.relations)
        # ... and the owner's id does not reach other people's memory either.
        self.assertEqual(self.client.delete(f"/memory/relations/{self.b_works}", headers=headers(OWNER_ID)).status_code, 404)
        self.assertIn(self.b_works, self.store.relations)

    def test_not_found_looks_the_same_as_someone_elses(self) -> None:
        theirs = self.client.delete(f"/memory/entities/{self.b_acme}", headers=headers(ALICE)).json()
        missing = self.client.delete(f"/memory/entities/{uuid.uuid4()}", headers=headers(ALICE)).json()
        self.assertEqual(theirs, missing)
        for bad in ("not-a-uuid", "1", "%20"):
            self.assertEqual(self.client.delete(f"/memory/entities/{bad}", headers=headers(ALICE)).status_code, 404)

    def test_every_statement_filters_on_scope(self) -> None:
        for sql in G.STATEMENTS:
            self.assertRegex(sql, r"WHERE[^;]*\bscope = \$\d", sql)

    # ---- editing -----------------------------------------------------------------------

    def test_rename_reembeds_and_shows_at_once(self) -> None:
        response = self.client.patch(f"/memory/entities/{self.a_nok}", json={"name": "Noknoi"}, headers=headers(ALICE))
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["reembedded"])
        self.assertEqual(body["node"], {"id": str(self.a_nok), "name": "Noknoi", "type": "person", "description": "the user's sister"})
        self.assertEqual(self.embedded, ["Noknoi (person): the user's sister"])
        stored = self.store.entities[self.a_nok]
        self.assertEqual((stored["name_key"], stored["embedding"]), ("noknoi", FAKE_VECTOR))
        self.assertIn("Noknoi", self.names(ALICE))

    def test_description_and_type_changes_reembed(self) -> None:
        self.client.patch(f"/memory/entities/{self.a_cm}", json={"description": "  Nok's  home city "}, headers=headers(ALICE))
        self.client.patch(f"/memory/entities/{self.a_cm}", json={"type": "City"}, headers=headers(ALICE))
        self.assertEqual(self.embedded, ["Chiang Mai (place): Nok's home city", "Chiang Mai (city): Nok's home city"])
        self.assertEqual(self.store.entities[self.a_cm]["type"], "city")

    def test_no_change_is_no_write(self) -> None:
        response = self.client.patch(
            f"/memory/entities/{self.a_nok}", json={"name": "Nok", "type": "person"}, headers=headers(ALICE)
        )
        self.assertEqual(response.json()["reembedded"], False)
        self.assertEqual(self.embedded, [])
        self.assertNotIn(G._UPDATE_ENTITY_SQL, [sql for sql, _ in self.store.calls])

    def test_a_case_only_rename_is_kept(self) -> None:
        response = self.client.patch(f"/memory/entities/{self.a_cm}", json={"name": "chiang mai"}, headers=headers(ALICE))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.store.entities[self.a_cm]["name"], "chiang mai")

    def test_name_clash_within_a_scope_only(self) -> None:
        response = self.client.patch(f"/memory/entities/{self.a_cm}", json={"name": " NOK "}, headers=headers(ALICE))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "name_taken")
        self.assertEqual(self.store.entities[self.a_cm]["name"], "Chiang Mai")
        # Bob's entries do not count: another person's "Nok" is no clash.
        response = self.client.patch(f"/memory/entities/{self.b_acme}", json={"name": "Nok"}, headers=headers(BOB))
        self.assertEqual(response.status_code, 200)

    def test_me_keeps_its_name_and_nobody_else_takes_it(self) -> None:
        response = self.client.patch(f"/memory/entities/{self.a_me}", json={"name": "Alice"}, headers=headers(ALICE))
        self.assertEqual((response.status_code, response.json()["code"]), (400, "me_locked"))
        for alias in ("me", "Me", "user", "myself"):
            response = self.client.patch(f"/memory/entities/{self.a_nok}", json={"name": alias}, headers=headers(ALICE))
            self.assertEqual((response.status_code, response.json()["code"]), (400, "reserved"), alias)
        response = self.client.patch(f"/memory/entities/{self.a_me}", json={"description": "Alice, a nurse"}, headers=headers(ALICE))
        self.assertEqual(response.status_code, 200)

    def test_bad_values_are_refused(self) -> None:
        cases = (
            ({}, "invalid"),
            ({"name": "   "}, "invalid"),
            ({"name": "x" * 121}, "invalid"),
            ({"type": "!!!"}, "invalid"),
            ({"description": "y" * 301}, "invalid"),
            ({"description": "her password is hunter22"}, "refused"),
            ({"description": "card 4111 1111 1111 1111"}, "refused"),
        )
        for body, code in cases:
            response = self.client.patch(f"/memory/entities/{self.a_nok}", json=body, headers=headers(ALICE))
            self.assertEqual((response.status_code, response.json().get("code")), (400, code), body)
        self.assertEqual(self.store.entities[self.a_nok]["name"], "Nok")
        self.assertEqual(self.embedded, [])

    def test_embedding_failure_saves_nothing(self) -> None:
        async def broken(texts: list[str], doing: str = "") -> list[str]:
            raise mcp_memory.ToolError("Memory could not update its search index: OpenAI could not be reached.")

        with mock.patch.object(mcp_memory, "embed", broken):
            response = self.client.patch(f"/memory/entities/{self.a_nok}", json={"name": "Noknoi"}, headers=headers(ALICE))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "embedding_failed")
        self.assertEqual(self.store.entities[self.a_nok]["name"], "Nok")

    # ---- deleting ------------------------------------------------------------------------

    def test_delete_entity_takes_its_relations_and_keeps_the_rest(self) -> None:
        response = self.client.delete(f"/memory/entities/{self.a_nok}", headers=headers(ALICE))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["deleted"], str(self.a_nok))
        self.assertEqual(set(body["links"]), {str(self.a_sister), str(self.a_lives)})
        self.assertEqual(self.store.of("user:" + ALICE), ({"me", "Chiang Mai"}, set()))
        self.assertEqual(self.store.of("user:" + BOB), ({"me", "Acme"}, {"works_at"}))
        self.assertEqual(self.client.delete(f"/memory/entities/{self.a_nok}", headers=headers(ALICE)).status_code, 404)

    def test_delete_relation_keeps_both_entries(self) -> None:
        response = self.client.delete(f"/memory/relations/{self.a_lives}", headers=headers(ALICE))
        self.assertEqual(response.json(), {"deleted": str(self.a_lives)})
        self.assertEqual(self.store.of("user:" + ALICE), ({"me", "Nok", "Chiang Mai"}, {"sister_of"}))


# =====================================================================
# The same against a real PostgreSQL + pgvector (opt-in)
# =====================================================================

TEST_URL = os.environ.get("JARVIS_MEMORY_TEST_DATABASE_URL", "").strip()


@unittest.skipUnless(TEST_URL, "set JARVIS_MEMORY_TEST_DATABASE_URL to a local/throwaway Postgres with pgvector")
class RealDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        run = uuid.uuid4().hex[:12]
        self.alice, self.bob = f"kgtest-a-{run}", f"kgtest-b-{run}"
        self.scopes = ("user:" + self.alice, "user:" + self.bob)
        for patcher in (
            mock.patch.dict(os.environ, {"JARVIS_MEMORY_DATABASE_URL": TEST_URL, "JARVIS_OWNER_USER_ID": OWNER_ID}),
            mock.patch.object(mcp_memory, "embed", self._embed),
            mock.patch.object(mcp_memory, "_pool", None),
            mock.patch.object(mcp_memory, "_pool_lock", None),
            mock.patch.object(mcp_memory, "_search_path", ""),
            mock.patch.object(mcp_memory, "_last_used", 0.0),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.embedded: list[str] = []
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.loop.run_until_complete(self._seed())
        self.addCleanup(lambda: self.loop.run_until_complete(self._clean()))

    async def _embed(self, texts: list[str], doing: str = "") -> list[str]:
        self.embedded.extend(texts)
        return [FAKE_VECTOR for _ in texts]

    async def _seed(self) -> None:
        conn = await mcp_memory._connect()
        try:
            await mcp_memory.ensure_schema(conn)  # creates the tables in the TEST database if missing
            self.ids: dict[str, uuid.UUID] = {}
            for scope, rows in ((self.scopes[0], ("me", "Nok", "Chiang Mai")), (self.scopes[1], ("me", "Acme"))):
                for name in rows:
                    self.ids[f"{scope}/{name}"] = await conn.fetchval(
                        f"INSERT INTO {mcp_memory.SCHEMA}.entities (scope, name, name_key, type, description) "
                        "VALUES ($1, $2, $3, 'thing', 'seeded') RETURNING id",
                        scope, name, mcp_memory.key_of(name),
                    )
            a, b = self.scopes
            for scope, source, verb, target in ((a, "Nok", "sister_of", "me"), (a, "Nok", "lives_in", "Chiang Mai"), (b, "me", "works_at", "Acme")):
                self.ids[f"{scope}/{verb}"] = await conn.fetchval(
                    f"INSERT INTO {mcp_memory.SCHEMA}.relations (scope, source_id, target_id, relation_type) "
                    "VALUES ($1, $2, $3, $4) RETURNING id",
                    scope, self.ids[f"{scope}/{source}"], self.ids[f"{scope}/{target}"], verb,
                )
        finally:
            await conn.close()

    async def _clean(self) -> None:
        await mcp_memory.close_pool()
        conn = await mcp_memory._connect()
        try:
            await conn.execute(f"DELETE FROM {mcp_memory.SCHEMA}.entities WHERE scope = ANY($1::text[])", list(self.scopes))
        finally:
            await conn.close()

    def call(self, coroutine: Any) -> Any:
        return self.loop.run_until_complete(coroutine)

    def test_isolation_on_a_real_database(self) -> None:
        a, b = self.scopes
        graph_a = self.call(G.graph(a))
        self.assertEqual({n["name"] for n in graph_a["nodes"]}, {"me", "Nok", "Chiang Mai"})
        self.assertEqual({l["relation_type"] for l in graph_a["links"]}, {"sister_of", "lives_in"})
        self.assertEqual({n["name"] for n in self.call(G.graph(b))["nodes"]}, {"me", "Acme"})

        nok = self.ids[f"{a}/Nok"]
        for attempt in (
            G.update_entity(b, nok, name="Stolen"),
            G.delete_entity(b, nok),
            G.delete_relation(b, self.ids[f"{a}/sister_of"]),
            G.delete_entity("owner", nok),
        ):
            with self.assertRaises(G.GraphError) as caught:
                self.call(attempt)
            self.assertEqual(caught.exception.status, 404)
        self.assertEqual(len(self.call(G.graph(a))["links"]), 2)
        self.assertEqual(self.embedded, [])

    def test_edits_and_deletes_on_a_real_database(self) -> None:
        a, b = self.scopes
        nok, chiang_mai = self.ids[f"{a}/Nok"], self.ids[f"{a}/Chiang Mai"]
        result = self.call(G.update_entity(a, nok, name="Noknoi", description="the user's younger sister"))
        self.assertTrue(result["reembedded"])
        self.assertEqual(self.embedded, ["Noknoi (thing): the user's younger sister"])
        with self.assertRaises(G.GraphError) as caught:
            self.call(G.update_entity(a, chiang_mai, name="noknoi"))
        self.assertEqual(caught.exception.status, 409)
        self.call(G.update_entity(b, self.ids[f"{b}/Acme"], name="Noknoi"))  # another scope: no clash

        removed = self.call(G.delete_relation(a, self.ids[f"{a}/lives_in"]))
        self.assertEqual(removed["deleted"], str(self.ids[f"{a}/lives_in"]))
        gone = self.call(G.delete_entity(a, nok))
        self.assertEqual(gone["links"], [str(self.ids[f"{a}/sister_of"])])
        graph_a = self.call(G.graph(a))
        self.assertEqual({n["name"] for n in graph_a["nodes"]}, {"me", "Chiang Mai"})
        self.assertEqual(graph_a["links"], [])
        self.assertEqual({l["relation_type"] for l in self.call(G.graph(b))["links"]}, {"works_at"})


if __name__ == "__main__":
    unittest.main()
