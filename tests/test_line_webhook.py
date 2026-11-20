"""
Tests for Jarvis on LINE - the owner's LINE chat with Jarvis (api_server.py POST /webhooks/line,
line_channel.py, chat_sync.py, memory_ingest.ingest_text, system_topology.py's LINE node):

    .venv\\Scripts\\python -m pytest tests/test_line_webhook.py -v

* security - X-Line-Signature (HMAC-SHA256 of the raw body with the channel secret): missing 400,
  wrong 401, not configured 503; only the owner's own 1:1 chat is answered, everyone else is ignored
  with 200; a redelivered event is answered once; through a tunnel only the webhook path is reachable;
  the webhook-only listener serves nothing else
* text - the message goes through the same turn as the web hub (channel line, the owner's memory),
  with the LINE session's history; the message and the answer are kept in that session; LINE's
  loading animation, the reply, and the push when the reply token has expired
* replies - markdown out, tables as rows, widgets as their sentence, Jarvis's files as a note, long
  answers cut into bubbles of at most 5000 characters and 5 per call
* photos - fetched from LINE's content API, read by the vision model, asked about, and - for a
  document or whiteboard, or after "save this" - saved to memory as line_image_<timestamp>.jpg

Nothing reaches the network or a database: LINE's API is an httpx.MockTransport, the brain, the vision
model, memory and the web app's session tables are stand-ins.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime
import hashlib
import hmac
import json
import os
import socket
import sys
import unittest
from typing import Any, Optional
from unittest import mock

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for folder in (ROOT, HERE):
    if folder not in sys.path:
        sys.path.insert(0, folder)

from fastapi.testclient import TestClient  # noqa: E402

import api_server  # noqa: E402
import chat_sync  # noqa: E402
import line_channel  # noqa: E402
import mcp_client  # noqa: E402
import mcp_memory as M  # noqa: E402
import memory_ingest  # noqa: E402
import request_context  # noqa: E402
import system_topology  # noqa: E402

SECRET = "test-channel-secret"
OWNER_LINE = "U" + "a" * 32
STRANGER_LINE = "U" + "b" * 32
OWNER_WEB = "cownerweb000000000000000001"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200
ENV = {
    "LINE_CHANNEL_SECRET": SECRET,
    "LINE_CHANNEL_ACCESS_TOKEN": "test-access-token",
    "LINE_TARGET_USER_ID": "",
    "LINE_USER_ID": OWNER_LINE,
    "JARVIS_OWNER_USER_ID": OWNER_WEB,
    "JARVIS_LINE_PORT": "0",
    "JARVIS_ALLOW_PROXIED": "",
    "OPENAI_API_KEY": "sk-test",
}


def sign(body: bytes, secret: str = SECRET) -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


_event_number = 0


def event(message: dict[str, Any], user: str = OWNER_LINE, source_type: str = "user", **extra: Any) -> dict[str, Any]:
    global _event_number
    _event_number += 1
    source: dict[str, Any] = {"type": source_type, "userId": user}
    if source_type == "group":
        source["groupId"] = "C" + "c" * 32
    return {
        "type": "message",
        "mode": "active",
        "timestamp": 1763600000000,
        "source": source,
        "webhookEventId": f"01TESTEVENT{_event_number:06d}",
        "deliveryContext": {"isRedelivery": False},
        "replyToken": f"reply-token-{_event_number}",
        "message": message,
        **extra,
    }


def text(words: str, **extra: Any) -> dict[str, Any]:
    return event({"id": "1000001", "type": "text", "text": words}, **extra)


def image(**extra: Any) -> dict[str, Any]:
    return event({"id": "2000002", "type": "image", "contentProvider": {"type": "line"}}, **extra)


def delivery(*events: dict[str, Any]) -> bytes:
    return json.dumps({"destination": "U" + "d" * 32, "events": list(events)}).encode()


# ---- stand-ins ------------------------------------------------------------------------------


class FakeLine:
    """LINE's Messaging API as an httpx.MockTransport: records every call, answers as told."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str], Any]] = []
        self.reply_status = 200
        self.content = JPEG
        self.content_status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        body = json.loads(request.content) if request.content else None
        self.calls.append((url, dict(request.headers), body))
        if url == line_channel.LOADING_URL:
            return httpx.Response(202, json={})
        if url == line_channel.REPLY_URL:
            if self.reply_status != 200:
                return httpx.Response(self.reply_status, json={"message": "Invalid reply token"})
            return httpx.Response(200, json={"sentMessages": []})
        if url == line_channel.PUSH_URL:
            return httpx.Response(200, json={"sentMessages": []})
        if url.startswith(line_channel.LINE_DATA_API):
            return httpx.Response(self.content_status, content=self.content, headers={"Content-Type": "image/jpeg"})
        return httpx.Response(404, json={"message": "Not found"})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    def to(self, url: str) -> list[tuple[str, dict[str, str], Any]]:
        return [call for call in self.calls if call[0] == url]

    def sent_texts(self) -> list[str]:
        return [m["text"] for url, _, body in self.calls if url in (line_channel.REPLY_URL, line_channel.PUSH_URL) for m in body["messages"]]


