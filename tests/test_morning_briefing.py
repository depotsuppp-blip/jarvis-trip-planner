"""
Tests for the Morning Briefing (scripts/morning_briefing.py).

    pip install -r requirements-briefing.txt pytest
    python -m pytest tests/test_morning_briefing.py -v
    (or: python -m unittest tests.test_morning_briefing -v)

Nothing here reaches the network: feeds, article pages, Google, Postgres, OpenAI and LINE
are all stand-ins (a fake requests.Session, a fake OpenAI client, a fake DB connection).
The RSS tests need feedparser (requirements-briefing.txt) and are skipped without it, so
the repo's main test run in .venv keeps working.

What is checked:
- RSS / Atom parsing: the recency window, undated and non-http items dropped, HTML cleaned,
  Hacker News text dropped, full-text feeds, de-duplication publishers-first, one retry
  for a feed that timed out.
- URL integrity: the model can only answer with numbers; made-up numbers, URLs, bools are
  dropped; every link in the Flex message is a URL a feed gave; summary sentences with a
  link or an unverifiable number are removed.
- The Flex Message against LINE's limits (bubbles, bytes, altText, labels, URIs, colours,
  empty texts) - for normal and worst-case input - and validate_flex catching each break.
- Graceful degradation: no / broken / expired Google token, a token without the Calendar
  scope, Calendar errors, no DATABASE_URL, a failing DB, OpenAI down, LINE refusing the
  Flex message (plain-text fallback), and what reaches the logs (no token, no LINE user ID,
  no calendar or project lines under GitHub Actions).
"""

from __future__ import annotations

import contextlib
import email.utils
import importlib.util
import io
import json
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional
from unittest import mock
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "morning_briefing.py"

_spec = importlib.util.spec_from_file_location("morning_briefing", SCRIPT)
mb = importlib.util.module_from_spec(_spec)
sys.modules["morning_briefing"] = mb  # dataclasses look the module up while it loads
_spec.loader.exec_module(mb)

HAS_FEEDPARSER = importlib.util.find_spec("feedparser") is not None
needs_feedparser = unittest.skipUnless(HAS_FEEDPARSER, "feedparser is not installed (pip install -r requirements-briefing.txt)")

NOW = datetime(2026, 11, 20, 1, 0, tzinfo=timezone.utc)  # 08:00 in Bangkok
LONG_TEXT = (
    "Anthropic became a public company on Thursday and raised 81.3 billion dollars in its IPO. "
    "Each of the 7 founders sold about 3.4 million shares at the IPO price of 83 dollars. "
) * 8


# =====================================================================
# Stand-ins
# =====================================================================


class FakeResponse:
    def __init__(self, status: int = 200, json_data: Any = None, content: bytes = b"", headers: Optional[dict] = None, url: str = ""):
        self.status_code = status
        self._json = json_data
        self.content = content if content or json_data is None else json.dumps(json_data).encode()
        self.headers = headers or {}
        self.url = url

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def json(self) -> Any:
        if self._json is None:
            raise ValueError("no JSON")
        return self._json

    def iter_content(self, size: int):
        yield self.content

    def close(self) -> None:
        pass


class FakeSession:
    """requests.Session with routes: (method, url prefix) -> FakeResponse, or a callable(url, kwargs)."""

    def __init__(self, routes: Optional[dict] = None, default: Optional[FakeResponse] = None):
        self.routes = routes or {}
        self.default = default or FakeResponse(404)
        self.calls: list[tuple[str, str, dict]] = []
        self.headers: dict = {}

    def _answer(self, method: str, url: str, kwargs: dict) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        for (route_method, prefix), answer in self.routes.items():
            if route_method == method and url.startswith(prefix):
                if isinstance(answer, Exception):
                    raise answer
                return answer(url, kwargs) if callable(answer) else answer
        return self.default

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._answer("GET", url, kwargs)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._answer("POST", url, kwargs)


class FakeOpenAI:
    """client.chat.completions.create(...) answering by the JSON schema's name."""

    def __init__(self, selection: Any = None, summary: Any = None, fail: bool = False):
        self.selection = selection
        self.summary = summary
        self.fail = fail
        self.requests: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        if self.fail:
            raise RuntimeError("OpenAI is down")
        name = kwargs["response_format"]["json_schema"]["name"]
        payload = self.selection if name == "story_selection" else self.summary
        if callable(payload):
            payload = payload(kwargs)
        content = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class FakeCursor:
    def __init__(self, connection: "FakeConnection"):
        self.connection = connection

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        pass

    def execute(self, sql: str, params: Any = None) -> None:
        self.connection.statements.append((sql.strip(), params))

    def fetchall(self) -> list:
        return self.connection.rows


class FakeConnection:
    def __init__(self, rows: list):
        self.rows = rows
        self.statements: list[tuple[str, Any]] = []
        self.autocommit = False
        self.closed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def close(self) -> None:
        self.closed = True


def rfc822(moment: datetime) -> str:
    return email.utils.format_datetime(moment)


def rss(items: list[dict], title: str = "Feed") -> bytes:
    parts = []
    for item in items:
        pub = f"<pubDate>{rfc822(item['published'])}</pubDate>" if item.get("published") else ""
        source = f"<source url=\"https://x.example\">{escape(item['source'])}</source>" if item.get("source") else ""
        parts.append(
            f"<item><title>{escape(item['title'])}</title><link>{escape(item['link'])}</link>{pub}"
            f"<description>{escape(item.get('description', ''))}</description>{source}</item>"
        )
    return f"<?xml version='1.0'?><rss version='2.0'><channel><title>{title}</title>{''.join(parts)}</channel></rss>".encode()


def make_article(number: int, title: str, url: str, source: str = "TechCrunch", hours_ago: float = 2, description: str = "A short description.", kind: str = "publisher", feed_text: str = "") -> Any:
    return mb.Article(number, title, url, source, NOW - timedelta(hours=hours_ago), description, source, kind, feed_text or description)


