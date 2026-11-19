"""
Jarvis's long-term memory as a person sees it: the web hub's Memory page
(TripPlanner_Web /jarvis/memory) shows one person's knowledge graph and lets them
fix or delete what is in it. api_server.py serves it:

    GET    /memory/graph                 graph(scope)
    PATCH  /memory/entities/{id}         update_entity(scope, id, name=, type=, description=)
    DELETE /memory/entities/{id}         delete_entity(scope, id)
    PATCH  /memory/relations/{id}        update_relation(scope, id, status=)
    DELETE /memory/relations/{id}        delete_relation(scope, id)

The graph is the one mcp_memory.py keeps (tables jarvis_memory.entities and
jarvis_memory.relations), and this file reuses that module's database code
as it is: the same connection pool and database URL, the same retry on a
connection that died while idle, the same error wording. It only runs in
api_server.py's process, on its event loop; the memory tool server Jarvis
itself calls is a separate process with a pool of its own.

ONE SCOPE PER CALL. Every function takes the caller's scope ("owner", or
"user:<id>" - mcp_client.web_scope, from the signed-in person's id the web app
forwards), and every statement here filters on it, the id lookups included: an
entry or relation of another scope is "not found", exactly like one that does
not exist.

NOTHING IS CREATED. Unlike the tool server, this never creates memory's tables
(mcp_memory._transaction(create=False)): a database where nothing has been
remembered yet shows an empty graph, and an edit there finds nothing. Tables
made by an older version are still brought up to date (relation statuses).

FACTS HAVE A STATUS. A relation is active, completed or archived, with the date
it last changed (mcp_memory.sql); update_relation lets the person change it.

AN EDIT KEEPS SEARCH RIGHT. recall finds entries by the embedding of
"name (type): description" (mcp_memory.entity_text), so when an edit changes
any of the three, the entry is embedded again (text-embedding-3-small) and both
are saved together. If the embedding cannot be made, nothing is saved: an entry
whose words and embedding disagree would be found for the old words.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Optional

import mcp_memory
from mcp_memory import SCHEMA, ToolError

try:
    import asyncpg
except ImportError:  # pragma: no cover - requirements.txt has it
    asyncpg = None  # type: ignore[assignment]

_SCOPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,159}$")  # as mcp_stdio's


class GraphError(ToolError):
    """A refusal the page can show: `status` (HTTP), `code` (machine-readable), and the message."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _not_found(what: str = "entry") -> GraphError:
    return GraphError(404, "not_found", f"That memory {what} was not found.")


def _scope(scope: str) -> str:
    if not isinstance(scope, str) or not _SCOPE.match(scope):
        raise GraphError(401, "no_user", "Memory needs to know whose it is.")
    return scope


def _uuid(value: Any, what: str) -> uuid.UUID:
    """An id from the address: anything that is not a UUID is simply not found."""
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        raise _not_found(what) from None


def _usable() -> None:
    """The database is configured at all (the tool server says the same before any call)."""
    try:
        mcp_memory.db_settings()
    except ToolError as exc:
        raise GraphError(503, "not_configured", str(exc)) from None


async def _db(operation: Any, doing: str) -> Any:
    """mcp_memory._db (one retry on a dropped connection), with its failures as GraphErrors."""
    try:
        return await mcp_memory._db(operation, doing)
    except (GraphError, mcp_memory.NoMemoryYet):
        raise
    except ToolError as exc:
        raise GraphError(503, "unavailable", str(exc)) from None


async def close() -> None:
    """Closes the memory database pool (api_server.py at shutdown)."""
    await mcp_memory.close_pool()


def _node(row: Any) -> dict[str, Any]:
    return {"id": str(row["id"]), "name": row["name"], "type": row["type"], "description": row["description"]}


def _link(row: Any) -> dict[str, Any]:
    updated = row["updated_at"]
    return {
        "id": str(row["id"]),
        "source": str(row["source_id"]),
        "target": str(row["target_id"]),
        "relation_type": row["relation_type"],
        "weight": float(row["weight"]),
        # active (holds now), completed (done) or archived (no longer true) - mcp_memory.sql
        "status": row["status"],
        # When it was last told or changed, as an ISO 8601 timestamp.
        "updated_at": updated.isoformat() if hasattr(updated, "isoformat") else None,
    }


# =====================================================================
# The queries - every one filters on scope
# =====================================================================