class FakeStore:
    """The web app's Conversation / Message tables (chat_sync.ChatStore's surface)."""

    def __init__(self, rows: Optional[list[tuple[str, str]]] = None, broken: bool = False) -> None:
        self.rows: dict[str, list[tuple[str, str]]] = {}
        self.preset = rows or []
        self.broken = broken
        self.owners: list[str] = []

    async def line_conversation(self, user_id: str) -> Optional[str]:
        if self.broken:
            return None
        self.owners.append(user_id)
        conversation = chat_sync.line_conversation_id(user_id)
        self.rows.setdefault(conversation, list(self.preset))
        return conversation

    async def history(self, conversation: str, limit: int = chat_sync.HISTORY_TURNS) -> Optional[list[tuple[str, str]]]:
        return list(self.rows[conversation][-limit:])

    async def add(self, conversation: str, role: str, content: str) -> bool:
        self.rows[conversation].append((role, content))
        return True

    async def close(self) -> None:
        pass


class FakeBrain:
    """What a turn needs of LLMBrain; records what it was asked, by whom, with which history."""

    available = True
    tool_names = ["mcp__local_fs__read_file"]

    def __init__(self, reply: str = "Sure - noted.", memory_facts: int = 0) -> None:
        self.reply = reply
        self.memory_facts = memory_facts
        self.questions: list[str] = []
        self.histories: list[list[tuple[str, str]]] = []
        self.callers: list[Any] = []
        self.scopes: list[str] = []
        self.kwargs: list[dict[str, Any]] = []

    def set_history(self, turns: Any) -> None:
        if turns:
            self.histories.append(list(turns))

    async def run(self, question: str, **kwargs: Any):
        self.questions.append(question)
        self.kwargs.append(kwargs)
        self.callers.append(request_context.current())
        self.scopes.append(mcp_client.caller_scope())
        if self.memory_facts:
            request_context.add_memory_event({"action": "saved", "entries": 1, "facts": self.memory_facts, "merged": 0, "summary": "x"})
        yield {"type": "progress", "status": "Thinking..."}
        yield {"type": "final", "node": "Final_Response", "message": self.reply}


class FakeOpenAI:
    """openai.AsyncOpenAI's chat.completions.create, answering with one fixed photo reading."""

    def __init__(self, reading: dict[str, Any], fail: bool = False) -> None:
        self.reading = reading
        self.fail = fail
        self.requests: list[dict[str, Any]] = []
        self.chat = self
        self.completions = self

    async def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        if self.fail:
            raise RuntimeError("vision is down")
        message = type("Message", (), {"content": json.dumps(self.reading, ensure_ascii=False)})
        return type("Response", (), {"choices": [type("Choice", (), {"message": message})]})


WHITEBOARD = {
    "kind": "whiteboard",
    "title": "Sprint planning whiteboard",
    "summary": "Tasks for the Atlas launch.",
    "text": "Atlas launch: 2027-03-01\n- Nok: payments\n- Me: API",
}
HOLIDAY = {"kind": "photo", "title": "Beach at sunset", "summary": "A beach.", "text": ""}


class LineCase(unittest.TestCase):
    """The app with every outside part replaced; a delivery is posted and its background work runs."""

    def setUp(self) -> None:
        self.line = FakeLine()
        self.store = FakeStore()
        self.brain = FakeBrain()
        self.openai = FakeOpenAI(WHITEBOARD)
        self.saved: list[tuple[str, str, str]] = []
        self.save_result: Optional[dict[str, Any]] = {"source_file": "x", "count": 3, "facts": [], "ingested_at": "now"}
        line_channel._seen.clear()
        line_channel._last_text = (0.0, "")
        api_server._line_transcript.clear()

        async def ready() -> Any:
            return self.brain

        async def ingest_text(scope: str, source_file: str, words: str, **_: Any) -> Optional[dict[str, Any]]:
            self.saved.append((scope, source_file, words))
            return dict(self.save_result, source_file=source_file) if self.save_result else None

        for patcher in (
            mock.patch.dict(os.environ, ENV),
            mock.patch.object(api_server, "_ready_brain", ready),
            mock.patch.object(api_server, "_chat_store", self.store),
            mock.patch.object(api_server, "_line_http", self.line.client),
            mock.patch.object(M, "_openai_client", lambda: self.openai),
            mock.patch.object(memory_ingest, "ingest_text", ingest_text),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(api_server.app)

    def post(self, body: bytes, signature: Optional[str] = "sign", headers: Optional[dict[str, str]] = None, client: Any = None) -> httpx.Response:
        sent = dict(headers or {})
        if signature == "sign":
            sent["X-Line-Signature"] = sign(body)
        elif signature is not None:
            sent["X-Line-Signature"] = signature
        sent.setdefault("Content-Type", "application/json")
        return (client or self.client).post(line_channel.WEBHOOK_PATH, content=body, headers=sent)

    def session(self) -> list[tuple[str, str]]:
        return self.store.rows.get(chat_sync.line_conversation_id(OWNER_WEB), [])


# ---- security ---------------------------------------------------------------------------------


class SignatureTests(LineCase):
    def test_a_valid_signature_is_accepted_and_answered(self) -> None:
        response = self.post(delivery(text("hello")))
        self.assertEqual((response.status_code, response.json()), (200, {"ok": True}))
        self.assertEqual(self.brain.questions, ["hello"])

    def test_a_missing_signature_is_400_and_nothing_runs(self) -> None:
        response = self.post(delivery(text("hello")), signature=None)
        self.assertEqual((response.status_code, response.json()["code"]), (400, "no_signature"))
        self.assertEqual((self.brain.questions, self.line.calls), ([], []))

    def test_a_wrong_signature_is_401_and_nothing_runs(self) -> None:
        body = delivery(text("hello"))
        for bad in (sign(body, "another-secret"), sign(body + b" "), "not base64 at all", sign(body)[:-4] + "AAAA"):
            response = self.post(body, signature=bad)
            self.assertEqual((response.status_code, response.json()["code"]), (401, "bad_signature"), bad)
        self.assertEqual((self.brain.questions, self.line.calls), ([], []))

    def test_a_tampered_body_fails_the_signature(self) -> None:
        signed = delivery(text("hello"))
        tampered = signed.replace(b"hello", b"rm -rf")
        self.assertEqual(self.post(tampered, signature=sign(signed)).status_code, 401)

    def test_no_channel_secret_is_503(self) -> None:
        with mock.patch.dict(os.environ, {"LINE_CHANNEL_SECRET": ""}):
            response = self.post(delivery(text("hello")))
        self.assertEqual((response.status_code, response.json()["code"]), (503, "not_configured"))
        self.assertEqual(self.brain.questions, [])

    def test_a_signed_body_that_is_not_json_is_400(self) -> None:
        self.assertEqual(self.post(b"not json").json()["code"], "bad_body")

    def test_a_huge_body_is_413(self) -> None:
        body = b'{"events": [], "pad": "' + b"x" * (line_channel.MAX_BODY_BYTES + 10) + b'"}'
        self.assertEqual(self.post(body).status_code, 413)

    def test_line_console_verify_with_no_events_is_200(self) -> None:
        response = self.post(json.dumps({"destination": "U" + "d" * 32, "events": []}).encode())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.line.calls, [])

    def test_verify_signature_unit(self) -> None:
        body = b'{"events":[]}'
        self.assertTrue(line_channel.verify_signature(SECRET, body, sign(body)))
        self.assertTrue(line_channel.verify_signature(SECRET, body, " " + sign(body) + "\n"))
        self.assertFalse(line_channel.verify_signature("", body, sign(body, "")))
        self.assertFalse(line_channel.verify_signature(SECRET, body, ""))
        self.assertFalse(line_channel.verify_signature(SECRET, body, "ลายเซ็น"))


