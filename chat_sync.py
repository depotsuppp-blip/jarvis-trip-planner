"""
The web hub's chat sessions, read and written from Python, so one conversation can be had from more
than one place: the owner's LINE chat with Jarvis (api_server.py, POST /webhooks/line) is kept as a
session of the web app, and shows in /jarvis like any other.

The tables are TripPlanner_Web's (prisma/schema.prisma, migration 20261009120000_add_chat_conversation_
message), which the web app owns - nothing here changes their shape:

    "Conversation" (id text PK, "userId" text -> "User".id, title text, "createdAt" timestamp(3),
                    "updatedAt" timestamp(3))      one session; the hub lists them newest "updatedAt" first
    "Message"      (id text PK, "conversationId" text -> "Conversation".id, role 'user' | 'jarvis',
                    content text, "createdAt" timestamp(3))

written the way the web app writes them (lib/chatTurns.ts): ids like Prisma's cuid() (Prisma makes
them, not the database), times in UTC without a zone (Prisma's DateTime), "updatedAt" set on every
message so the session is the hub's "latest" after LINE was used - GET /api/chat opens it.

THE LINE SESSION. One per owner, with a fixed id: line_conversation_id(JARVIS_OWNER_USER_ID), titled
"LINE". It is made on the first LINE message. The owner can open it in /jarvis and carry on there; a
LINE message then sees those web turns too (the last HISTORY_TURNS, like the web).

The database is memory's (mcp_memory.db_settings: JARVIS_MEMORY_DATABASE_URL, DIRECT_URL or
DATABASE_URL - the web app's Neon database). Its own small pool, apart from memory's, which pins its
connections to the jarvis_memory schema. A failure here never stops a LINE reply: the caller keeps a
short transcript in the process instead and the console says why (the error's class, never the URL).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
from typing import Any, Optional

HISTORY_TURNS = 20            # lib/chatTurns.ts HISTORY_TURNS: the earlier turns a turn is shown
LINE_TITLE = "LINE"
CONNECT_TIMEOUT_SECONDS = 10.0
QUERY_TIMEOUT_SECONDS = 15.0
MAX_ATTACHMENTS = 5

_CONVERSATION = 'public."Conversation"'
_MESSAGE = 'public."Message"'

# Times as Prisma writes a DateTime column without @db.Timestamptz: UTC, no zone.
_NOW = "(now() AT TIME ZONE 'UTC')"

ENSURE_SQL = f"""
INSERT INTO {_CONVERSATION} (id, "userId", title, "createdAt", "updatedAt")
VALUES ($1, $2, $3, {_NOW}, {_NOW})
ON CONFLICT (id) DO NOTHING
"""
OWNER_SQL = f'SELECT "userId" FROM {_CONVERSATION} WHERE id = $1'
HISTORY_SQL = f"""
SELECT role, content FROM {_MESSAGE}
WHERE "conversationId" = $1
ORDER BY "createdAt" DESC, id DESC
LIMIT $2
"""
ADD_SQL = f"""
INSERT INTO {_MESSAGE} (id, "conversationId", role, content, "createdAt")
VALUES ($1, $2, $3, $4, {_NOW})
"""
TOUCH_SQL = f'UPDATE {_CONVERSATION} SET "updatedAt" = {_NOW} WHERE id = $1'


def _log(message: str) -> None:
    print(f"[ChatSync] {message}", flush=True)


def new_id() -> str:
    """An id shaped like Prisma's cuid(): "c" and 24 lowercase characters, unique enough to be a key."""
    return "c" + secrets.token_hex(12)


def line_conversation_id(user_id: str) -> str:
    """The owner's LINE session: "line-<web user id>" (a hash of it when it holds anything unusual)."""
    user = user_id.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{1,100}", user):
        return f"line-{user}"
    return "line-h" + hashlib.sha256(user.encode("utf-8")).hexdigest()[:32]