_NODES_SQL = (
    f"SELECT id, name, type, description FROM {SCHEMA}.entities "
    f"WHERE scope = $1 ORDER BY name_key LIMIT {mcp_memory.MAX_ENTITIES}"
)
_LINKS_SQL = (
    f"SELECT id, source_id, target_id, relation_type, weight, status, updated_at FROM {SCHEMA}.relations "
    f"WHERE scope = $1 ORDER BY weight DESC, updated_at DESC LIMIT {mcp_memory.MAX_RELATIONS}"
)
_ENTITY_SQL = (
    f"SELECT id, name, name_key, type, description, embedding IS NOT NULL AS embedded "
    f"FROM {SCHEMA}.entities WHERE id = $1 AND scope = $2"
)
_LOCK_ENTITY_SQL = f"SELECT id FROM {SCHEMA}.entities WHERE id = $1 AND scope = $2 FOR UPDATE"
# $7 is the new embedding as text, or NULL to keep the one it has.
_UPDATE_ENTITY_SQL = f"""
UPDATE {SCHEMA}.entities
SET name = $3, name_key = $4, type = $5, description = $6,
    embedding = CASE WHEN $7::text IS NULL THEN embedding ELSE $7::text::vector END,
    updated_at = now()
WHERE id = $1 AND scope = $2
RETURNING id, name, type, description
"""
_DELETE_LINKS_OF_SQL = (
    f"DELETE FROM {SCHEMA}.relations WHERE scope = $2 AND (source_id = $1 OR target_id = $1) RETURNING id"
)
_DELETE_ENTITY_SQL = f"DELETE FROM {SCHEMA}.entities WHERE id = $1 AND scope = $2 RETURNING id"
_DELETE_RELATION_SQL = f"DELETE FROM {SCHEMA}.relations WHERE id = $1 AND scope = $2 RETURNING id"
_UPDATE_RELATION_SQL = f"""
UPDATE {SCHEMA}.relations SET status = $3, updated_at = now()
WHERE id = $1 AND scope = $2
RETURNING id, source_id, target_id, relation_type, weight, status, updated_at
"""

# Every statement above, for the test that each one is scoped.
STATEMENTS = (
    _NODES_SQL, _LINKS_SQL, _ENTITY_SQL, _LOCK_ENTITY_SQL, _UPDATE_ENTITY_SQL,
    _DELETE_LINKS_OF_SQL, _DELETE_ENTITY_SQL, _DELETE_RELATION_SQL, _UPDATE_RELATION_SQL,
)


# =====================================================================
# Reading
# =====================================================================


async def graph(scope: str) -> dict[str, Any]:
    """{"nodes": [{id, name, type, description}], "links": [{id, source, target, relation_type, weight}]}."""
    scope = _scope(scope)
    _usable()

    async def read() -> dict[str, Any]:
        async with mcp_memory._transaction(readonly=True, create=False) as conn:
            nodes = [_node(row) for row in await conn.fetch(_NODES_SQL, scope)]
            links = [_link(row) for row in await conn.fetch(_LINKS_SQL, scope)]
        # Both ends of every link are in `nodes` (the limits are the ones remember keeps to).
        known = {node["id"] for node in nodes}
        return {"nodes": nodes, "links": [l for l in links if l["source"] in known and l["target"] in known]}

    try:
        return await _db(read, "load the memory graph")
    except mcp_memory.NoMemoryYet:
        return {"nodes": [], "links": []}


# =====================================================================
# Editing an entry
# =====================================================================


def _clean_name(value: Any) -> str:
    try:
        return mcp_memory.entity_name(value, "name")
    except ToolError as exc:
        raise GraphError(400, "invalid", str(exc)) from None


def _clean_type(value: Any) -> str:
    """One of mcp_memory.ENTITY_TYPES - the same fixed set the extractor is held to."""
    kind = str(value or "").strip().lower()
    if kind not in mcp_memory.ENTITY_TYPES:
        raise GraphError(400, "invalid", f"'type' must be one of: {', '.join(mcp_memory.ENTITY_TYPES)}.")
    return kind


def _clean_description(value: Any) -> str:
    try:
        return mcp_memory._text(value, "description", mcp_memory.MAX_DESCRIPTION, required=False)
    except ToolError as exc:
        raise GraphError(400, "invalid", str(exc)) from None