class OwnerOnlyTests(LineCase):
    def test_a_stranger_is_ignored_with_200(self) -> None:
        response = self.post(delivery(text("give me the owner's memory", user=STRANGER_LINE)))
        self.assertEqual(response.status_code, 200)
        self.assertEqual((self.brain.questions, self.line.calls, self.session()), ([], [], []))

    def test_a_group_or_room_is_ignored_even_from_the_owner(self) -> None:
        response = self.post(delivery(text("hi all", source_type="group"), text("hi room", source_type="room")))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.brain.questions, [])

    def test_only_the_owners_events_in_a_mixed_delivery_run(self) -> None:
        self.post(delivery(text("stranger", user=STRANGER_LINE), text("mine"), text("stranger 2", user=STRANGER_LINE)))
        self.assertEqual(self.brain.questions, ["mine"])

    def test_no_owner_configured_means_nobody_is_answered(self) -> None:
        with mock.patch.dict(os.environ, {"LINE_USER_ID": "", "LINE_TARGET_USER_ID": ""}):
            self.assertEqual(self.post(delivery(text("hello"))).status_code, 200)
        self.assertEqual(self.brain.questions, [])

    def test_the_target_name_wins_over_the_legacy_one(self) -> None:
        with mock.patch.dict(os.environ, {"LINE_TARGET_USER_ID": STRANGER_LINE}):
            self.post(delivery(text("from the old id")))
            self.post(delivery(text("from the new id", user=STRANGER_LINE)))
        self.assertEqual(self.brain.questions, ["from the new id"])

    def test_a_redelivered_event_is_answered_once(self) -> None:
        once = text("only once")
        self.post(delivery(once))
        again = dict(once, deliveryContext={"isRedelivery": True})
        self.post(delivery(again))
        self.assertEqual(self.brain.questions, ["only once"])

    def test_follow_and_standby_events_are_not_messages(self) -> None:
        follow = {"type": "follow", "mode": "active", "source": {"type": "user", "userId": OWNER_LINE}, "replyToken": "t", "webhookEventId": "F1"}
        standby = text("in standby", mode="standby")
        self.post(delivery(follow, standby))
        self.assertEqual(self.brain.questions, [])

    def test_no_access_token_means_no_answer_but_still_200(self) -> None:
        with mock.patch.dict(os.environ, {"LINE_CHANNEL_ACCESS_TOKEN": ""}):
            self.assertEqual(self.post(delivery(text("hello"))).status_code, 200)
        self.assertEqual((self.brain.questions, self.line.calls), ([], []))