def all_strings(node: Any):
    if isinstance(node, dict):
        for value in node.values():
            yield from all_strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from all_strings(value)
    elif isinstance(node, str):
        yield node


def all_uris(node: Any):
    if isinstance(node, dict):
        if node.get("type") == "uri":
            yield node["uri"]
        for value in node.values():
            yield from all_uris(value)
    elif isinstance(node, list):
        for value in node:
            yield from all_uris(value)


def story(number: int, title: str, url: str, sentences: Optional[list] = None, basis: str = "article") -> Any:
    return mb.Story(make_article(number, title, url), sentences or ["ประโยคที่หนึ่ง", "ประโยคที่สอง", "ประโยคที่สาม", "ประโยคที่สี่"], basis)


def context(events: Optional[list] = None, projects: Optional[list] = None, calendar_status: str = "ok") -> Any:
    return mb.DailyContext(
        mb.Section(calendar_status, "", events or []),
        mb.Section("ok" if projects else "skipped", "", projects or []),
    )


# =====================================================================
# Step 1 - RSS parsing and de-duplication
# =====================================================================


@needs_feedparser
class RssParsingTests(unittest.TestCase):
    def test_keeps_only_recent_dated_http_items_and_cleans_html(self):
        feed = mb.Feed("TechCrunch", "https://techcrunch.example/feed")
        content = rss(
            [
                {"title": "Fresh &amp; real", "link": "https://tc.example/a?utm_source=rss", "published": NOW - timedelta(hours=3), "description": "<p>Big <b>news</b> &amp; more</p>"},
                {"title": "Too old", "link": "https://tc.example/old", "published": NOW - timedelta(hours=60)},
                {"title": "No date", "link": "https://tc.example/undated"},
                {"title": "Bad link", "link": "javascript:alert(1)", "published": NOW - timedelta(hours=1)},
                {"title": "From the future", "link": "https://tc.example/future", "published": NOW + timedelta(days=3)},
            ]
        )
        items = mb.parse_feed(content, feed, NOW)
        self.assertEqual([a.title for a in items], ["Fresh & real"])
        self.assertEqual(items[0].url, "https://tc.example/a?utm_source=rss")  # kept exactly as the feed gave it
        self.assertEqual(items[0].description, "Big news & more")
        self.assertEqual(items[0].source, "TechCrunch")

    def test_aggregator_and_headline_only_text_is_dropped(self):
        hn = mb.Feed("Hacker News", "https://hn.example/rss", kind="hn")
        [item] = mb.parse_feed(rss([{"title": "Show HN: a tool", "link": "https://tool.example/", "published": NOW - timedelta(hours=2), "description": "Article URL: https://tool.example/ Points: 120"}]), hn, NOW)
        self.assertEqual((item.description, item.feed_text), ("", ""))
        publisher = mb.Feed("Example", "https://ex.example/rss")
        [item] = mb.parse_feed(rss([{"title": "OpenAI ships a model", "link": "https://ex.example/a", "published": NOW - timedelta(hours=2), "description": "<a>OpenAI ships a model</a>"}]), publisher, NOW)
        self.assertEqual(item.description, "")  # only the headline again: nothing to summarise from

    def test_full_text_feed_is_kept_whole(self):
        feed = mb.Feed("Blognone", "https://blognone.example/atom.xml")
        body = "เนื้อข่าว " * 400
        content = rss([{"title": "ข่าวไทย", "link": "https://blognone.example/node/1", "published": NOW - timedelta(hours=1), "description": f"<p>{body}</p>"}])
        [item] = mb.parse_feed(content, feed, NOW)
        self.assertGreater(len(item.feed_text), mb.FEED_FULL_TEXT_CHARS)
        self.assertLessEqual(len(item.description), 600)

    def test_dedupe_keeps_the_publisher_and_numbers_newest_first(self):
        publisher = mb.Feed("TechCrunch", "https://tc.example/feed")
        other = mb.Feed("The Verge", "https://verge.example/rss")
        hn = mb.Feed("Hacker News", "https://hn.example/rss", kind="hn")
        session = FakeSession(
            {
                ("GET", publisher.url): FakeResponse(200, content=rss([
                    {"title": "Nvidia unveils a chip", "link": "https://tc.example/nvidia/", "published": NOW - timedelta(hours=5), "description": "Chips."},
                    {"title": "Older story", "link": "https://tc.example/older", "published": NOW - timedelta(hours=20), "description": "Old."},
                ])),
                ("GET", other.url): FakeResponse(200, content=rss([
                    {"title": "Nvidia Unveils a Chip!", "link": "https://verge.example/nvidia-chip", "published": NOW - timedelta(hours=4), "description": "Same story."},
                ])),
                ("GET", hn.url): FakeResponse(200, content=rss([
                    {"title": "Same link via HN", "link": "https://www.tc.example/nvidia?utm_medium=hn#top", "published": NOW - timedelta(hours=1)},
                    {"title": "A Show HN", "link": "https://show.example/", "published": NOW - timedelta(hours=1)},
                ])),
            }
        )
        articles, stats = mb.collect_articles(session, (publisher, other, hn), NOW)
        self.assertEqual([a.title for a in articles], ["A Show HN", "Nvidia unveils a chip", "Older story"])
        self.assertEqual([a.id for a in articles], [1, 2, 3])
        self.assertEqual(articles[1].url, "https://tc.example/nvidia/")
        self.assertEqual(stats["feeds_ok"], 3)

    def test_a_dead_or_broken_feed_is_counted_not_fatal(self):
        good = mb.Feed("Good", "https://good.example/feed")
        session = FakeSession(
            {
                ("GET", good.url): FakeResponse(200, content=rss([{"title": "Story", "link": "https://good.example/s", "published": NOW - timedelta(hours=1)}])),
                ("GET", "https://dead.example"): ConnectionError("down"),
            },
            default=FakeResponse(429),
        )
        feeds = (good, mb.Feed("Dead", "https://dead.example/feed"), mb.Feed("Limited", "https://limited.example/feed"))
        articles, stats = mb.collect_articles(session, feeds, NOW)
        self.assertEqual(len(articles), 1)
        self.assertEqual((stats["feeds_ok"], stats["feeds_failed"]), (1, 2))

    def test_a_timed_out_feed_is_tried_once_more(self):
        feed = mb.Feed("Slow", "https://slow.example/feed")
        answers = iter([mb.requests.exceptions.ReadTimeout("slow"), FakeResponse(200, content=b"<rss/>")])

        def answer(url: str, kwargs: dict) -> FakeResponse:
            value = next(answers)
            if isinstance(value, Exception):
                raise value
            return value

        session = FakeSession({("GET", feed.url): answer})
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(mb.fetch_feed(session, feed), b"<rss/>")
        self.assertEqual(len(session.calls), 2)
        down = FakeSession({("GET", feed.url): mb.requests.exceptions.ConnectTimeout("down")})
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(mb.fetch_feed(down, feed))
        self.assertEqual(len(down.calls), 2)


