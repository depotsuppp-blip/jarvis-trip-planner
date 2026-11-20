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
- Hero images: feed media / og:image / twitter:image / feed <img> extraction and their order,
  the https + 2000-character + JPEG/PNG + no-SVG/avatar/pixel rules, LINE-size URL variants,
  the probe (status, type, real PNG/JPEG dimensions), and the accent bar when nothing passes.
- The structured summary: three parts, capped (2 / 2 / 1), grounded part by part, and the
  fallback when nothing about what happened survives.
- The Flex Message against LINE's limits (bubbles, bytes, altText, labels, URIs, image URLs,
  aspect ratios, colours, empty texts) - for normal and worst-case input - validate_flex
  catching each break, and the editorial layout (palette, masthead, index, blocks, callout).
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
import struct
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
            f"<description>{escape(item.get('description', ''))}</description>{source}{item.get('extra', '')}</item>"
        )
    return (
        "<?xml version='1.0'?><rss version='2.0' xmlns:media='http://search.yahoo.com/mrss/'>"
        f"<channel><title>{title}</title>{''.join(parts)}</channel></rss>"
    ).encode()


def png(width: int, height: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00" + b"\x00" * 64


def jpeg(width: int, height: int) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
    exif = b"\xff\xe1" + struct.pack(">H", 2002) + b"\x00" * 2000  # a big block before the frame header
    sof = b"\xff\xc2" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", height, width) + b"\x03" + b"\x00" * 9
    return b"\xff\xd8" + app0 + exif + sof + b"\x00" * 64


def image_response(data: bytes, kind: str = "") -> FakeResponse:
    kind = kind or ("image/png" if data.startswith(b"\x89PNG") else "image/jpeg")
    return FakeResponse(200, content=data, headers={"Content-Type": kind, "Content-Length": str(len(data))})


def pages_of(texts: dict, images: Optional[dict] = None) -> Callable[[Any, str], Any]:
    """An extract_article stand-in: url -> Page(text, og images)."""
    return lambda session, url: mb.Page(texts.get(url), tuple((images or {}).get(url, ())))


def make_article(number: int, title: str, url: str, source: str = "TechCrunch", hours_ago: float = 2, description: str = "A short description.", kind: str = "publisher", feed_text: str = "", images: tuple = (), inline_images: tuple = ()) -> Any:
    return mb.Article(number, title, url, source, NOW - timedelta(hours=hours_ago), description, source, kind, feed_text or description, images, inline_images)


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


SUMMARY = {
    "what_happened": ["ประโยคที่หนึ่ง", "ประโยคที่สอง"],
    "why_it_matters": ["ประโยคที่สาม"],
    "takeaway": "ประโยคที่สี่",
}


def summary(**parts: Any) -> Any:
    data = {**SUMMARY, **parts}
    return mb.Summary(list(data["what_happened"]), list(data["why_it_matters"]), data["takeaway"])


def story(number: int, title: str, url: str, parts: Optional[Any] = None, basis: str = "article", image: str = "") -> Any:
    return mb.Story(make_article(number, title, url), parts or summary(), basis, image)


def components(node: Any, kind: str):
    """Every component of one type, depth first."""
    if isinstance(node, dict):
        if node.get("type") == kind:
            yield node
        for value in node.values():
            yield from components(value, kind)
    elif isinstance(node, list):
        for value in node:
            yield from components(value, kind)


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

    def test_feed_media_and_inline_images_are_collected_and_filtered(self):
        feed = mb.Feed("Ars Technica", "https://ars.example/feed")
        extra = (
            "<media:content url='https://secure.gravatar.com/avatar/abc?s=96' medium='image'/>"   # the author's face
            "<media:content url='https://cdn.ars.example/hero-1152x648.jpg' medium='image' type='image/jpeg' width='1152' height='648'/>"
            "<media:content url='https://cdn.ars.example/clip.mp4' medium='video'/>"
            "<media:thumbnail url='https://cdn.ars.example/hero-150x150.jpg' width='150' height='150'/>"  # too small
            "<media:thumbnail url='http://cdn.ars.example/plain-http.jpg' width='800' height='450'/>"    # not https
            "<enclosure url='https://cdn.ars.example/diagram.svg' type='image/svg+xml' length='100'/>"
            "<enclosure url='https://cdn.ars.example/enclosed.png' type='image/png' length='90000'/>"
        )
        description = "<p><img src='https://cdn.ars.example/inline.jpg?a=1&amp;b=2' width='1200'/> Body text that is long enough.</p>"
        content = rss([{"title": "A story", "link": "https://ars.example/a", "published": NOW - timedelta(hours=1), "description": description, "extra": extra}])
        [item] = mb.parse_feed(content, feed, NOW)
        self.assertEqual(item.images, ("https://cdn.ars.example/hero-1152x648.jpg", "https://cdn.ars.example/enclosed.png"))
        self.assertEqual(item.inline_images, ("https://cdn.ars.example/inline.jpg?a=1&b=2",))

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
# Hero images
# =====================================================================


class ImageTests(unittest.TestCase):
    def test_clean_image_url_keeps_only_https_jpeg_png_photos(self):
        good = {
            "https://cdn.example/a.jpg": "https://cdn.example/a.jpg",
            "https://cdn.example/a.PNG?w=1200": "https://cdn.example/a.PNG?w=1200",
            "https://media.example/photos/1/191:100/w_1280,c_limit/Story": "https://media.example/photos/1/191:100/w_1280,c_limit/Story",  # no extension: the probe decides
            "HTTPS://cdn.example/b.jpeg": "https://cdn.example/b.jpeg",
            "//cdn.example/protocol-relative.jpg": "https://cdn.example/protocol-relative.jpg",
            "https://cdn.example/ไฟล์ ภาพ.jpg": "https://cdn.example/%E0%B9%84%E0%B8%9F%E0%B8%A5%E0%B9%8C%20%E0%B8%A0%E0%B8%B2%E0%B8%9E.jpg",
            "https://www.blognone.example/files/LINE%20THAILAND/image4.jpg": "https://www.blognone.example/files/LINE%20THAILAND/image4.jpg",
        }
        for raw, expected in good.items():
            self.assertEqual(mb.clean_image_url(raw), expected, raw)
        bad = (
            "http://cdn.example/a.jpg",                  # LINE loads https only
            "https://cdn.example/logo.svg",
            "https://cdn.example/anim.gif",
            "https://cdn.example/photo.webp",
            "https://cdn.example/clip.mp4",
            "https://cdn.example/pixel.png",
            "https://cdn.example/t/1x1.jpg",
            "https://secure.gravatar.com/avatar/abc.jpg",
            "https://cdn.example/avatars/author.jpg",
            "https://pixel.wp.com/g.png",
            "data:image/png;base64,iVBORw0KGgo=",
            "javascript:alert(1)",
            "/relative/without/base.jpg",
            "https://cdn.example/" + "a" * 2000 + ".jpg",  # over LINE's 2000 characters
            "",
            None,
        )
        for raw in bad:
            self.assertEqual(mb.clean_image_url(raw), "", raw)
        self.assertEqual(mb.clean_image_url("/img/hero.jpg", "https://news.example/story/1"), "https://news.example/img/hero.jpg")

    def test_is_line_image_url(self):
        self.assertTrue(mb.is_line_image_url("https://cdn.example/a.jpg?x=1&y=2"))
        for bad in ("http://cdn.example/a.jpg", "https://cdn.example/ภาพ.jpg", "https://cdn.example/a b.jpg", "https://", "https://e.example/" + "a" * 2000, 42):
            self.assertFalse(mb.is_line_image_url(bad), bad)

    def test_feed_media_order_and_filters_without_feedparser(self):
        entry = {
            "media_content": [{"url": "", "medium": None}, {"url": "https://c.example/video.jpg", "medium": "video"}, {"url": "https://c.example/content.jpg", "medium": "image", "type": "image/jpeg", "width": "1152", "height": "648"}],
            "media_thumbnail": [{"url": "https://c.example/thumb-big.jpg", "width": "1232", "height": "1232"}, {"url": "https://c.example/thumb-small.jpg", "width": "150"}],
            "enclosures": [{"href": "https://c.example/enc.png", "type": "image/png"}, {"href": "https://c.example/enc.webp", "type": "image/webp"}, {"href": "https://c.example/a.mp3", "type": "audio/mpeg"}],
        }
        self.assertEqual(mb.feed_media_images(entry), ("https://c.example/content.jpg", "https://c.example/thumb-big.jpg", "https://c.example/enc.png"))
        self.assertEqual(mb.feed_media_images({}), ())

    def test_page_images_prefer_og_then_twitter_from_the_head(self):
        page = b"""<!doctype html><html><head>
            <meta name="twitter:image" content="https://cdn.example/tw.jpg">
            <meta content="https://cdn.example/og.jpg?quality=90&amp;strip=all&amp;w=1200" property="og:image" />
            <meta property="og:image:width" content="1200">
            <meta property='og:image:secure_url' content='/img/secure.jpg'>
            </head><body><meta property="og:image" content="https://cdn.example/in-body.jpg"></body></html>"""
        self.assertEqual(
            mb.page_images(page, "https://news.example/story"),
            ("https://news.example/img/secure.jpg", "https://cdn.example/og.jpg?quality=90&strip=all&w=1200", "https://cdn.example/tw.jpg"),
        )
        self.assertEqual(mb.page_images(b"<html><head><title>No images</title></head></html>", "https://n.example/"), ())
        self.assertEqual(mb.page_images(b"\xff\xfe<<<not html", "https://n.example/"), ())

    def test_inline_images_skip_small_and_relative_ones(self):
        markup = (
            "<p><img src='https://feeds.example/~r/track.gif' width='1' height='1'></p>"
            "<img src='/relative.jpg'><img src='https://b.example/icon.png' width='32'>"
            "<img data-src='https://b.example/lead.jpg'><img src='https://b.example/second.jpg' width='1200'><img src='https://b.example/third.jpg'>"
        )
        self.assertEqual(mb.inline_images(markup), ("https://b.example/lead.jpg", "https://b.example/second.jpg"))
        self.assertEqual(mb.inline_images("no images here"), ())

    def test_line_sized_variants(self):
        cases = {
            "https://tc.example/a.jpg?resize=1200,800": "https://tc.example/a.jpg?resize=1024,683",
            "https://mit.example/a.jpg?resize=1200%2C600": "https://mit.example/a.jpg?resize=1024%2C512",
            "https://verge.example/a.jpg?quality=90&strip=all&crop=0%2C10%2C100%2C80&w=1200": "https://verge.example/a.jpg?quality=90&strip=all&crop=0%2C10%2C100%2C80&w=1024",
            "https://p.example/a.jpg?w=2048&h=1024": "https://p.example/a.jpg?w=1024&h=512",
            "https://media.example/photos/1/191:100/w_1280,c_limit/Story": "https://media.example/photos/1/191:100/w_1024,c_limit/Story",
        }
        for url, expected in cases.items():
            self.assertEqual(mb.line_sized(url), expected, url)
        for url in ("https://ars.example/hero-1152x648.jpg", "https://p.example/a.jpg?w=800", "https://p.example/a.jpg?resize=1024,576", "https://p.example/a.jpg?width=2000&show=w=5000"):
            self.assertEqual(mb.line_sized(url), "", url)

    def test_image_size_reads_png_and_jpeg_headers(self):
        self.assertEqual(mb.image_size(png(1200, 630)), (1200, 630))
        self.assertEqual(mb.image_size(jpeg(1024, 576)), (1024, 576))
        self.assertEqual((mb.image_format(png(1, 1)), mb.image_format(jpeg(1, 1)), mb.image_format(b"GIF89a")), ("png", "jpeg", ""))
        self.assertIsNone(mb.image_size(jpeg(1024, 576)[:600]))  # cut before the frame header
        self.assertIsNone(mb.image_size(b"<html>"))

    def test_probe_accepts_real_photos_and_rejects_the_rest(self):
        routes = {
            ("GET", "https://ok.example/png"): image_response(png(1200, 630)),
            ("GET", "https://ok.example/jpeg"): image_response(jpeg(1024, 576)),
            ("GET", "https://ok.example/unknown-size"): image_response(jpeg(1024, 576)[:600]),
            ("GET", "https://bad.example/pixel"): image_response(png(1, 1)),
            ("GET", "https://bad.example/thumb"): image_response(jpeg(150, 150)),
            ("GET", "https://bad.example/blocked"): FakeResponse(403),
            ("GET", "https://bad.example/html"): FakeResponse(200, content=b"<html>", headers={"Content-Type": "text/html"}),
            ("GET", "https://bad.example/webp"): FakeResponse(200, content=b"RIFF....WEBP", headers={"Content-Type": "image/webp"}),
            ("GET", "https://bad.example/lies"): FakeResponse(200, content=b"<svg/>", headers={"Content-Type": "image/png"}),
            ("GET", "https://bad.example/huge"): FakeResponse(200, content=png(2000, 1000), headers={"Content-Type": "image/png", "Content-Length": str(11 * 1024 * 1024)}),
            ("GET", "https://bad.example/redirected"): FakeResponse(200, content=png(1200, 630), headers={"Content-Type": "image/png"}, url="http://plain.example/x.png"),
            ("GET", "https://bad.example/down"): ConnectionError("dns"),
        }
        session = FakeSession(routes)
        for path in ("png", "jpeg", "unknown-size"):
            ok, why, _ = mb.probe_image(session, f"https://ok.example/{path}")
            self.assertTrue(ok, f"{path}: {why}")
        ok, why, size = mb.probe_image(session, "https://ok.example/png")
        self.assertEqual(size, (1200, 630))
        self.assertIn("over LINE's documented 1024 px", why)
        self.assertIsNone(mb.probe_image(session, "https://ok.example/unknown-size")[2])
        for path in ("pixel", "thumb", "blocked", "html", "webp", "lies", "huge", "redirected", "down"):
            ok, why, _ = mb.probe_image(session, f"https://bad.example/{path}")
            self.assertFalse(ok, f"{path} passed: {why}")

    def test_choose_image_follows_the_order_and_prefers_line_size(self):
        article = make_article(1, "Story", "https://news.example/1", images=("https://feed.example/broken.jpg",), inline_images=("https://feed.example/inline.jpg",))
        page = mb.Page(LONG_TEXT, ("https://cdn.example/og.jpg?resize=1200,800", "https://cdn.example/tw.jpg"))
        tried: list[str] = []

        def probe(session: Any, url: str) -> tuple:
            tried.append(url)
            return (url != "https://feed.example/broken.jpg", "test", (1024, 683))

        url, where = mb.choose_image(None, article, page, probe)
        self.assertEqual(url, "https://cdn.example/og.jpg?resize=1024,683")
        self.assertIn("page meta", where)
        self.assertEqual(tried, ["https://feed.example/broken.jpg", "https://cdn.example/og.jpg?resize=1024,683"])
        tried.clear()
        url, where = mb.choose_image(None, article, mb.Page(), lambda s, u: (tried.append(u) or False, "HTTP 403", None))
        self.assertEqual(url, "")
        self.assertEqual(tried, ["https://feed.example/broken.jpg", "https://feed.example/inline.jpg"])
        self.assertTrue(where.startswith("none"))

    def test_a_later_image_within_line_size_beats_an_oversized_one(self):
        sizes = {
            "https://feed.example/square-1232.jpg": (1232, 1232),
            "https://cdn.example/photos/1/191:100/w_1024,c_limit/x": (1024, 536),
            "https://feed.example/thumb-500.jpg": (500, 500),
            "https://cdn.example/og-1456.jpg": (1456, 816),
        }
        probe = lambda s, url: (True, "test", sizes.get(url))  # noqa: E731
        wired = make_article(1, "WIRED-like", "https://news.example/1", images=("https://feed.example/square-1232.jpg",))
        url, _ = mb.choose_image(None, wired, mb.Page(None, ("https://cdn.example/photos/1/191:100/w_1280,c_limit/x",)), probe)
        self.assertEqual(url, "https://cdn.example/photos/1/191:100/w_1024,c_limit/x")
        ars = make_article(2, "Ars-like", "https://news.example/2", images=("https://cdn.example/og-1456.jpg", "https://feed.example/thumb-500.jpg"))
        url, _ = mb.choose_image(None, ars, mb.Page(), probe)
        self.assertEqual(url, "https://cdn.example/og-1456.jpg")  # a small thumbnail never beats a sharp photo
        decoder = make_article(3, "Decoder-like", "https://news.example/3")
        url, where = mb.choose_image(None, decoder, mb.Page(None, ("https://cdn.example/og-1456.jpg",)), probe)
        self.assertEqual(url, "https://cdn.example/og-1456.jpg")  # nothing within 1024 px: the first that loaded
        self.assertIn("page meta", where)

    def test_choose_image_stops_after_a_few_tries(self):
        article = make_article(1, "Story", "https://news.example/1", images=tuple(f"https://feed.example/{n}.jpg" for n in range(10)))
        tried: list[str] = []
        url, _ = mb.choose_image(None, article, mb.Page(), lambda s, u: (tried.append(u) or False, "HTTP 404", None))
        self.assertEqual((url, len(tried)), ("", mb.MAX_IMAGE_PROBES))

    def test_extract_article_returns_the_og_image_even_without_text(self):
        html_page = (
            b"<html><head><meta property='og:image' content='https://cdn.example/og.jpg'></head>"
            b"<body><p>Too short to be an article.</p></body></html>"
        )
        session = FakeSession({("GET", "https://news.example/"): FakeResponse(200, content=html_page, headers={"Content-Type": "text/html; charset=utf-8"}, url="https://news.example/story")})
        with contextlib.redirect_stderr(io.StringIO()):
            page = mb.extract_article(session, "https://news.example/story")
            self.assertEqual(page, mb.Page(None, ("https://cdn.example/og.jpg",)))
            self.assertEqual(mb.extract_article(FakeSession(), "https://gone.example/"), mb.Page())


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
        stories = mb.build_stories(None, None, "m", articles, [1, 2, 3, 4], wanted=2, extract=pages_of(pages))
        self.assertEqual([s.article.id for s in stories], [1, 4])

    def test_full_text_preferred_but_rank_order_kept(self):
        articles = [
            make_article(1, "Top story without a readable page", "https://paywall.example/1", description="Teaser only."),
            make_article(2, "Second story", "https://open.example/2"),
            make_article(3, "Third story here", "https://open.example/3"),
        ]
        pages = {"https://open.example/2": LONG_TEXT, "https://open.example/3": LONG_TEXT}
        stories = mb.build_stories(None, None, "m", articles, [1, 2, 3], wanted=2, extract=pages_of(pages))
        self.assertEqual([s.article.id for s in stories], [2, 3])
        stories = mb.build_stories(None, None, "m", articles[:2], [1, 2], wanted=2, extract=pages_of(pages))
        self.assertEqual([s.article.id for s in stories], [1, 2])  # nothing better: the teaser one, in rank order

    def test_backfill_when_every_shortlisted_page_is_unreadable(self):
        titles = ("Nvidia ships Rubin chips", "EU fines Meta", "OpenAI opens Tokyo office", "Robotaxis reach Bangkok", "Anthropic IPO prices")
        articles = [make_article(n, title, f"https://site{n}.example/", description="") for n, title in enumerate(titles, start=1)]
        ranked = mb.with_backfill([1, 2], articles, extra=2)
        self.assertEqual(ranked[:2], [1, 2])
        self.assertEqual(len(ranked), 4)
        self.assertEqual(len(set(ranked)), 4)
        readable = {f"https://site{n}.example/": LONG_TEXT for n in ranked[2:]}
        stories = mb.build_stories(None, None, "m", articles, ranked, wanted=2, extract=pages_of(readable))
        self.assertEqual(sorted(s.article.id for s in stories), sorted(ranked[2:]))

    def test_summary_drops_links_and_unverifiable_numbers(self):
        client = FakeOpenAI(
            summary={
                "what_happened": [
                    "Anthropic ระดมทุนได้ 81.3 พันล้านดอลลาร์จากการ IPO.",
                    "มูลค่าบริษัทแตะ 500 พันล้านดอลลาร์",          # 500 is not in the article
                    "ผู้ก่อตั้ง 7 คนขายหุ้นที่ราคา 83 ดอลลาร์",
                ],
                "why_it_matters": ["อ่านต่อที่ https://evil.example/phish", "• นี่คือก้าวสำคัญของวงการ AI."],  # a link from the model
                "takeaway": "ต้องจับตาราคาหุ้นในสัปดาห์หน้า",
            }
        )
        article = make_article(1, "Anthropic IPO", "https://tc.example/ipo")
        result = mb.summarize(client, "gpt-4o-mini", article, LONG_TEXT)
        self.assertEqual(result.basis, "article")
        self.assertEqual(result.summary.what_happened, ["Anthropic ระดมทุนได้ 81.3 พันล้านดอลลาร์จากการ IPO", "ผู้ก่อตั้ง 7 คนขายหุ้นที่ราคา 83 ดอลลาร์"])
        self.assertEqual(result.summary.why_it_matters, ["นี่คือก้าวสำคัญของวงการ AI"])
        self.assertEqual(result.summary.takeaway, "ต้องจับตาราคาหุ้นในสัปดาห์หน้า")
        prompt = client.requests[0]["messages"][1]["content"]
        self.assertIn(LONG_TEXT.strip()[:200], prompt)
        self.assertNotIn("tc.example", prompt)  # the model is never even shown the URL

    def test_summary_from_feed_text_is_labelled_and_failure_falls_back(self):
        article = make_article(1, "Teaser story", "https://x.example/1", description="Company X launched product Y today.")
        good = mb.summarize(FakeOpenAI(summary={"what_happened": ["บริษัท X เปิดตัว Y"], "why_it_matters": ["เป็นข่าวสำคัญ"], "takeaway": ""}), "m", article, None)
        self.assertEqual(good.basis, "feed")
        bad = mb.summarize(FakeOpenAI(summary={"what_happened": ["มีผู้ใช้ 9 ล้านคน"], "why_it_matters": [], "takeaway": ""}), "m", article, None)
        self.assertEqual((bad.basis, bad.sentences), ("fallback", ["Company X launched product Y today."]))
        down = mb.summarize(FakeOpenAI(fail=True), "m", article, None)
        self.assertEqual(down.basis, "fallback")
        offline = mb.summarize(None, "m", article, None)
        self.assertEqual(offline.basis, "fallback")

    def test_every_link_in_the_message_comes_from_a_feed(self):
        articles = [
            make_article(1, "Story one", "https://one.example/a?utm_source=rss", images=("https://img.example/one.jpg",)),
            make_article(2, "Story two", "https://two.example/b"),
        ]
        client = FakeOpenAI(summary={"what_happened": ["ดูได้ที่ www.fake.example", "ประโยคจริง"], "why_it_matters": ["อีกประโยค"], "takeaway": "https://evil.example"})
        session = FakeSession({("GET", "https://img.example/"): image_response(jpeg(1024, 563))})
        stories = mb.build_stories(session, client, "m", articles, [1, 2], wanted=2, extract=pages_of({a.url: LONG_TEXT for a in articles}))
        message = mb.build_flex_message(stories, context(), date(2026, 11, 20))
        feed_urls = {a.url for a in articles}
        self.assertEqual(set(all_uris(message)), feed_urls)
        image_urls = {image["url"] for image in components(message, "image")}
        self.assertEqual(image_urls, {"https://img.example/one.jpg"})
        for text in all_strings(message):
            if "http" in text or "www." in text:
                self.assertIn(text, feed_urls | image_urls)


class StructuredSummaryTests(unittest.TestCase):
    article = make_article(1, "Anthropic IPO", "https://tc.example/ipo")

    def test_the_model_is_asked_for_three_parts_under_a_strict_schema(self):
        client = FakeOpenAI(summary=SUMMARY)
        result = mb.summarize(client, "gpt-4o-mini", self.article, LONG_TEXT)
        schema = client.requests[0]["response_format"]["json_schema"]
        self.assertTrue(schema["strict"])
        self.assertEqual(set(schema["schema"]["required"]), {"what_happened", "why_it_matters", "takeaway"})
        self.assertEqual(schema["schema"]["properties"]["takeaway"], {"type": "string"})
        self.assertEqual(result.summary, summary())
        self.assertEqual(result.sentences, ["ประโยคที่หนึ่ง", "ประโยคที่สอง", "ประโยคที่สาม", "ประโยคที่สี่"])

    def test_parts_are_capped_at_two_two_and_one(self):
        data = {"what_happened": ["ก หนึ่ง", "ก สอง", "ก สาม"], "why_it_matters": ["ข หนึ่ง", "ข สอง", "ข สาม"], "takeaway": "ค หนึ่ง"}
        parsed, dropped = mb.parse_summary(data, LONG_TEXT, self.article)
        self.assertEqual((parsed.what_happened, parsed.why_it_matters, parsed.takeaway, dropped), (["ก หนึ่ง", "ก สอง"], ["ข หนึ่ง", "ข สอง"], "ค หนึ่ง", 0))

    def test_an_ungrounded_takeaway_is_dropped_but_the_summary_kept(self):
        data = {"what_happened": ["ผู้ก่อตั้ง 7 คนขายหุ้น", "ราคา IPO อยู่ที่ 83 ดอลลาร์"], "why_it_matters": [], "takeaway": "หุ้นจะขึ้นไปถึง 150 ดอลลาร์"}
        parsed, dropped = mb.parse_summary(data, LONG_TEXT, self.article)
        self.assertEqual((parsed.takeaway, parsed.why_it_matters, dropped), ("", [], 1))
        self.assertEqual(len(parsed.sentences), 2)

    def test_not_a_summary_without_what_happened_or_with_one_sentence(self):
        for data in (
            {"what_happened": [], "why_it_matters": ["ข หนึ่ง", "ข สอง"], "takeaway": "ค หนึ่ง"},
            {"what_happened": ["ก หนึ่ง"], "why_it_matters": [], "takeaway": ""},
            {"what_happened": "ก หนึ่ง", "why_it_matters": None, "takeaway": 7},
            {"sentences": ["the old shape", "is not read"]},
            ["not", "an object"],
            None,
        ):
            self.assertIsNone(mb.parse_summary(data, LONG_TEXT, self.article)[0], data)
        self.assertEqual(mb.summarize(FakeOpenAI(summary={"sentences": ["a", "b"]}), "m", self.article, LONG_TEXT).basis, "fallback")


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
    HERO = "https://cdn.example/hero.jpg?resize=1024,563"

    def full_message(self) -> dict:
        events = [mb.Event("", "", "Holiday")] + [mb.Event(f"{9 + i:02d}:00", f"{10 + i:02d}:00", f"Meeting {i}") for i in range(7)]
        stories = [story(1, "Story one", "https://one.example/a", image=self.HERO), story(2, "Story two", "https://two.example/b")]
        return mb.build_flex_message(stories, context(events, ["TripPlanner", "Jarvis"]), date(2026, 11, 20))

    def test_full_briefing_is_valid(self):
        message = self.full_message()
        self.assertEqual(mb.validate_flex(message), [])
        self.assertEqual(len(message["contents"]["contents"]), 3)
        self.assertTrue(message["altText"].startswith("☀️ Jarvis Morning Briefing — "))
        self.assertIn("20 November 2026", message["altText"])
        json.loads(json.dumps(message, ensure_ascii=False))  # serialisable as sent

    def test_every_colour_is_from_the_editorial_palette(self):
        palette = set(mb.COLORS.values())
        self.assertEqual(
            {key: mb.COLORS[key] for key in ("card", "callout", "ink", "body", "meta", "crimson", "pill", "hairline")},
            {"card": "#FFFFFF", "callout": "#F5F5F7", "ink": "#1D1D1F", "body": "#3A3A3C", "meta": "#6E6E73", "crimson": "#FA243C", "pill": "#FFF0F2", "hairline": "#E5E5EA"},
        )

        def colours(node: Any):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key.lower().endswith("color"):
                        yield value
                    yield from colours(value)
            elif isinstance(node, list):
                for value in node:
                    yield from colours(value)

        used = set(colours(self.full_message()))
        self.assertTrue(used <= palette, used - palette)
        self.assertTrue({"#FFFFFF", "#F5F5F7", "#1D1D1F", "#FA243C", "#FFF0F2", "#E5E5EA"} <= used)

    def test_cover_masthead_index_and_context(self):
        cover = self.full_message()["contents"]["contents"][0]
        body = cover["body"]["contents"]
        masthead, thai, english = body[0], body[1], body[2]
        self.assertEqual(masthead["text"].replace(" ", "").replace(" ", " "), "JARVIS DAILY")
        self.assertEqual((masthead["weight"], masthead["color"], masthead["size"]), ("bold", "#FA243C", "xs"))
        self.assertEqual((thai["text"], thai["weight"], thai["color"]), ("วันศุกร์ที่ 20 พฤศจิกายน 2569", "bold", "#1D1D1F"))
        self.assertEqual((english["text"], english["color"]), ("Friday, 20 November 2026 · Bangkok", "#6E6E73"))
        index = [b for b in components(cover, "box") if b.get("backgroundColor") == "#F5F5F7" and b.get("layout") == "horizontal"]
        self.assertEqual(len(index), 2)
        for number, card in zip(("01", "02"), index):
            self.assertEqual(card["cornerRadius"], "md")
            numeral, words = card["contents"]
            self.assertEqual((numeral["text"], numeral["weight"], numeral["color"]), (number, "bold", "#FA243C"))
            self.assertEqual([t["text"] for t in words["contents"]], [f"Story {'one' if number == '01' else 'two'}", "TechCrunch"])
        context_box = body[-1]
        self.assertEqual((context_box["backgroundColor"], context_box["cornerRadius"]), ("#F5F5F7", "md"))
        text = json.dumps(context_box, ensure_ascii=False)
        self.assertIn("ตารางวันนี้", text)
        self.assertIn("และอีก 3 นัดหมาย", text)  # 8 events, 5 shown
        self.assertIn("TripPlanner · Jarvis", text)
        self.assertEqual(cover["hero"]["backgroundColor"], "#FA243C")  # the accent rule on top

    def test_news_card_with_a_hero_image(self):
        news = self.full_message()["contents"]["contents"][1]
        self.assertEqual(
            news["hero"],
            {"type": "image", "url": self.HERO, "size": "full", "aspectRatio": "20:11", "aspectMode": "cover", "action": {"type": "uri", "label": "อ่านข่าวเต็ม", "uri": "https://one.example/a"}},
        )
        meta_row, headline, separator, blocks, note = news["body"]["contents"]
        pill, source = meta_row["contents"]
        self.assertEqual((pill["backgroundColor"], pill["contents"][0]["text"], pill["contents"][0]["color"]), ("#FFF0F2", "TECH & AI", "#FA243C"))
        self.assertTrue(source["text"].startswith("TechCrunch · ") and source["color"] == "#6E6E73" and source["align"] == "end")
        self.assertEqual((headline["text"], headline["weight"], headline["color"], headline["wrap"]), ("Story one", "bold", "#1D1D1F", True))
        self.assertIn(headline["size"], ("lg", "xl"))
        self.assertEqual((separator["type"], separator["color"]), ("separator", "#E5E5EA"))
        self.assertEqual(blocks["spacing"], "md")
        what, why, takeaway = blocks["contents"]
        self.assertEqual([t["text"] for t in what["contents"]], ["เกิดอะไรขึ้น", "ประโยคที่หนึ่ง ประโยคที่สอง"])
        self.assertEqual([t["text"] for t in why["contents"]], ["ทำไมถึงสำคัญ", "ประโยคที่สาม"])
        self.assertEqual([t["text"] for t in takeaway["contents"]], ["สิ่งที่น่าจับตา", "ประโยคที่สี่"])
        self.assertEqual((takeaway["backgroundColor"], takeaway["cornerRadius"], takeaway["paddingAll"]), ("#F5F5F7", "md", "md"))
        for block in (what, why, takeaway):
            label, paragraph = block["contents"]
            self.assertEqual((label["weight"], label["color"]), ("bold", "#FA243C"))
            self.assertEqual((paragraph["size"], paragraph["wrap"]), ("sm", True))
        self.assertEqual(what["contents"][1]["color"], "#3A3A3C")
        self.assertEqual(note["text"], mb.BASIS_NOTE["article"])
        button = news["footer"]["contents"][0]
        self.assertEqual((button["style"], button["color"], button["height"]), ("primary", "#1D1D1F", "sm"))
        self.assertEqual(button["action"], {"type": "uri", "label": "อ่านข่าวเต็ม", "uri": "https://one.example/a"})

    def test_news_card_without_an_image_gets_the_accent_bar(self):
        news = self.full_message()["contents"]["contents"][2]
        self.assertEqual(news["hero"], {"type": "box", "layout": "vertical", "height": "6px", "backgroundColor": "#FA243C", "contents": []})
        self.assertEqual(list(components(news, "image")), [])
        bad = mb.build_flex_message([story(1, "S", "https://one.example/a", image="http://plain.example/x.jpg")], context(), date(2026, 11, 20))
        self.assertEqual(bad["contents"]["contents"][1]["hero"]["type"], "box")  # never an image LINE would not load
        self.assertEqual(mb.validate_flex(bad), [])

    def test_a_summary_without_why_or_takeaway_shows_only_what_it_has(self):
        parts = mb.Summary(["Company X launched product Y today."])
        message = mb.build_flex_message([story(1, "S", "https://one.example/a", parts=parts, basis="fallback")], context(), date(2026, 11, 20))
        self.assertEqual(mb.validate_flex(message), [])
        blocks = message["contents"]["contents"][1]["body"]["contents"][3]["contents"]
        self.assertEqual(len(blocks), 1)
        self.assertNotIn("สิ่งที่น่าจับตา", json.dumps(message, ensure_ascii=False))
        self.assertIn(mb.BASIS_NOTE["fallback"], json.dumps(message, ensure_ascii=False))

    def test_compact_cover_without_context(self):
        message = mb.build_flex_message([story(1, "Only story", "https://one.example/a")], context(calendar_status="skipped"), date(2026, 11, 20))
        self.assertEqual(mb.validate_flex(message), [])
        cover = json.dumps(message["contents"]["contents"][0], ensure_ascii=False)
        self.assertNotIn("ตารางวันนี้", cover)
        self.assertNotIn("โฟกัสตอนนี้", cover)
        self.assertIn("ข่าวเด่นวันนี้", cover)
        self.assertIn("Only story", cover)

    def test_empty_calendar_shows_only_with_projects(self):
        quiet = json.dumps(mb.build_flex_message([story(1, "S", "https://one.example/a")], context([]), date(2026, 11, 20)), ensure_ascii=False)
        self.assertNotIn("ตารางวันนี้", quiet)  # connected, nothing today, nothing else: no box at all
        busy = mb.build_flex_message([story(1, "S", "https://one.example/a")], context([], ["Jarvis"]), date(2026, 11, 20))
        self.assertEqual(mb.validate_flex(busy), [])
        text = json.dumps(busy, ensure_ascii=False)
        self.assertIn("ไม่มีนัดหมายวันนี้", text)
        self.assertIn("โฟกัสตอนนี้", text)

    def test_no_stories_but_events(self):
        message = mb.build_flex_message([], context([mb.Event("09:00", "10:00", "Stand-up")]), date(2026, 11, 20))
        self.assertEqual(mb.validate_flex(message), [])
        self.assertEqual(len(message["contents"]["contents"]), 1)
        cover = json.dumps(message, ensure_ascii=False)
        self.assertIn("วันนี้ยังไม่พบข่าวเด่นด้าน Tech & AI", cover)
        self.assertIn("Stand-up", cover)
        self.assertNotIn("footer", message["contents"]["contents"][0])

    def test_worst_case_sizes_stay_under_the_limits(self):
        huge_title = "ก" * 300
        parts = mb.Summary(["ข" * 400] * 2, ["ข" * 400] * 2, "ข" * 400)
        image = "https://e.example/" + "i" * (mb.LINE_MAX_IMAGE_URL - len("https://e.example/"))
        stories = [mb.Story(make_article(i, huge_title, "https://e.example/" + "p" * 900), parts, "article", image) for i in (1, 2)]
        events = [mb.Event("09:00", "10:00", "ค" * 70) for _ in range(25)]
        message = mb.build_flex_message(stories, context(events, ["ง" * 60] * 3), date(2026, 11, 20))
        self.assertEqual(mb.validate_flex(message), [])
        for bubble in message["contents"]["contents"]:
            self.assertLess(mb.json_bytes(bubble), mb.LINE_MAX_BUBBLE_BYTES)
        self.assertLess(mb.json_bytes(message["contents"]), mb.LINE_MAX_CAROUSEL_BYTES)

    def test_validator_catches_each_rule(self):
        def broken(change: Callable[[dict], None]) -> list[str]:
            message = mb.build_flex_message([story(1, "S", "https://one.example/a", image=self.HERO)], context(), date(2026, 11, 20))
            change(message)
            return mb.validate_flex(message)

        news = lambda m: m["contents"]["contents"][1]  # noqa: E731
        action = lambda m: news(m)["footer"]["contents"][0]["action"]  # noqa: E731
        cases = {
            "empty text": lambda m: news(m)["body"]["contents"].__setitem__(1, {"type": "text", "text": "  "}),
            "bad colour": lambda m: news(m)["styles"]["body"].__setitem__("backgroundColor", "white"),
            "long label": lambda m: action(m).__setitem__("label", "อ่านข่าวเต็มที่หน้าเว็บของสำนักข่าว"),
            "javascript uri": lambda m: action(m).__setitem__("uri", "javascript:alert(1)"),
            "http image": lambda m: news(m)["hero"].__setitem__("url", "http://cdn.example/hero.jpg"),
            "long image url": lambda m: news(m)["hero"].__setitem__("url", "https://cdn.example/" + "i" * 2000),
            "non-ascii image url": lambda m: news(m)["hero"].__setitem__("url", "https://cdn.example/ภาพ.jpg"),
            "tall aspect ratio": lambda m: news(m)["hero"].__setitem__("aspectRatio", "1:4"),
            "aspect ratio format": lambda m: news(m)["hero"].__setitem__("aspectRatio", "wide"),
            "aspect mode": lambda m: news(m)["hero"].__setitem__("aspectMode", "stretch"),
            "hero type": lambda m: news(m).__setitem__("hero", {"type": "picture"}),
            "too many bubbles": lambda m: m["contents"]["contents"].extend([news(m)] * 12),
            "alt text": lambda m: m.__setitem__("altText", "x" * 1501),
            "box layout": lambda m: news(m)["body"].__setitem__("layout", "grid"),
            "huge bubble": lambda m: news(m)["body"]["contents"].append({"type": "text", "text": "x" * 31000}),
            "not flex": lambda m: m.__setitem__("type", "text"),
        }
        for name, change in cases.items():
            self.assertTrue(broken(change), f"validate_flex missed: {name}")

    def test_text_fallback_fits_a_text_message(self):
        stories = [mb.Story(make_article(1, "T", "https://one.example/a"), mb.Summary(["ข" * 6000]), "article"), story(2, "U", "https://two.example/b")]
        fallback = mb.text_fallback(stories, context([mb.Event("09:00", "10:00", "Stand-up")]), date(2026, 11, 20))
        self.assertEqual(fallback["type"], "text")
        self.assertLessEqual(len(fallback["text"]), mb.LINE_MAX_TEXT_MESSAGE)
        self.assertIn("09:00–10:00 Stand-up", fallback["text"])
        short = mb.text_fallback([story(1, "U", "https://two.example/b")], context(), date(2026, 11, 20))["text"]
        for line in ("01. U (TechCrunch)", "เกิดอะไรขึ้น: ประโยคที่หนึ่ง ประโยคที่สอง", "ทำไมถึงสำคัญ: ประโยคที่สาม", "สิ่งที่น่าจับตา: ประโยคที่สี่", "https://two.example/b"):
            self.assertIn(line, short)

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
                {
                    "title": "EU passes AI liability rules",
                    "link": "https://tc.example/eu",
                    "published": now - timedelta(hours=3),
                    "description": "Rules.",
                    "extra": "<media:content url='https://img.example/eu-hero.jpg' medium='image' type='image/jpeg' width='1024' height='576'/>",
                },
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
                ("GET", "https://img.example/"): image_response(jpeg(1024, 576)),
                ("POST", "https://api.line.me/"): line,
            },
            default=FakeResponse(503),
        )
        self.client = FakeOpenAI(selection={"ids": [2, 1]}, summary=SUMMARY)

    def run_main(self, argv: list[str], env: dict) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(mb.requests, "Session", return_value=self.session), mock.patch.object(
            mb, "make_openai_client", return_value=self.client
        ), mock.patch.object(mb, "extract_article", return_value=mb.Page(LONG_TEXT)), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
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
        first, second = message["contents"]["contents"][1:]
        self.assertEqual(first["footer"]["contents"][0]["action"]["uri"], "https://tc.example/eu")  # the model's order
        self.assertEqual((first["hero"]["type"], first["hero"]["url"]), ("image", "https://img.example/eu-hero.jpg"))  # the feed's media:content
        self.assertEqual(second["hero"]["type"], "box")  # no image anywhere: the accent bar
        self.assertIn("hero image: https://img.example/eu-hero.jpg", out)
        self.assertIn("เกิดอะไรขึ้น:", out)
        probes = [url for method, url, _ in self.session.calls if url.startswith("https://img.example/")]
        self.assertEqual(probes, ["https://img.example/eu-hero.jpg"])

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