class ExposureTests(LineCase):
    def test_through_a_tunnel_only_the_webhook_is_reachable(self) -> None:
        for header in ({"X-Forwarded-For": "203.0.113.9"}, {"CF-Connecting-IP": "203.0.113.9"}, {"Forwarded": "for=203.0.113.9"}, {"ngrok-skip-browser-warning": "1"}):
            self.assertEqual(self.client.get("/health", headers=header).status_code, 404, header)
            self.assertEqual(self.client.post("/chat", json={"prompt": "read my files"}, headers=header).status_code, 404, header)
            self.assertEqual(self.client.get("/memory/graph", headers={**header, "X-Jarvis-User-Id": OWNER_WEB}).status_code, 404)
        body = delivery(text("via the tunnel"))
        response = self.post(body, headers={"X-Forwarded-For": "203.0.113.9"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.brain.questions, ["via the tunnel"])

    def test_the_guard_can_be_lifted_on_purpose(self) -> None:
        with mock.patch.dict(os.environ, {"JARVIS_ALLOW_PROXIED": "1"}):
            response = self.client.post("/chat", json={"prompt": "hi"}, headers={"X-Forwarded-For": "10.0.0.2"})
        self.assertEqual(response.status_code, 200)

    def test_the_webhook_only_app_serves_nothing_else(self) -> None:
        only = TestClient(api_server.line_app)
        for path in ("/health", "/chat", "/memory/graph", "/openapi.json", "/docs", "/artifacts/x.png", "/system/topology"):
            self.assertEqual(only.get(path).status_code, 404, path)
        self.assertEqual(only.post("/chat", json={"prompt": "hi"}).status_code, 404)
        self.assertEqual(self.post(delivery(text("hi")), client=only).status_code, 200)
        self.assertEqual(self.brain.questions, ["hi"])

    def test_the_listener_is_a_real_loopback_port_with_only_the_webhook(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

        async def scenario() -> tuple[int, int, int]:
            with mock.patch.dict(os.environ, {"JARVIS_LINE_PORT": str(port)}):
                started = await api_server._start_line_listener()
            self.assertIsNotNone(started)
            try:
                async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
                    for _ in range(50):
                        try:
                            health = await http.get("/health")
                            break
                        except httpx.ConnectError:
                            await asyncio.sleep(0.05)
                    unsigned = await http.post(line_channel.WEBHOOK_PATH, content=b"{}")
                    forged = await http.post(line_channel.WEBHOOK_PATH, content=b"{}", headers={"X-Line-Signature": "AAAA"})
                return health.status_code, unsigned.status_code, forged.status_code
            finally:
                await api_server._stop_line_listener(*started)

        self.assertEqual(asyncio.run(scenario()), (404, 400, 401))

    def test_a_taken_port_is_reported_not_fatal(self) -> None:
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen()
            port = taken.getsockname()[1]
            with mock.patch.dict(os.environ, {"JARVIS_LINE_PORT": str(port)}), contextlib.redirect_stdout(open(os.devnull, "w")):
                self.assertIsNone(asyncio.run(api_server._start_line_listener()))

    def test_no_listener_without_a_secret_or_with_port_0(self) -> None:
        for env in ({"LINE_CHANNEL_SECRET": ""}, {"JARVIS_LINE_PORT": "0"}, {"JARVIS_LINE_PORT": "8000"}):
            with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(open(os.devnull, "w")):
                self.assertIsNone(asyncio.run(api_server._start_line_listener()), env)

    def test_settings_port(self) -> None:
        self.assertEqual(line_channel.settings({"JARVIS_LINE_PORT": ""}).port, 8001)
        self.assertEqual(line_channel.settings({"JARVIS_LINE_PORT": "9123"}).port, 9123)
        self.assertEqual(line_channel.settings({"JARVIS_LINE_PORT": "off"}).port, 0)
        self.assertEqual(line_channel.settings({"JARVIS_LINE_PORT": "99999"}).port, 8001)


# ---- text messages ----------------------------------------------------------------------------


class TextTurnTests(LineCase):
    def test_text_goes_through_the_brain_as_the_owner_on_line(self) -> None:
        self.post(delivery(text("What's on my calendar today?")))
        self.assertEqual(self.brain.questions, ["What's on my calendar today?"])
        caller = self.brain.callers[0]
        self.assertEqual((caller.channel, caller.user_id, caller.app_url), (request_context.CHANNEL_LINE, OWNER_WEB, ""))
        self.assertTrue(caller.is_web and caller.is_line)
        self.assertEqual(self.brain.scopes, ["owner"])  # the owner's memory
        self.assertIsNone(self.brain.kwargs[0]["emit"])  # no Approval Card on LINE: gated tools are declined

    def test_loading_animation_then_the_reply(self) -> None:
        self.post(delivery(text("hello")))
        urls = [call[0] for call in self.line.calls]
        self.assertEqual(urls[0], line_channel.LOADING_URL)
        self.assertEqual(urls[-1], line_channel.REPLY_URL)
        _, headers, body = self.line.to(line_channel.LOADING_URL)[0]
        self.assertEqual(body, {"chatId": OWNER_LINE, "loadingSeconds": 30})
        self.assertEqual(headers["authorization"], "Bearer test-access-token")
        _, _, reply = self.line.to(line_channel.REPLY_URL)[0]
        self.assertTrue(reply["replyToken"].startswith("reply-token-"))
        self.assertEqual(reply["messages"], [{"type": "text", "text": "Sure - noted."}])
        self.assertEqual(self.line.to(line_channel.PUSH_URL), [])

    def test_an_expired_reply_token_falls_back_to_push(self) -> None:
        self.line.reply_status = 400
        self.post(delivery(text("hello")))
        [(_, headers, push)] = self.line.to(line_channel.PUSH_URL)
        self.assertEqual(push["to"], OWNER_LINE)
        self.assertEqual(push["messages"], [{"type": "text", "text": "Sure - noted."}])
        self.assertTrue(headers.get("x-line-retry-key"))

    def test_the_turn_is_kept_in_the_shared_line_session(self) -> None:
        self.post(delivery(text("first")))
        self.brain.reply = "Second answer."
        self.post(delivery(text("second")))
        self.assertEqual(
            self.session(),
            [("user", "first"), ("jarvis", "Sure - noted."), ("user", "second"), ("jarvis", "Second answer.")],
        )
        self.assertEqual(self.store.owners[0], OWNER_WEB)
        # The second turn saw the first one, and not itself.
        self.assertEqual(self.brain.histories[-1], [("user", "first"), ("assistant", "Sure - noted.")])

    def test_web_turns_in_the_session_are_history_too(self) -> None:
        stored_file = json.dumps({"text": "what is this?", "attachments": [{"path": "uploads/0123456789abcdef-spec.pdf", "name": "spec.pdf", "mime": "application/pdf", "size": 10}]})
        self.store.preset = [("user", stored_file), ("jarvis", "It is the Atlas spec.")]
        self.post(delivery(text("and its deadline?")))
        history = self.brain.histories[-1]
        self.assertEqual(history[1], ("assistant", "It is the Atlas spec."))
        self.assertIn("what is this?", history[0][1])
        # The file is still pointed at (attachments.with_context; here it is not on disk, so by name).
        self.assertIn('"spec.pdf"', history[0][1])

    def test_without_the_session_database_a_short_transcript_is_kept_here(self) -> None:
        self.store.broken = True
        self.post(delivery(text("one")))
        self.post(delivery(text("two")))
        self.assertEqual(self.brain.histories[-1], [("user", "one"), ("assistant", "Sure - noted.")])

    def test_messages_in_one_delivery_are_answered_in_order(self) -> None:
        self.post(delivery(text("a"), text("b"), text("c")))
        self.assertEqual(self.brain.questions, ["a", "b", "c"])
        self.assertEqual(len(self.line.to(line_channel.REPLY_URL)), 3)

    def test_what_memory_saved_is_said_under_the_reply(self) -> None:
        self.brain.memory_facts = 2
        self.post(delivery(text("remember that Nok is my sister")))
        self.assertEqual(self.line.sent_texts(), ["Sure - noted.\n\n🧠 Saved 2 facts to memory"])

    def test_no_model_is_said_plainly(self) -> None:
        async def unready() -> Any:
            raise api_server.HTTPException(status_code=503, detail="no model")

        with mock.patch.object(api_server, "_ready_brain", unready):
            self.post(delivery(text("hello")))
        self.assertIn("no language model", self.line.sent_texts()[0])
        self.assertEqual(self.session(), [("user", "hello")])  # no reply kept

    def test_a_sticker_is_not_answered_and_a_voice_note_is_explained(self) -> None:
        sticker = event({"id": "3", "type": "sticker", "packageId": "1", "stickerId": "1"})
        voice = event({"id": "4", "type": "audio", "duration": 1000})
        self.post(delivery(sticker, voice))
        self.assertEqual(self.brain.questions, [])
        self.assertEqual(self.line.sent_texts(), [line_channel.UNSUPPORTED_REPLY])

    def test_a_long_answer_arrives_in_bubbles_of_at_most_5000(self) -> None:
        self.brain.reply = "\n\n".join(f"Paragraph {n}. " + "word " * 300 for n in range(8))
        self.post(delivery(text("tell me everything")))
        [(_, _, reply)] = self.line.to(line_channel.REPLY_URL)
        texts = [m["text"] for m in reply["messages"]]
        self.assertGreater(len(texts), 1)
        self.assertLessEqual(len(texts), 5)
        self.assertTrue(all(len(t) <= 5000 for t in texts))
        self.assertEqual(self.session()[-1], ("jarvis", self.brain.reply.strip()))  # the web keeps it whole


# ---- what a reply looks like on LINE ----------------------------------------------------------


class ReplyFormatTests(unittest.TestCase):
    def test_markdown_is_taken_out(self) -> None:
        reply = "## Today\n\n**Stand-up** at 9:00, then *lunch*.\n\n* item one\n+ item two\n> quoted\n\nUse `make build`.\n\n---\n```\ncode line\n```"
        self.assertEqual(
            line_channel.plain_text(reply),
            "Today\n\nStand-up at 9:00, then lunch.\n\n- item one\n- item two\nquoted\n\nUse make build.\n\ncode line",
        )

    def test_a_table_becomes_one_line_per_row(self) -> None:
        reply = "Your week:\n\n| Day | Plan |\n|-----|:----:|\n| Mon | Gym |\n| Tue | Atlas review |\n\nDone."
        self.assertEqual(line_channel.plain_text(reply), "Your week:\n\nDay · Plan\nMon · Gym\nTue · Atlas review\n\nDone.")

    def test_links_widgets_and_files(self) -> None:
        self.assertEqual(line_channel.plain_text("See [the trip](https://trip.example/t/1)."), "See the trip: https://trip.example/t/1.")
        self.assertEqual(line_channel.plain_text("[https://a.example](https://a.example)"), "https://a.example")
        image = line_channel.plain_text("Here it is:\n![Generated Image](/artifacts/image_20261120_101500.png)")
        self.assertIn("image_20261120_101500.png - saved in Jarvis's artifacts folder", image)
        deck = line_channel.plain_text("[Download Presentation](/artifacts/presentation_1.pptx)")
        self.assertTrue(deck.startswith("(Download Presentation: presentation_1.pptx"))
        widget = json.dumps({"text": "Systems look healthy.", "widgets": [{"tool": "system_status", "data": {}}]})
        self.assertEqual(line_channel.plain_text(widget), "Systems look healthy.")

    def test_chunking_limits(self) -> None:
        self.assertEqual(line_channel.line_texts("short"), ["short"])
        self.assertEqual(line_channel.line_texts(""), ["(Jarvis had nothing to say.)"])
        unbroken = "x" * 12_000
        pieces = line_channel.line_texts(unbroken)
        self.assertEqual([len(p) for p in pieces], [5000, 5000, 2000])
        huge = "\n\n".join("y" * 4000 for _ in range(9))
        pieces = line_channel.line_texts(huge)
        self.assertEqual(len(pieces), 5)
        self.assertTrue(pieces[-1].endswith(line_channel.MORE_IN_WEB))
        self.assertTrue(all(len(p) <= 5000 for p in pieces))
        self.assertEqual(line_channel.line_texts("hi", "🧠 Saved 1 fact to memory"), ["hi\n\n🧠 Saved 1 fact to memory"])

    def test_text_messages_never_send_empty_or_more_than_five(self) -> None:
        self.assertEqual(line_channel.text_messages(["a", "  ", "b"]), [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}])
        self.assertEqual(len(line_channel.text_messages([str(n) for n in range(9)])), 5)

    def test_memory_note(self) -> None:
        self.assertEqual(line_channel.memory_note(None), "")
        self.assertEqual(line_channel.memory_note({"count": 1, "source_file": "line_image_1.jpg"}), "🧠 Saved 1 fact to memory from line_image_1.jpg")
        self.assertEqual(line_channel.memory_note(None, [{"action": "confirmed", "facts": 0}, {"action": "failed"}]), "")


# ---- photos -----------------------------------------------------------------------------------


class PhotoTests(LineCase):
    def test_a_whiteboard_is_read_answered_and_saved_to_memory(self) -> None:
        self.post(delivery(image()))
        [(url, headers, _)] = [c for c in self.line.calls if c[0].startswith(line_channel.LINE_DATA_API)]
        self.assertEqual(url, "https://api-data.line.me/v2/bot/message/2000002/content")
        self.assertEqual(headers["authorization"], "Bearer test-access-token")
        # The vision model got the photo itself.
        request = self.openai.requests[0]
        self.assertEqual(request["model"], "gpt-4o-mini")
        self.assertTrue(request["response_format"]["json_schema"]["strict"])
        part = request["messages"][1]["content"][1]
        self.assertEqual(part["type"], "image_url")
        self.assertEqual(part["image_url"]["url"], "data:image/jpeg;base64," + base64.b64encode(JPEG).decode())
        # Memory got what was read, under the photo's name, in the owner's scope.
        [(scope, source_file, words)] = self.saved
        self.assertEqual(scope, "owner")
        self.assertRegex(source_file, r"^line_image_\d{8}_\d{6}\.jpg$")
        self.assertIn("Atlas launch: 2027-03-01", words)
        # The brain was asked about what it shows, and told not to save it twice.
        question = self.brain.questions[0]
        self.assertIn(source_file, question)
        self.assertIn("Atlas launch: 2027-03-01", question)
        self.assertIn("do not call the memory tool's remember", question)
        # The answer says what memory really took; the session shows the photo and what was read.
        self.assertTrue(self.line.sent_texts()[0].endswith(f"🧠 Saved 3 facts to memory from {source_file}"))
        user, reply = self.session()
        self.assertEqual(user[0], "user")
        self.assertTrue(user[1].startswith(f"📷 Photo from LINE ({source_file})"))
        self.assertIn("whiteboard: Sprint planning whiteboard", user[1])
        self.assertEqual(reply, ("jarvis", "Sure - noted."))

    def test_an_ordinary_photo_is_not_saved_unless_asked(self) -> None:
        self.openai.reading = HOLIDAY
        self.post(delivery(image()))
        self.assertEqual(self.saved, [])
        self.assertNotIn("remember", self.brain.questions[0])
        self.assertNotIn("🧠", self.line.sent_texts()[0])

    def test_save_this_before_the_photo_saves_it(self) -> None:
        self.openai.reading = HOLIDAY
        self.post(delivery(text("save this photo please")))
        self.post(delivery(image()))
        self.assertEqual(len(self.saved), 1)
        self.saved.clear()
        self.post(delivery(text("จำไว้นะ"), image()))
        self.assertEqual(len(self.saved), 1)

    def test_thai_words_that_only_contain_jam_do_not_count(self) -> None:
        for words in ("จำเป็นต้องไปไหม", "จำนวนเท่าไหร่", "what a nice view"):
            self.assertFalse(line_channel.wants_saving(words), words)
        for words in ("save it", "Remember this", "ช่วยจำด้วย", "บันทึกไว้", "เก็บไว้ให้หน่อย"):
            self.assertTrue(line_channel.wants_saving(words), words)

    def test_a_photo_that_cannot_be_downloaded_is_said_and_kept(self) -> None:
        self.line.content_status = 404
        self.post(delivery(image()))
        self.assertEqual(self.brain.questions, [])
        self.assertIn("I couldn't read that photo", self.line.sent_texts()[0])
        self.assertEqual(self.saved, [])
        self.assertIn("It could not be read", self.session()[0][1])

    def test_vision_failure_or_no_key_is_said(self) -> None:
        self.openai.fail = True
        self.post(delivery(image()))
        self.assertIn("vision model failed", self.line.sent_texts()[-1])

        def no_key() -> Any:
            raise M.ToolError("no key")

        with mock.patch.object(M, "_openai_client", no_key):
            self.post(delivery(image()))
        self.assertIn("OPENAI_API_KEY", self.line.sent_texts()[-1])
        self.assertEqual(self.brain.questions, [])

    def test_memory_saving_nothing_adds_no_note(self) -> None:
        self.save_result = None
        self.post(delivery(image()))
        self.assertEqual(len(self.saved), 1)
        self.assertNotIn("🧠", self.line.sent_texts()[0])

    def test_not_a_photo_is_refused_before_the_model(self) -> None:
        self.line.content = b"%PDF-1.7 not an image"
        self.post(delivery(image()))
        self.assertEqual(self.openai.requests, [])

    def test_reading_is_held_to_its_shape(self) -> None:
        reading = line_channel.parse_reading({"kind": "SELFIE", "title": "  a\n  b ", "summary": "s", "text": "t" * 20_000})
        self.assertEqual((reading.kind, reading.title, len(reading.text)), ("other", "a b", 12_000))
        with self.assertRaises(line_channel.LineError):
            line_channel.parse_reading(["not", "an", "object"])

    def test_image_names(self) -> None:
        when = datetime.datetime(2026, 11, 20, 9, 30, 15)
        self.assertEqual(line_channel.image_name(when), "line_image_20261120_093015.jpg")
        self.assertEqual(line_channel.image_name(when, "image/png"), "line_image_20261120_093015.png")


class IngestTextTests(unittest.TestCase):
    """memory_ingest.ingest_text: the same path as a document, from text."""

    def run_ingest(self, words: str, **patches: Any) -> tuple[Optional[dict[str, Any]], list[tuple]]:
        calls: list[tuple] = []

        async def save_text(scope: str, name: str, body: str, cut: bool, when: Any) -> dict[str, Any]:
            calls.append((scope, name, body, cut))
            return {"source_file": name, "count": 2, "facts": [], "ingested_at": when.isoformat()}

        with mock.patch.object(memory_ingest, "_save_text", patches.get("save_text", save_text)), mock.patch.object(
            M, "db_settings", patches.get("db_settings", lambda: None)
        ), mock.patch.object(M, "_openai_client", patches.get("openai", lambda: None)), contextlib.redirect_stdout(open(os.devnull, "w")):
            result = asyncio.run(memory_ingest.ingest_text("owner", "line_image_20261120_093015.jpg", words))
        return result, calls

    def test_saves_under_the_photos_name(self) -> None:
        result, calls = self.run_ingest("Atlas launch 2027-03-01")
        self.assertEqual(calls, [("owner", "line_image_20261120_093015.jpg", "Atlas launch 2027-03-01", False)])
        self.assertEqual((result["source_file"], result["count"]), ("line_image_20261120_093015.jpg", 2))

    def test_long_text_is_cut_like_a_document(self) -> None:
        _, calls = self.run_ingest("w" * (memory_ingest.MAX_DOCUMENT_CHARS + 50))
        self.assertTrue(calls[0][3])
        self.assertTrue(calls[0][2].endswith("[The rest of the document is not included here.]"))

    def test_never_raises(self) -> None:
        def not_set_up() -> None:
            raise M.ToolError("Memory is not set up")

        async def broken(*_: Any) -> None:
            raise RuntimeError("bug")

        self.assertEqual(self.run_ingest("x", db_settings=not_set_up), (None, []))
        self.assertIsNone(self.run_ingest("x", save_text=broken)[0])
        self.assertEqual(self.run_ingest("   "), (None, []))

    def test_ingest_document_still_reads_files_the_same_way(self) -> None:
        # The split kept ingest_document's order: config, read, then the shared _save_text.
        order: list[str] = []
        attachment = type("A", (), {"path": "uploads/0123456789abcdef-a.md", "name": "a.md"})()

        async def save_text(scope: str, name: str, body: str, cut: bool, when: Any) -> None:
            order.append(f"save:{name}:{body}")

        with mock.patch.object(M, "db_settings", lambda: order.append("config")), mock.patch.object(
            M, "_openai_client", lambda: None
        ), mock.patch.object(memory_ingest, "document_text", lambda a: (order.append("read") or ("body", False))), mock.patch.object(
            memory_ingest, "_save_text", save_text
        ):
            asyncio.run(memory_ingest.ingest_document("owner", attachment))
        self.assertEqual(order, ["config", "read", "save:a.md:body"])


# ---- the shared session tables (chat_sync.py) ---------------------------------------------------


class FakeConnection:
    def __init__(self, owner: str = OWNER_WEB, rows: Optional[list[dict[str, str]]] = None) -> None:
        self.owner = owner
        self.rows = rows or []
        self.statements: list[tuple[str, tuple]] = []
        self.transactions = 0

    async def execute(self, sql: str, *args: Any) -> str:
        self.statements.append((sql, args))
        return "OK"

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.statements.append((sql, args))
        return self.owner

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, str]]:
        self.statements.append((sql, args))
        return self.rows

    @contextlib.asynccontextmanager
    async def transaction(self) -> Any:
        self.transactions += 1
        yield