class UrlHelperTests(unittest.TestCase):
    def test_canonical_url_ignores_tracking_and_cosmetics(self):
        a = mb.canonical_url("https://www.Example.com/post/?utm_source=x&id=7&fbclid=1#comments")
        b = mb.canonical_url("http://example.com/post?id=7")
        self.assertEqual(a, b)

    def test_is_safe_url(self):
        self.assertTrue(mb.is_safe_url("https://example.com/a"))
        for bad in ("javascript:alert(1)", "ftp://example.com/a", "https://", "https://exa mple.com", "https://e.com/" + "a" * 1000, ""):
            self.assertFalse(mb.is_safe_url(bad), bad)


# =====================================================================
# Step 2/3 - selection and summaries: no URL ever comes from the model
# =====================================================================


class SelectionIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.articles = [
            make_article(1, "OpenAI releases a new reasoning model", "https://openai-news.example/1"),
            make_article(2, "EU passes AI liability rules", "https://policy.example/2", source="The Verge", hours_ago=5),
            make_article(3, "Best headphones deal this week", "https://deals.example/3", source="WIRED", hours_ago=1),
        ]

    def test_valid_picks_drops_everything_not_on_the_list(self):
        raw = [999, "https://evil.example/phish", 2, True, 2.0, "3", 2, 1, -1, None]
        self.assertEqual(mb.valid_picks(raw, [1, 2, 3], limit=5), [2, 3, 1])
        self.assertEqual(mb.valid_picks(raw, [1, 2, 3], limit=1), [2])
        self.assertEqual(mb.valid_picks({"ids": [1]}, [1], limit=5), [])

    def test_model_answer_with_made_up_ids_and_urls(self):
        client = FakeOpenAI(selection={"ids": [42, "https://evil.example/x", 2, 1]})
        ids, by = mb.select_story_ids(client, "gpt-4o-mini", self.articles, NOW)
        self.assertEqual((ids, by), ([2, 1], "model"))
        request = client.requests[0]
        self.assertEqual(request["model"], "gpt-4o-mini")
        self.assertTrue(request["response_format"]["json_schema"]["strict"])
        self.assertEqual(request["response_format"]["json_schema"]["schema"]["properties"]["ids"]["items"], {"type": "integer"})

    def test_garbage_or_failure_falls_back_to_the_heuristic(self):
        for client in (FakeOpenAI(selection="not json"), FakeOpenAI(selection={"ids": [77]}), FakeOpenAI(fail=True), None):
            ids, by = mb.select_story_ids(client, "gpt-4o-mini", self.articles, NOW)
            self.assertEqual(by, "heuristic")
            self.assertTrue(set(ids) <= {1, 2, 3})
            self.assertEqual(ids[0], 1)  # AI words + newest wins

    def test_same_event_headlines(self):
        a = make_article(1, "Apple may be developing its own AI agent that runs your Mac", "https://a.example/1")
        b = make_article(2, "Apple is hiring engineers to build its own AI agent", "https://b.example/2")
        c = make_article(3, "EU passes AI liability rules", "https://c.example/3")
        d = make_article(4, "Anthropic’s 7 founders cashed out nearly $2B in record-breaking IPO", "https://d.example/4")
        e = make_article(5, "Anthropic hits Nasdaq exchanges at $111, 33% above IPO price", "https://e.example/5")
        f = make_article(6, "Google is putting AI chats right on the homepage", "https://f.example/6")
        g = make_article(7, "Google fined in the EU over Android", "https://g.example/7")
        self.assertTrue(mb.same_event(a, b))
        self.assertTrue(mb.same_event(d, e))
        self.assertFalse(mb.same_event(a, c))
        self.assertFalse(mb.same_event(f, g))  # same company, one shared word: two stories
        self.assertFalse(mb.same_event(d, f))

    def test_build_stories_skips_the_same_event_and_text_less_items(self):
        articles = [
            make_article(1, "Apple may be developing its own AI agent that runs your Mac", "https://a.example/1"),
            make_article(2, "Apple is hiring engineers to build its own AI agent", "https://b.example/2"),
            make_article(3, "Aggregator item with nothing to read", "https://blocked.example/z", description="", kind="hn"),
            make_article(4, "EU passes AI liability rules", "https://c.example/4"),
        ]
        pages = {"https://a.example/1": LONG_TEXT, "https://c.example/4": LONG_TEXT}
        stories = mb.build_stories(None, None, "m", articles, [1, 2, 3, 4], wanted=2, extract=lambda s, url: pages.get(url))
        self.assertEqual([s.article.id for s in stories], [1, 4])

    def test_full_text_preferred_but_rank_order_kept(self):
        articles = [
            make_article(1, "Top story without a readable page", "https://paywall.example/1", description="Teaser only."),
            make_article(2, "Second story", "https://open.example/2"),
            make_article(3, "Third story here", "https://open.example/3"),
        ]
        pages = {"https://open.example/2": LONG_TEXT, "https://open.example/3": LONG_TEXT}
        stories = mb.build_stories(None, None, "m", articles, [1, 2, 3], wanted=2, extract=lambda s, url: pages.get(url))
        self.assertEqual([s.article.id for s in stories], [2, 3])
        stories = mb.build_stories(None, None, "m", articles[:2], [1, 2], wanted=2, extract=lambda s, url: pages.get(url))
        self.assertEqual([s.article.id for s in stories], [1, 2])  # nothing better: the teaser one, in rank order

    def test_backfill_when_every_shortlisted_page_is_unreadable(self):
        titles = ("Nvidia ships Rubin chips", "EU fines Meta", "OpenAI opens Tokyo office", "Robotaxis reach Bangkok", "Anthropic IPO prices")
        articles = [make_article(n, title, f"https://site{n}.example/", description="") for n, title in enumerate(titles, start=1)]
        ranked = mb.with_backfill([1, 2], articles, extra=2)
        self.assertEqual(ranked[:2], [1, 2])
        self.assertEqual(len(ranked), 4)
        self.assertEqual(len(set(ranked)), 4)
        readable = {f"https://site{n}.example/" for n in ranked[2:]}
        stories = mb.build_stories(None, None, "m", articles, ranked, wanted=2, extract=lambda s, url: LONG_TEXT if url in readable else None)
        self.assertEqual(sorted(s.article.id for s in stories), sorted(ranked[2:]))

    def test_summary_drops_links_and_unverifiable_numbers(self):
        client = FakeOpenAI(
            summary={
                "sentences": [
                    "Anthropic ระดมทุนได้ 81.3 พันล้านดอลลาร์จากการ IPO.",
                    "ผู้ก่อตั้ง 7 คนขายหุ้นที่ราคา 83 ดอลลาร์",
                    "มูลค่าบริษัทแตะ 500 พันล้านดอลลาร์",          # 500 is not in the article
                    "อ่านต่อที่ https://evil.example/phish",       # a link from the model
                    "• นี่คือก้าวสำคัญของวงการ AI.",
                ]
            }
        )
        article = make_article(1, "Anthropic IPO", "https://tc.example/ipo")
        result = mb.summarize(client, "gpt-4o-mini", article, LONG_TEXT)
        self.assertEqual(result.basis, "article")
        self.assertEqual(
            result.sentences,
            ["Anthropic ระดมทุนได้ 81.3 พันล้านดอลลาร์จากการ IPO", "ผู้ก่อตั้ง 7 คนขายหุ้นที่ราคา 83 ดอลลาร์", "นี่คือก้าวสำคัญของวงการ AI"],
        )
        prompt = client.requests[0]["messages"][1]["content"]
        self.assertIn(LONG_TEXT.strip()[:200], prompt)
        self.assertNotIn("tc.example", prompt)  # the model is never even shown the URL

    def test_summary_from_feed_text_is_labelled_and_failure_falls_back(self):
        article = make_article(1, "Teaser story", "https://x.example/1", description="Company X launched product Y today.")
        good = mb.summarize(FakeOpenAI(summary={"sentences": ["บริษัท X เปิดตัว Y", "เป็นข่าวสำคัญ"]}), "m", article, None)
        self.assertEqual(good.basis, "feed")
        bad = mb.summarize(FakeOpenAI(summary={"sentences": ["มีผู้ใช้ 9 ล้านคน"]}), "m", article, None)
        self.assertEqual((bad.basis, bad.sentences), ("fallback", ["Company X launched product Y today."]))
        down = mb.summarize(FakeOpenAI(fail=True), "m", article, None)
        self.assertEqual(down.basis, "fallback")
        offline = mb.summarize(None, "m", article, None)
        self.assertEqual(offline.basis, "fallback")

    def test_every_link_in_the_message_comes_from_a_feed(self):
        articles = [make_article(1, "Story one", "https://one.example/a?utm_source=rss"), make_article(2, "Story two", "https://two.example/b")]
        client = FakeOpenAI(summary={"sentences": ["ดูได้ที่ www.fake.example", "ประโยคจริง", "อีกประโยค", "https://evil.example"]})
        stories = mb.build_stories(None, client, "m", articles, [1, 2], wanted=2, extract=lambda s, url: LONG_TEXT)
        message = mb.build_flex_message(stories, context(), date(2026, 11, 20))
        feed_urls = {a.url for a in articles}
        self.assertEqual(set(all_uris(message)), feed_urls)
        for text in all_strings(message):
            if "http" in text or "www." in text:
                self.assertIn(text, feed_urls)