async def update_entity(
    scope: str,
    entity_id: Any,
    *,
    name: Optional[str] = None,
    type: Optional[str] = None,  # noqa: A002 - the field's name in the API
    description: Optional[str] = None,
) -> dict[str, Any]:
    """
    Changes an entry's name, type and/or description (None = leave it). Returns
    {"node": {...}, "reembedded": bool}. The person's own entry, "me", keeps its name, and
    no other entry may take it; a name another entry of this scope has is refused (409).
    """
    scope = _scope(scope)
    key = _uuid(entity_id, "entry")
    if name is None and type is None and description is None:
        raise GraphError(400, "invalid", "Say what to change: name, type or description.")
    _usable()

    async def current() -> Any:
        async with mcp_memory._transaction(readonly=True, create=False) as conn:
            return await conn.fetchrow(_ENTITY_SQL, key, scope)

    try:
        row = await _db(current, "open the memory")
    except mcp_memory.NoMemoryYet:
        raise _not_found() from None
    if row is None:
        raise _not_found()

    new_name = row["name"] if name is None else _clean_name(name)
    new_type = row["type"] if type is None else _clean_type(type)
    new_description = row["description"] if description is None else _clean_description(description)
    new_key = mcp_memory.key_of(new_name)
    is_me = row["name_key"] == mcp_memory.ME
    if is_me and new_key != mcp_memory.ME:
        raise GraphError(400, "me_locked", "This entry is you (\"me\"): its name stays as it is.")
    if not is_me and new_key == mcp_memory.ME:
        raise GraphError(400, "reserved", "\"me\" is the name memory keeps for you; pick another name.")
    reason = mcp_memory.secret_reason(f"{new_name} {new_description}")
    if reason:
        raise GraphError(400, "refused", f"Not saved: {reason}.")

    before = mcp_memory.Node(row["name"], row["name_key"], row["type"], row["description"])
    after = mcp_memory.Node(new_name, new_key, new_type, new_description)
    if (new_name, new_type, new_description) == (row["name"], row["type"], row["description"]):
        return {"node": _node(row), "reembedded": False}

    vector: Optional[str] = None
    if mcp_memory.entity_text(after) != mcp_memory.entity_text(before) or not row["embedded"]:
        try:
            vector = (await mcp_memory.embed([mcp_memory.entity_text(after)], "update its search index"))[0]
        except ToolError as exc:
            raise GraphError(503, "embedding_failed", f"{exc} Nothing was changed.") from None

    async def write() -> Any:
        async with mcp_memory._transaction(create=False) as conn:
            try:
                return await conn.fetchrow(
                    _UPDATE_ENTITY_SQL, key, scope, new_name, new_key, new_type, new_description, vector
                )
            except asyncpg.exceptions.UniqueViolationError:
                raise GraphError(409, "name_taken", f"Memory already has an entry named {new_name}.") from None

    try:
        updated = await _db(write, "save the change")
    except mcp_memory.NoMemoryYet:
        raise _not_found() from None
    if updated is None:  # deleted meanwhile
        raise _not_found()
    return {"node": _node(updated), "reembedded": vector is not None}


# =====================================================================
# Deleting
# =====================================================================


async def delete_entity(scope: str, entity_id: Any) -> dict[str, Any]:
    """Deletes an entry and every relation it is in. {"deleted": id, "links": [the relations' ids]}."""
    scope = _scope(scope)
    key = _uuid(entity_id, "entry")
    _usable()

    async def remove() -> dict[str, Any]:
        async with mcp_memory._transaction(create=False) as conn:
            if await conn.fetchrow(_LOCK_ENTITY_SQL, key, scope) is None:
                raise _not_found()
            # Scoped here too, rather than left to the foreign key's cascade.
            links = [str(r["id"]) for r in await conn.fetch(_DELETE_LINKS_OF_SQL, key, scope)]
            if await conn.fetchrow(_DELETE_ENTITY_SQL, key, scope) is None:
                raise _not_found()
            return {"deleted": str(key), "links": links}

    try:
        return await _db(remove, "delete it")
    except mcp_memory.NoMemoryYet:
        raise _not_found() from None


async def update_relation(scope: str, relation_id: Any, *, status: Any) -> dict[str, Any]:
    """
    Sets a relation's status - active, completed or archived - as a person corrects it by hand
    ("I finished that project"); its date moves to now. {"link": {...}}.
    """
    scope = _scope(scope)
    key = _uuid(relation_id, "relation")
    wanted = str(status or "").strip().lower()
    if wanted not in mcp_memory.RELATION_STATUSES:
        raise GraphError(400, "invalid", f"'status' must be one of: {', '.join(mcp_memory.RELATION_STATUSES)}.")
    _usable()

    async def write() -> Any:
        async with mcp_memory._transaction(create=False) as conn:
            return await conn.fetchrow(_UPDATE_RELATION_SQL, key, scope, wanted)

    try:
        row = await _db(write, "save the change")
    except mcp_memory.NoMemoryYet:
        raise _not_found("relation") from None
    if row is None:
        raise _not_found("relation")
    return {"link": _link(row)}


async def delete_relation(scope: str, relation_id: Any) -> dict[str, Any]:
    """Deletes one relation; both entries stay (delete an entry on its own). {"deleted": id}."""
    scope = _scope(scope)
    key = _uuid(relation_id, "relation")
    _usable()

    async def remove() -> Any:
        async with mcp_memory._transaction(create=False) as conn:
            return await conn.fetchrow(_DELETE_RELATION_SQL, key, scope)

    try:
        row = await _db(remove, "delete it")
    except mcp_memory.NoMemoryYet:
        raise _not_found("relation") from None
    if row is None:
        raise _not_found("relation")
    return {"deleted": str(row["id"])}