class FakePool:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    @contextlib.asynccontextmanager
    async def acquire(self) -> Any:
        yield self.connection

    async def close(self) -> None:
        pass


class ChatSyncTests(unittest.TestCase):
    def store(self, connection: FakeConnection) -> chat_sync.ChatStore:
        store = chat_sync.ChatStore()
        store._pool = FakePool(connection)
        return store

    def test_the_line_session_is_made_once_with_a_fixed_id(self) -> None:
        connection = FakeConnection()
        conversation = asyncio.run(self.store(connection).line_conversation(OWNER_WEB))
        self.assertEqual(conversation, f"line-{OWNER_WEB}")
        sql, args = connection.statements[0]
        self.assertIn('INSERT INTO public."Conversation"', sql)
        self.assertIn("ON CONFLICT (id) DO NOTHING", sql)
        self.assertIn("AT TIME ZONE 'UTC'", sql)
        self.assertEqual(args, (f"line-{OWNER_WEB}", OWNER_WEB, "LINE"))

    def test_a_session_id_owned_by_someone_else_is_not_used(self) -> None:
        self.assertIsNone(asyncio.run(self.store(FakeConnection(owner="cother")).line_conversation(OWNER_WEB)))

    def test_add_writes_the_message_and_moves_the_session_up_in_one_transaction(self) -> None:
        connection = FakeConnection()
        self.assertTrue(asyncio.run(self.store(connection).add("line-x", "user", "hello")))
        (insert, args), (touch, touch_args) = connection.statements
        self.assertIn('INSERT INTO public."Message"', insert)
        self.assertRegex(args[0], r"^c[0-9a-f]{24}$")
        self.assertEqual(args[1:], ("line-x", "user", "hello"))
        self.assertIn('UPDATE public."Conversation" SET "updatedAt"', touch)
        self.assertEqual((touch_args, connection.transactions), (("line-x",), 1))

    def test_history_is_oldest_first(self) -> None:
        rows = [{"role": "jarvis", "content": "2"}, {"role": "user", "content": "1"}]
        self.assertEqual(asyncio.run(self.store(FakeConnection(rows=rows)).history("line-x")), [("user", "1"), ("jarvis", "2")])

    def test_a_database_failure_is_none_not_an_exception(self) -> None:
        store = chat_sync.ChatStore()

        async def no_pool() -> Any:
            raise M.ToolError("Memory is not set up")

        store._get_pool = no_pool  # type: ignore[method-assign]
        with contextlib.redirect_stdout(open(os.devnull, "w")):
            self.assertIsNone(asyncio.run(store.line_conversation(OWNER_WEB)))
            self.assertIsNone(asyncio.run(store.history("line-x")))
            self.assertFalse(asyncio.run(store.add("line-x", "user", "hi")))

    def test_ids(self) -> None:
        self.assertEqual(chat_sync.line_conversation_id("cabc_123"), "line-cabc_123")
        self.assertRegex(chat_sync.line_conversation_id("weird id/with spaces"), r"^line-h[0-9a-f]{32}$")
        self.assertNotEqual(chat_sync.new_id(), chat_sync.new_id())

    def test_stored_file_messages_are_read_as_strictly_as_the_web_app_reads_them(self) -> None:
        good = json.dumps({"text": "hi", "attachments": [{"path": "uploads/a.pdf", "name": "a.pdf", "mime": "application/pdf", "size": 3}]})
        self.assertEqual(chat_sync.parse_stored_user_message(good)[1][0]["path"], "uploads/a.pdf")
        for raw in (
            json.dumps({"text": "hi", "attachments": [{"path": "/etc/passwd"}]}),
            json.dumps({"text": "hi", "attachments": [], "extra": 1}),
            json.dumps({"text": "hi", "attachments": []}),
            '{"text": "broken json"',
            "plain words",
        ):
            self.assertEqual(chat_sync.parse_stored_user_message(raw), (raw, []), raw)