# =====================================================================
# Step 4 - optional context, degrading gracefully
# =====================================================================

TOKEN = {
    "token": "expired-access",
    "expiry": "2026-11-19T00:00:00Z",
    "refresh_token": "refresh-123",
    "client_id": "client.apps.googleusercontent.com",
    "client_secret": "shh",
    "token_uri": "https://oauth2.googleapis.com/token",
    "scopes": ["https://www.googleapis.com/auth/calendar.events", "https://www.googleapis.com/auth/classroom.courses.readonly"],
}

EVENTS = {
    "items": [
        {"summary": "Stand-up", "start": {"dateTime": "2026-11-20T09:00:00+07:00"}, "end": {"dateTime": "2026-11-20T09:30:00+07:00"}},
        {"summary": "Public holiday", "start": {"date": "2026-11-20"}, "end": {"date": "2026-11-21"}},
        {"summary": "Cancelled thing", "status": "cancelled", "start": {"dateTime": "2026-11-20T10:00:00+07:00"}},
        {"summary": "Declined meeting", "attendees": [{"self": True, "responseStatus": "declined"}], "start": {"dateTime": "2026-11-20T11:00:00+07:00"}},
        {"summary": "Office", "eventType": "workingLocation", "start": {"date": "2026-11-20"}},
        {"summary": "Lunch with Nok", "start": {"dateTime": "2026-11-20T05:00:00Z"}, "end": {"dateTime": "2026-11-20T06:00:00Z"}},
    ]
}