def parse_stored_user_message(raw: str) -> tuple[str, list[dict[str, Any]]]:
    """
    A user message as the web app stores it: plain text, or a file message's JSON
    {"text": ..., "attachments": [{path, name, mime, size}]} - read as strictly as the web app reads it
    (lib/jarvis/attachments.ts parseStoredUserMessage: only those keys, paths in uploads/), else the
    text as it is.
    """
    plain: tuple[str, list[dict[str, Any]]] = (raw, [])
    trimmed = raw.strip()
    if not (trimmed.startswith("{") and trimmed.endswith("}")) or len(trimmed) > 20_000:
        return plain
    try:
        body = json.loads(trimmed)
    except ValueError:
        return plain
    if not isinstance(body, dict) or set(body) - {"text", "attachments"}:
        return plain
    text, files = body.get("text"), body.get("attachments")
    if not isinstance(text, str) or not isinstance(files, list) or not files or len(files) > MAX_ATTACHMENTS:
        return plain
    kept = []
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not item["path"].startswith("uploads/"):
            return plain
        size = item.get("size")
        kept.append(
            {
                "path": item["path"][:300],
                "name": str(item.get("name") or "")[:300],
                "mime": str(item.get("mime") or "")[:200],
                "size": size if isinstance(size, int) and size >= 0 else 0,
            }
        )
    return text, kept


class ChatStore:
    """The web app's sessions in its own database, for the brain's process (one pool, made on first use)."""

    def __init__(self) -> None:
        self._pool: Any = None
        self._lock: Optional[asyncio.Lock] = None

    async def _get_pool(self) -> Any:
        if self._pool is None:
            if self._lock is None:
                self._lock = asyncio.Lock()
            async with self._lock:
                if self._pool is None:
                    import asyncpg

                    import mcp_memory

                    settings = mcp_memory.db_settings()  # mcp_memory.ToolError when no database is set
                    self._pool = await asyncpg.create_pool(
                        dsn=settings.dsn,
                        ssl=settings.ssl,
                        min_size=0,
                        max_size=1,
                        max_inactive_connection_lifetime=60,  # Neon drops an idle compute's connections
                        statement_cache_size=0,               # safe behind PgBouncer
                        timeout=CONNECT_TIMEOUT_SECONDS,
                        command_timeout=QUERY_TIMEOUT_SECONDS,
                        server_settings={"application_name": "jarvis-chat-sync"},
                    )
        return self._pool

    async def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            try:
                await pool.close()
            except Exception:  # noqa: BLE001
                pass

    async def _run(self, doing: str, work: Any) -> Any:
        """`work(connection)` with one retry on a dropped connection; None (logged) when it fails."""
        for attempt in (1, 2):
            try:
                pool = await self._get_pool()
                async with pool.acquire() as connection:
                    return await work(connection)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - by class only: a driver message can quote the URL
                name = type(exc).__name__
                dropped = name in ("ConnectionDoesNotExistError", "InterfaceError", "InternalClientError", "ConnectionResetError")
                if attempt == 1 and dropped and self._pool is not None:
                    try:
                        await self._pool.expire_connections()
                    except Exception:  # noqa: BLE001
                        pass
                    continue
                _log(f"{doing} failed ({name})")
                return None
        return None

    async def line_conversation(self, user_id: str) -> Optional[str]:
        """The owner's LINE session, made the first time; None without a web user id or a database."""
        if not user_id.strip():
            return None
        conversation = line_conversation_id(user_id)

        async def work(connection: Any) -> Optional[str]:
            await connection.execute(ENSURE_SQL, conversation, user_id.strip(), LINE_TITLE)
            owner = await connection.fetchval(OWNER_SQL, conversation)
            return conversation if owner == user_id.strip() else None

        return await self._run("opening the LINE session", work)

    async def history(self, conversation: str, limit: int = HISTORY_TURNS) -> Optional[list[tuple[str, str]]]:
        """The session's last `limit` messages as (role, content), oldest first; None when unreadable."""

        async def work(connection: Any) -> list[tuple[str, str]]:
            rows = await connection.fetch(HISTORY_SQL, conversation, limit)
            return [(str(row["role"]), str(row["content"])) for row in reversed(rows)]

        return await self._run("reading the session", work)

    async def add(self, conversation: str, role: str, content: str) -> bool:
        """One message, and the session moved to the top of the hub's list - in one transaction."""

        async def work(connection: Any) -> bool:
            async with connection.transaction():
                await connection.execute(ADD_SQL, new_id(), conversation, role, content)
                await connection.execute(TOUCH_SQL, conversation)
            return True

        return bool(await self._run(f"saving a {role} message", work))