# ---- the rest of Jarvis's view of LINE ----------------------------------------------------------


class ChannelTests(unittest.TestCase):
    def test_line_is_a_text_chat_without_cards_on_the_owners_memory(self) -> None:
        line = request_context.RequestContext(channel=request_context.CHANNEL_LINE, user_id="someone-else")
        with request_context.use(line):
            self.assertTrue(line.is_web and line.is_line)
            self.assertEqual(mcp_client.caller_scope(), "owner")
            self.assertFalse(request_context.add_widget({"tool": "system_status"}))
        web = request_context.RequestContext(channel=request_context.CHANNEL_WEB, user_id="someone-else")
        with request_context.use(web):
            self.assertFalse(web.is_line)
            self.assertEqual(mcp_client.caller_scope(), "user:someone-else")
            self.assertTrue(request_context.add_widget({"tool": "system_status"}))

    def test_owner_events_filter_unit(self) -> None:
        line_channel._seen.clear()
        payload = {"events": [text("a"), text("b", user=STRANGER_LINE), "junk", {"type": "message", "source": None}]}
        kept, others = line_channel.owner_events(payload, OWNER_LINE)
        # The stranger and the event with no source are both "not the owner's chat"; "junk" is no event at all.
        self.assertEqual(([e["message"]["text"] for e in kept], others), (["a"], 2))
        self.assertEqual(line_channel.owner_events({"events": "nope"}, OWNER_LINE), ([], 0))
        self.assertEqual(line_channel.owner_events(None, OWNER_LINE), ([], 0))