class CalendarTests(unittest.TestCase):
    def google_session(self, refresh: Optional[FakeResponse] = None, events: Optional[FakeResponse] = None) -> FakeSession:
        return FakeSession(
            {
                ("POST", "https://oauth2.googleapis.com/token"): refresh or FakeResponse(200, {"access_token": "fresh-access", "expires_in": 3599}),
                ("GET", mb.CALENDAR_API): events or FakeResponse(200, EVENTS),
            }
        )

    def test_missing_token_is_skipped_without_network(self):
        session = self.google_session()
        section = mb.fetch_calendar(session, {}, NOW, mb.bangkok_zone())
        self.assertEqual(section.status, "skipped")
        self.assertEqual(session.calls, [])

    def test_broken_or_scopeless_token(self):
        session = self.google_session()
        self.assertEqual(mb.fetch_calendar(session, {"GOOGLE_TOKEN_JSON": "{not json"}, NOW, mb.bangkok_zone()).status, "error")
        self.assertEqual(mb.fetch_calendar(session, {"GOOGLE_TOKEN_JSON": "[1, 2]"}, NOW, mb.bangkok_zone()).status, "error")
        scopeless = dict(TOKEN, scopes=["https://www.googleapis.com/auth/classroom.courses.readonly"])
        section = mb.fetch_calendar(session, {"GOOGLE_TOKEN_JSON": json.dumps(scopeless)}, NOW, mb.bangkok_zone())
        self.assertEqual(section.status, "skipped")
        self.assertEqual(session.calls, [])

    def test_refresh_then_today_events_filtered_and_sorted(self):
        session = self.google_session()
        section = mb.fetch_calendar(session, {"GOOGLE_TOKEN_JSON": json.dumps(TOKEN)}, NOW, mb.bangkok_zone())
        self.assertEqual(section.status, "ok")
        self.assertEqual([(e.when, e.title) for e in section.items], [("ทั้งวัน", "Public holiday"), ("09:00–09:30", "Stand-up"), ("12:00–13:00", "Lunch with Nok")])
        refresh = session.calls[0]
        self.assertEqual(refresh[2]["data"]["grant_type"], "refresh_token")
        method, url, kwargs = session.calls[1]
        self.assertIn("/calendars/primary/events", url)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer fresh-access")
        self.assertEqual(kwargs["params"]["timeMin"], "2026-11-20T00:00:00+07:00")
        self.assertEqual(kwargs["params"]["timeMax"], "2026-11-21T00:00:00+07:00")

    def test_valid_saved_access_token_skips_the_refresh(self):
        session = self.google_session()
        fresh = dict(TOKEN, expiry=(NOW + timedelta(minutes=30)).isoformat())
        mb.fetch_calendar(session, {"GOOGLE_TOKEN_JSON": json.dumps(fresh), "GOOGLE_CALENDAR_ID": "me@example.com"}, NOW, mb.bangkok_zone())
        self.assertEqual([c[0] for c in session.calls], ["GET"])
        self.assertIn("/calendars/me%40example.com/events", session.calls[0][1])

    def test_expired_sign_in_and_api_errors_degrade(self):
        expired = self.google_session(refresh=FakeResponse(400, {"error": "invalid_grant"}))
        section = mb.fetch_calendar(expired, {"GOOGLE_TOKEN_JSON": json.dumps(TOKEN)}, NOW, mb.bangkok_zone())
        self.assertEqual(section.status, "skipped")
        self.assertIn("google_auth.py", section.note)
        forbidden = self.google_session(events=FakeResponse(403, {"error": {"code": 403}}))
        self.assertEqual(mb.fetch_calendar(forbidden, {"GOOGLE_TOKEN_JSON": json.dumps(TOKEN)}, NOW, mb.bangkok_zone()).status, "error")
        offline = FakeSession({("POST", "https://oauth2"): ConnectionError("dns")})
        self.assertEqual(mb.fetch_calendar(offline, {"GOOGLE_TOKEN_JSON": json.dumps(TOKEN)}, NOW, mb.bangkok_zone()).status, "error")
        no_refresh = dict(TOKEN)
        del no_refresh["refresh_token"]
        self.assertEqual(mb.fetch_calendar(self.google_session(), {"GOOGLE_TOKEN_JSON": json.dumps(no_refresh)}, NOW, mb.bangkok_zone()).status, "skipped")


class ProjectsTests(unittest.TestCase):
    def test_no_database_url_is_skipped(self):
        self.assertEqual(mb.fetch_projects({}).status, "skipped")

    def test_read_only_query_for_the_owner(self):
        connection = FakeConnection([("TripPlanner",), ("Jarvis Hub",)])
        section = mb.fetch_projects({"DATABASE_URL": "postgresql://u:p@h/db"}, connect=lambda *a, **k: connection)
        self.assertEqual((section.status, section.items), ("ok", ["TripPlanner", "Jarvis Hub"]))
        statements = [sql for sql, _ in connection.statements]
        self.assertEqual(statements[0], "BEGIN READ ONLY")
        self.assertEqual(statements[-1], "ROLLBACK")
        self.assertTrue(all(not s.upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP")) for s in statements))
        self.assertEqual(connection.statements[2][1]["scope"], "owner")
        self.assertTrue(connection.autocommit and connection.closed)

    def test_memory_url_wins_and_scope_is_configurable(self):
        seen = {}

        def connect(url: str, **kwargs: Any) -> FakeConnection:
            seen["url"] = url
            return FakeConnection([])

        env = {"DATABASE_URL": "postgresql://app", "JARVIS_MEMORY_DATABASE_URL": "postgresql://memory", "BRIEFING_MEMORY_SCOPE": "user:1"}
        self.assertEqual(mb.fetch_projects(env, connect=connect).status, "ok")
        self.assertEqual(seen["url"], "postgresql://memory")

    def test_database_failure_never_leaks_the_url(self):
        def connect(url: str, **kwargs: Any) -> None:
            raise RuntimeError(f"connection to {url} failed: password authentication failed for user briefing_reader")

        section = mb.fetch_projects({"DATABASE_URL": "postgresql://briefing_reader:hunter2@db.example.com/db"}, connect=connect)
        self.assertEqual(section.status, "error")
        self.assertNotIn("hunter2", section.note)
        self.assertNotIn("db.example.com", section.note)

    def test_gather_context_survives_a_crashing_part(self):
        with mock.patch.object(mb, "fetch_calendar", side_effect=RuntimeError("boom")), contextlib.redirect_stderr(io.StringIO()):
            result = mb.gather_context(FakeSession(), {}, NOW, mb.bangkok_zone())
        self.assertEqual(result.calendar.status, "error")
        self.assertEqual(result.projects.status, "skipped")


# =====================================================================
# Step 5 - the Flex message against LINE's limits
# =====================================================================


class FlexMessageTests(unittest.TestCase):
    def test_full_briefing_is_valid(self):
        events = [mb.Event("", "", "Holiday")] + [mb.Event(f"{9 + i:02d}:00", f"{10 + i:02d}:00", f"Meeting {i}") for i in range(7)]
        stories = [story(1, "Story one", "https://one.example/a"), story(2, "Story two", "https://two.example/b")]
        message = mb.build_flex_message(stories, context(events, ["TripPlanner", "Jarvis"]), date(2026, 11, 20))
        self.assertEqual(mb.validate_flex(message), [])
        bubbles = message["contents"]["contents"]
        self.assertEqual(len(bubbles), 3)
        self.assertTrue(message["altText"].startswith("☀️ Jarvis Morning Briefing — "))
        self.assertIn("20 November 2026", message["altText"])
        cover = json.dumps(bubbles[0], ensure_ascii=False)
        self.assertIn("วันศุกร์ที่ 20 พฤศจิกายน 2569", cover)
        self.assertIn("และอีก 3 นัดหมาย", cover)  # 8 events, 5 shown
        self.assertIn("TripPlanner · Jarvis", cover)
        news = bubbles[1]
        self.assertIn(mb.CATEGORY_BADGE, json.dumps(news["header"]))
        title = news["header"]["contents"][1]
        self.assertTrue(title["wrap"] and title["weight"] == "bold" and title["maxLines"] == 3)
        button = news["footer"]["contents"][0]
        self.assertEqual(button["action"], {"type": "uri", "label": "อ่านข่าวเต็ม", "uri": "https://one.example/a"})
        sentences = [c for c in news["body"]["contents"] if c.get("type") == "text"][:-1]
        self.assertEqual([c["text"] for c in sentences], stories[0].sentences)
        self.assertTrue(all(c["wrap"] for c in sentences))
        json.loads(json.dumps(message, ensure_ascii=False))  # serialisable as sent

    def test_compact_cover_without_context(self):
        message = mb.build_flex_message([story(1, "Only story", "https://one.example/a")], context(calendar_status="skipped"), date(2026, 11, 20))
        self.assertEqual(mb.validate_flex(message), [])
        cover = json.dumps(message["contents"]["contents"][0], ensure_ascii=False)
        self.assertNotIn("SCHEDULE", cover)
        self.assertNotIn("FOCUS", cover)
        self.assertIn("TOP STORIES", cover)

    def test_calendar_connected_but_empty_and_no_stories(self):
        message = mb.build_flex_message([], context([]), date(2026, 11, 20))
        self.assertEqual(mb.validate_flex(message), [])
        cover = json.dumps(message, ensure_ascii=False)
        self.assertIn("ไม่มีนัดหมายในปฏิทินวันนี้", cover)
        self.assertEqual(len(message["contents"]["contents"]), 1)

    def test_worst_case_sizes_stay_under_the_limits(self):
        huge_title = "ก" * 300
        sentences = ["ข" * 400] * 5
        stories = [mb.Story(make_article(i, huge_title, "https://e.example/" + "p" * 900), sentences, "article") for i in (1, 2)]
        events = [mb.Event("09:00", "10:00", "ค" * 70) for _ in range(25)]
        message = mb.build_flex_message(stories, context(events, ["ง" * 60] * 3), date(2026, 11, 20))
        self.assertEqual(mb.validate_flex(message), [])
        for bubble in message["contents"]["contents"]:
            self.assertLess(mb.json_bytes(bubble), mb.LINE_MAX_BUBBLE_BYTES)
        self.assertLess(mb.json_bytes(message["contents"]), mb.LINE_MAX_CAROUSEL_BYTES)

    def test_validator_catches_each_rule(self):
        def broken(change: Callable[[dict], None]) -> list[str]:
            message = mb.build_flex_message([story(1, "S", "https://one.example/a")], context(), date(2026, 11, 20))
            change(message)
            return mb.validate_flex(message)

        news = lambda m: m["contents"]["contents"][1]  # noqa: E731
        action = lambda m: news(m)["footer"]["contents"][0]["action"]  # noqa: E731
        cases = {
            "empty text": lambda m: news(m)["header"]["contents"].__setitem__(1, {"type": "text", "text": "  "}),
            "bad colour": lambda m: news(m)["styles"]["body"].__setitem__("backgroundColor", "black"),
            "long label": lambda m: action(m).__setitem__("label", "อ่านข่าวเต็มที่หน้าเว็บของสำนักข่าว"),
            "javascript uri": lambda m: action(m).__setitem__("uri", "javascript:alert(1)"),
            "too many bubbles": lambda m: m["contents"]["contents"].extend([news(m)] * 12),
            "alt text": lambda m: m.__setitem__("altText", "x" * 1501),
            "box layout": lambda m: news(m)["body"].__setitem__("layout", "grid"),
            "huge bubble": lambda m: news(m)["body"]["contents"].append({"type": "text", "text": "x" * 31000}),
            "not flex": lambda m: m.__setitem__("type", "text"),
        }
        for name, change in cases.items():
            self.assertTrue(broken(change), f"validate_flex missed: {name}")

    def test_text_fallback_fits_a_text_message(self):
        stories = [mb.Story(make_article(1, "T", "https://one.example/a"), ["ข" * 6000], "article"), story(2, "U", "https://two.example/b")]
        fallback = mb.text_fallback(stories, context([mb.Event("09:00", "10:00", "Stand-up")]), date(2026, 11, 20))
        self.assertEqual(fallback["type"], "text")
        self.assertLessEqual(len(fallback["text"]), mb.LINE_MAX_TEXT_MESSAGE)
        self.assertIn("09:00–10:00 Stand-up", fallback["text"])

    def test_dates(self):
        self.assertEqual(mb.thai_date(date(2026, 11, 20)), "วันศุกร์ที่ 20 พฤศจิกายน 2569")
        self.assertEqual(mb.english_date(date(2026, 11, 20)), "Friday, 20 November 2026")
        self.assertEqual(mb.thai_short_time(datetime(2026, 11, 19, 14, 30, tzinfo=timezone.utc)), "19 พ.ย. 21:30 น.")


# =====================================================================
# Sending, and the whole run
# =====================================================================


class LinePushTests(unittest.TestCase):
    def test_retry_uses_one_retry_key_and_409_counts_as_sent(self):
        answers = iter([FakeResponse(500, {"message": "oops"}), FakeResponse(409, {"message": "dup"})])
        session = FakeSession({("POST", mb.LINE_PUSH_URL): lambda url, kw: next(answers)})
        with mock.patch.object(mb.time, "sleep"):
            ok, reason, status = mb.push_message(session, "tok", "U1", {"type": "text", "text": "hi"})
        self.assertTrue(ok)
        keys = {call[2]["headers"]["X-Line-Retry-Key"] for call in session.calls}
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(len(keys), 1)

    def test_a_400_is_not_retried(self):
        session = FakeSession({("POST", mb.LINE_PUSH_URL): FakeResponse(400, {"message": "A message (messages[0]) in the request body is invalid", "details": [{"property": "/contents", "message": "bad"}]})})
        ok, reason, status = mb.push_message(session, "tok", "U1", {"type": "text", "text": "hi"})
        self.assertFalse(ok)
        self.assertEqual((status, len(session.calls)), (400, 1))
        self.assertIn("/contents", reason)

    def test_line_config_names(self):
        self.assertEqual(mb.line_config({"LINE_CHANNEL_ACCESS_TOKEN": "t", "LINE_USER_ID": "Uold"}), ("t", "Uold"))
        self.assertEqual(mb.line_config({"LINE_CHANNEL_ACCESS_TOKEN": "t", "LINE_USER_ID": "Uold", "LINE_TARGET_USER_ID": "Unew"}), ("t", "Unew"))


@needs_feedparser  # the run tests parse a real feed
class MainRunTests(unittest.TestCase):
    """main() end to end with every outside call replaced."""

    def setUp(self):
        now = datetime.now(timezone.utc)
        feed = rss(
            [
                {"title": "OpenAI releases a new reasoning model", "link": "https://tc.example/openai", "published": now - timedelta(hours=2), "description": "A new model."},
                {"title": "EU passes AI liability rules", "link": "https://tc.example/eu", "published": now - timedelta(hours=3), "description": "Rules."},
            ]
        )
        self.posts: list[dict] = []
        self.line_status = 200

        def line(url: str, kwargs: dict) -> FakeResponse:
            self.posts.append({"url": url, "headers": kwargs["headers"], "body": json.loads(kwargs["data"].decode("utf-8"))})
            status = self.line_status if self.posts[-1]["body"]["messages"][0]["type"] == "flex" else 200
            return FakeResponse(status, {"message": "invalid"} if status != 200 else {})

        self.session = FakeSession(
            {
                ("GET", mb.FEEDS[0].url): FakeResponse(200, content=feed),
                ("POST", "https://oauth2.googleapis.com/token"): FakeResponse(200, {"access_token": "fresh"}),
                ("GET", mb.CALENDAR_API): FakeResponse(200, {"items": [{"summary": "Dentist appointment", "start": {"dateTime": "2026-11-20T15:00:00+07:00"}, "end": {"dateTime": "2026-11-20T16:00:00+07:00"}}]}),
                ("POST", "https://api.line.me/"): line,
            },
            default=FakeResponse(503),
        )
        self.client = FakeOpenAI(selection={"ids": [2, 1]}, summary={"sentences": ["ประโยคแรก", "ประโยคที่สอง", "ประโยคที่สาม", "ประโยคที่สี่"]})

    def run_main(self, argv: list[str], env: dict) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(mb.requests, "Session", return_value=self.session), mock.patch.object(
            mb, "make_openai_client", return_value=self.client
        ), mock.patch.object(mb, "extract_article", return_value=LONG_TEXT), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = mb.main(argv, env=env)
        return code, out.getvalue(), err.getvalue()

    def test_dry_run_with_no_optional_settings(self):
        code, out, err = self.run_main(["--dry-run"], {"OPENAI_API_KEY": "sk-test"})
        self.assertEqual(code, 0, err)
        self.assertEqual(self.posts, [])
        self.assertIn("calendar: skipped", out)
        self.assertIn("projects: skipped", out)
        message = json.loads(out.split("---\n", 1)[1])
        self.assertEqual(mb.validate_flex(message), [])
        self.assertEqual(set(all_uris(message)), {"https://tc.example/eu", "https://tc.example/openai"})
        self.assertEqual(message["contents"]["contents"][1]["footer"]["contents"][0]["action"]["uri"], "https://tc.example/eu")  # the model's order

    def test_real_send_uses_the_legacy_recipient_name_and_logs_no_secrets(self):
        env = {"OPENAI_API_KEY": "sk-test", "LINE_CHANNEL_ACCESS_TOKEN": "tok-SECRET-123", "LINE_USER_ID": "U-legacy-recipient", "GOOGLE_TOKEN_JSON": json.dumps(TOKEN)}
        code, out, err = self.run_main([], env)
        self.assertEqual(code, 0, err)
        [push] = self.posts
        self.assertEqual(push["url"], mb.LINE_PUSH_URL)
        self.assertEqual(push["body"]["to"], "U-legacy-recipient")
        self.assertEqual(push["headers"]["Authorization"], "Bearer tok-SECRET-123")
        self.assertTrue(push["headers"]["X-Line-Retry-Key"])
        self.assertEqual(push["body"]["messages"][0]["type"], "flex")
        self.assertIn("Dentist appointment", json.dumps(push["body"], ensure_ascii=False))
        for secret in ("tok-SECRET-123", "U-legacy-recipient", "Dentist", "refresh-123", "sk-test"):
            self.assertNotIn(secret, out + err)

    def test_missing_line_settings_stop_before_any_work(self):
        code, out, err = self.run_main([], {"OPENAI_API_KEY": "sk-test"})
        self.assertEqual(code, 2)
        self.assertEqual(self.session.calls, [])
        self.assertEqual(self.client.requests, [])

    def test_missing_openai_key_stops_a_real_send(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(mb.requests, "Session", return_value=self.session), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = mb.main([], env={"LINE_CHANNEL_ACCESS_TOKEN": "t", "LINE_TARGET_USER_ID": "U1"})
        self.assertEqual(code, 2)
        self.assertEqual(self.session.calls, [])

    def test_line_refusing_the_flex_sends_plain_text_and_fails_the_run(self):
        self.line_status = 400
        env = {"OPENAI_API_KEY": "sk-test", "LINE_CHANNEL_ACCESS_TOKEN": "t", "LINE_TARGET_USER_ID": "U1"}
        code, out, err = self.run_main([], env)
        self.assertEqual(code, 1)
        self.assertEqual([p["body"]["messages"][0]["type"] for p in self.posts], ["flex", "text"])
        self.assertIn("https://tc.example/eu", self.posts[1]["body"]["messages"][0]["text"])

    def test_dry_run_in_github_actions_hides_private_lines(self):
        env = {"OPENAI_API_KEY": "sk-test", "GITHUB_ACTIONS": "true", "GOOGLE_TOKEN_JSON": json.dumps(TOKEN)}
        with mock.patch.object(mb, "fetch_projects", return_value=mb.Section("ok", "1 active project(s)", ["Secret Project X"])):
            code, out, err = self.run_main(["--dry-run"], env)
        self.assertEqual(code, 0, err)
        self.assertNotIn("Dentist", out + err)
        self.assertNotIn("Secret Project X", out + err)
        self.assertIn(mb.REDACTED, out)

    def test_validate_line_flag_calls_only_the_validate_endpoint(self):
        env = {"OPENAI_API_KEY": "sk-test", "LINE_CHANNEL_ACCESS_TOKEN": "t"}
        code, out, err = self.run_main(["--dry-run", "--validate-line"], env)
        self.assertEqual(code, 0, err)
        self.assertEqual([p["url"] for p in self.posts], [mb.LINE_VALIDATE_URL])
        self.assertNotIn("to", self.posts[0]["body"])

    def test_no_news_still_sends_a_cover_with_events_but_fails_the_run(self):
        self.session.routes.pop(("GET", mb.FEEDS[0].url))
        env = {"OPENAI_API_KEY": "sk-test", "LINE_CHANNEL_ACCESS_TOKEN": "t", "LINE_TARGET_USER_ID": "U1", "GOOGLE_TOKEN_JSON": json.dumps(TOKEN)}
        code, out, err = self.run_main([], env)
        self.assertEqual(code, 1)
        [push] = self.posts
        bubbles = push["body"]["messages"][0]["contents"]["contents"]
        self.assertEqual(len(bubbles), 1)
        self.assertIn("Dentist appointment", json.dumps(bubbles[0], ensure_ascii=False))

    def test_nothing_to_send(self):
        self.session.routes.pop(("GET", mb.FEEDS[0].url))
        env = {"OPENAI_API_KEY": "sk-test", "LINE_CHANNEL_ACCESS_TOKEN": "t", "LINE_TARGET_USER_ID": "U1"}
        code, out, err = self.run_main([], env)
        self.assertEqual(code, 1)
        self.assertEqual(self.posts, [])

    def test_openai_down_still_sends_a_labelled_briefing(self):
        self.client = FakeOpenAI(fail=True)
        env = {"OPENAI_API_KEY": "sk-test", "LINE_CHANNEL_ACCESS_TOKEN": "t", "LINE_TARGET_USER_ID": "U1"}
        code, out, err = self.run_main([], env)
        self.assertEqual(code, 0, err)
        flex = json.dumps(self.posts[0]["body"], ensure_ascii=False)
        self.assertIn(mb.BASIS_NOTE["fallback"], flex)


if __name__ == "__main__":
    unittest.main()