class TopologyTests(unittest.TestCase):
    def node(self, env: dict[str, str]) -> dict[str, Any]:
        with mock.patch.dict(os.environ, {**ENV, **env}):
            return system_topology._line_channel()

    def test_set_up(self) -> None:
        node = self.node({})
        self.assertEqual((node["id"], node["kind"], node["category"], node["status"]), ("channel:line", "core", "core", "online"))
        self.assertTrue(node["owner_only"])
        self.assertEqual([c["status"] for c in node["capabilities"]], ["online"] * 3)
        self.assertTrue(node["workflows"])

    def test_missing_settings_are_named_never_their_values(self) -> None:
        node = self.node({"LINE_CHANNEL_SECRET": "", "LINE_CHANNEL_ACCESS_TOKEN": ""})
        self.assertEqual((node["status"], node["status_note"]), ("offline", "Not set up"))
        self.assertIn("LINE_CHANNEL_SECRET, LINE_CHANNEL_ACCESS_TOKEN not set", node["diagnostics"][0])
        self.assertNotIn(OWNER_LINE, json.dumps(node))

    def test_no_owner_user_means_no_shared_session(self) -> None:
        node = self.node({"JARVIS_OWNER_USER_ID": ""})
        self.assertEqual(node["status"], "online")
        session = next(c for c in node["capabilities"] if c["name"] == "line_session")
        self.assertEqual(session["status"], "offline")
        self.assertTrue(any("JARVIS_OWNER_USER_ID" in d for d in node["diagnostics"]))


if __name__ == "__main__":
    unittest.main()
