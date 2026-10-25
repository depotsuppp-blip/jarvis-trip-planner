"""
A web page reader MCP server for Jarvis (see mcp_client.py): give it a URL, get
the page back as readable text. No API key, no account.

    fetch_url   downloads one public page (http or https) and returns its title
                and main content - headings, paragraphs, lists, tables and
                links - with scripts, menus, cookie banners and ads stripped.
                'url' is required; 'max_length' (characters per reply, default
                6000), 'start_index' (to continue a long page) and 'raw' (the
                HTML source instead) are optional. PDF, plain-text, JSON and
                XML addresses are read too.

It does the job of the reference server `mcp-server-fetch` (same arguments),
written for this project instead of installed: that one needs the `mcp` SDK
and, for its best output, Node.js, and it does not check where a URL points.

Jarvis starts it by itself, like the other servers that ship with it (see
mcp_client.py). To poke at it by hand:

    python mcp_fetch.py

then paste these lines, one at a time:

    {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"hand","version":"0"}}}
    {"jsonrpc":"2.0","id":2,"method":"tools/list"}
    {"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"fetch_url","arguments":{"url":"https://example.com"}}}

SAFETY
------
* Public addresses only. The host name is resolved first and EVERY address it
  resolves to must be public: localhost, the LAN (10/8, 172.16/12, 192.168/16),
  link-local (169.254/16, where cloud metadata lives), carrier-grade NAT and the
  other reserved ranges are refused - so is a redirect that leads to one. The
  connection then goes to the address that was checked (the TLS certificate is
  still verified against the name), so a name that resolves somewhere else a
  moment later (DNS rebinding) cannot slip past the check. This matters: the
  web chat lets any signed-in person hand Jarvis a URL, and a page Jarvis reads
  can contain text asking it to fetch something else. JARVIS_FETCH_ALLOW_PRIVATE=1
  lifts the rule, for an owner who wants Jarvis to read pages on their own network.
* robots.txt is honoured, as the reference server does for fetches the model
  starts (user-agent token "JarvisFetch"). A robots.txt that cannot be read does
  not block the page. JARVIS_FETCH_IGNORE_ROBOTS=1 turns the check off.
* Bounded: 5 MB per download, 5 redirects, a 24-second deadline for the whole
  call (mcp_client.py allows 30), and each reply fits what the client keeps of a
  tool result. No cookies, no logins, no JavaScript: a page that only draws itself
  in a browser comes back nearly empty, and the reply says so.

Failures (no such site, HTTP errors, a refused address, a timeout) come back as
a tool result with isError set, so the model can say what happened. Everything
this returns reaches the model labelled as external data (mcp_client.py).
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import re
import socket
import sys
import threading
import time
import urllib.robotparser
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

from mcp_stdio import Server, ToolError, env_on

USER_AGENT = "Mozilla/5.0 (compatible; JarvisFetch/1.0; personal assistant)"
ROBOTS_AGENT = "JarvisFetch"

DEFAULT_LENGTH = 6000
MIN_LENGTH = 500
# One reply (header + text + note) must fit what mcp_client.py keeps of a tool
# result (its MAX_RESULT_CHARS, 8000). The header is at most ~600 characters.
MAX_LENGTH = 7000
MAX_DOWNLOAD_BYTES = 5_000_000
MAX_ROBOTS_BYTES = 500_000
MAX_REDIRECTS = 5
CONNECT_TIMEOUT = 6.0
READ_TIMEOUT = 12.0
DEADLINE_SECONDS = 24.0
ROBOTS_SECONDS = 5.0
MAX_URL_CHARS = 2000
MAX_TEXT_CHARS = 400_000     # what is kept of one page's text, for paging through it
THIN_PAGE_CHARS = 200
CACHE_SECONDS = 600.0        # a page read in parts is downloaded once
CACHE_ENTRIES = 12
ROBOTS_CACHE_SECONDS = 3600.0
MAX_DEPTH = 300              # deeper HTML is flattened to its text
MAX_TABLE_ROWS = 60
MAX_TABLE_COLS = 12
MAX_LINK_CHARS = 300

_REDIRECTS = (301, 302, 303, 307, 308)
_WS = re.compile(r"[\s ​]+")


# =====================================================================
# Where a URL may go
# =====================================================================


def normalize_url(raw: Any) -> str:
    """The URL the model gave -> a clean absolute http(s) URL, or ToolError."""
    if not isinstance(raw, str) or not raw.strip():
        raise ToolError("Give the 'url' of the page to read, like https://example.com/page.")
    text = raw.strip().strip("<>\"'")
    if len(text) > MAX_URL_CHARS:
        raise ToolError("That URL is too long.")
    if re.search(r"[\x00-\x1f\x7f]", text):
        raise ToolError("That URL contains invalid characters.")
    if "://" not in text:
        if re.match(r"^(javascript|mailto|tel|data|file|ftp):", text, re.IGNORECASE):
            raise ToolError("Only http and https web pages can be read.")
        text = "https://" + text.lstrip("/")  # "example.com/page"
    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ToolError("Only http and https web pages can be read.")
    if parts.username is not None or parts.password is not None:
        raise ToolError("URLs with a user name or password in them are not fetched.")
    host = parts.hostname
    if not host:
        raise ToolError("That URL has no site name in it.")
    try:
        port = parts.port
    except ValueError:
        raise ToolError("That URL has an invalid port number.") from None
    if not _is_ip(host):
        try:
            host = host.rstrip(".").encode("idna").decode("ascii")  # a Thai or other non-Latin domain
        except UnicodeError:
            raise ToolError("That site name is not valid.") from None
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc += f":{port}"
    path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parts.query, safe="=&%:@!$'()*+,;/?-._~")
    return urlunsplit((scheme, netloc, path, query, ""))


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def is_public_address(address: str) -> bool:
    """True for an address on the public internet; False for loopback, LAN, link-local, reserved..."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = ip.ipv4_mapped or ip.sixtofour  # ::ffff:127.0.0.1 is 127.0.0.1
        if embedded is not None:
            ip = embedded
    return ip.is_global and not ip.is_multicast


def resolve_addresses(host: str, port: int) -> list[str]:
    """Every address `host` resolves to - all of them public, unless private ones are allowed."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        raise ToolError(f"Could not find the site {host} (the name does not resolve).") from None
    addresses: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise ToolError(f"Could not find the site {host}.")
    if not env_on("JARVIS_FETCH_ALLOW_PRIVATE") and not all(is_public_address(a) for a in addresses):
        raise ToolError(
            f"{host} points to a private or local network address, and this tool only reads public web pages."
        )
    addresses.sort(key=lambda a: ":" in a)  # IPv4 first: more often reachable from a home network
    return addresses


# =====================================================================
# Downloading
# =====================================================================


@dataclass
class Response:
    url: str
    status: int
    reason: str
    headers: dict[str, str]  # lower-case names
    body: bytes
    truncated: bool


def _request(url: str, deadline: float, max_bytes: int) -> Response:
    """One GET of `url` (redirects are not followed here), connected to an address that was checked."""
    import certifi
    import urllib3
    from urllib3.exceptions import (
        ConnectTimeoutError,
        HTTPError,
        MaxRetryError,
        NewConnectionError,
        ReadTimeoutError,
        SSLError,
    )

    parts = urlsplit(url)
    https = parts.scheme == "https"
    host = parts.hostname or ""
    port = parts.port or (443 if https else 80)
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    headers = {
        "Host": parts.netloc,
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,application/pdf;q=0.7,*/*;q=0.5",
        "Accept-Language": "en,th;q=0.8,*;q=0.5",
        "Accept-Encoding": "gzip, deflate",
    }

    failure = ""
    for address in resolve_addresses(host, port)[:4]:
        remaining = deadline - time.monotonic()
        if remaining < 1.0:
            break
        timeout = urllib3.Timeout(connect=min(CONNECT_TIMEOUT, remaining), read=min(READ_TIMEOUT, remaining))
        if https:
            pool: Any = urllib3.HTTPSConnectionPool(
                address, port, timeout=timeout, retries=False, maxsize=1,
                cert_reqs="CERT_REQUIRED", ca_certs=certifi.where(),
                server_hostname=host, assert_hostname=host,
            )
        else:
            pool = urllib3.HTTPConnectionPool(address, port, timeout=timeout, retries=False, maxsize=1)
        try:
            response = pool.urlopen(
                "GET", target, headers=headers, redirect=False, retries=False,
                preload_content=False, decode_content=True, assert_same_host=False,
            )
        except MaxRetryError as exc:  # urllib3 wraps some errors even with retries off
            pool.close()
            reason = exc.reason
            if isinstance(reason, SSLError):
                raise ToolError(f"{host} has an invalid or untrusted security certificate, so the page was not read.") from None
            failure = type(reason).__name__ if reason else "connection failed"
            continue
        except SSLError:
            pool.close()
            raise ToolError(f"{host} has an invalid or untrusted security certificate, so the page was not read.") from None
        except (NewConnectionError, ConnectTimeoutError) as exc:
            pool.close()
            failure = type(exc).__name__
            continue
        except ReadTimeoutError:
            pool.close()
            raise ToolError("The site took too long to answer. Try again later.") from None
        except (HTTPError, OSError) as exc:
            pool.close()
            raise ToolError(f"Could not read the page ({type(exc).__name__}).") from None
        try:
            if response.status in _REDIRECTS:
                body, truncated = b"", False
            else:
                body, truncated = _read_body(response, deadline, max_bytes)
        finally:
            response.release_conn()
            pool.close()
        return Response(
            url, response.status, response.reason or "",
            {k.lower(): v for k, v in response.headers.items()}, body, truncated,
        )
    if failure:
        raise ToolError(f"Could not connect to {host}.")
    raise ToolError("The site took too long to answer. Try again later.")


def _read_body(response: Any, deadline: float, max_bytes: int) -> tuple[bytes, bool]:
    """Up to `max_bytes` of the (decompressed) body, stopping at the deadline."""
    from urllib3.exceptions import HTTPError

    chunks: list[bytes] = []
    size = 0
    while True:
        if time.monotonic() > deadline:
            return b"".join(chunks), True
        try:
            chunk = response.read(65536)
        except (HTTPError, OSError):
            if size:
                return b"".join(chunks), True
            raise ToolError("The connection broke while reading the page. Try again.") from None
        if not chunk:
            return b"".join(chunks), False
        chunks.append(chunk)
        size += len(chunk)
        if size >= max_bytes:
            return b"".join(chunks)[:max_bytes], True


def download(url: str, deadline: float, max_bytes: int = MAX_DOWNLOAD_BYTES) -> Response:
    """GET `url`, following redirects - each hop is checked like the first."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        response = _request(current, deadline, max_bytes)
        location = response.headers.get("location")
        if response.status in _REDIRECTS and location:
            current = normalize_url(urljoin(current, location.strip()))
            continue
        return response
    raise ToolError("The page redirected too many times.")


# =====================================================================
# robots.txt
# =====================================================================

_lock = threading.Lock()
_robots: dict[str, tuple[float, Any]] = {}


def robots_allows(url: str, deadline: float) -> bool:
    if env_on("JARVIS_FETCH_IGNORE_ROBOTS"):
        return True
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    now = time.monotonic()
    with _lock:
        cached = _robots.get(origin)
    if cached is None or now - cached[0] > ROBOTS_CACHE_SECONDS:
        cached = (now, _load_robots(origin, min(deadline, now + ROBOTS_SECONDS)))
        with _lock:
            _robots[origin] = cached
    verdict = cached[1]
    if isinstance(verdict, bool):
        return verdict
    return robots_verdict(verdict, url)


def robots_verdict(parser: urllib.robotparser.RobotFileParser, url: str) -> bool:
    """
    parser.can_fetch(), except that the group for this agent is found by EXACT
    (case-insensitive) product token, as RFC 9309 says. The standard library
    matches by substring, so "Fetch" - an old offline reader that Wikipedia's
    robots.txt bans outright - would match "JarvisFetch" and block every page there.
    """
    if parser.disallow_all:
        return False
    if parser.allow_all:
        return True
    parsed = urlsplit(unquote(url))
    path = quote(urlunsplit(("", "", parsed.path, parsed.query, ""))) or "/"
    agent = ROBOTS_AGENT.lower()
    for entry in parser.entries:
        if any(str(name).lower() == agent for name in entry.useragents):
            return bool(entry.allowance(path))
    if parser.default_entry is not None:  # the "*" group
        return bool(parser.default_entry.allowance(path))
    return True


def _load_robots(origin: str, deadline: float) -> Any:
    """A parser, or True when there are no rules to follow (RFC 9309: a 4xx means none)."""
    try:
        response = download(origin + "/robots.txt", deadline, MAX_ROBOTS_BYTES)
    except ToolError:
        return True  # unreachable: the page request will meet the same problem and report it
    if response.status != 200 or not response.body:
        return True
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(response.body.decode("utf-8", errors="replace").splitlines())
    return parser


# =====================================================================
# Turning a response into text
# =====================================================================


def _media_type(headers: dict[str, str]) -> tuple[str, Optional[str]]:
    value = headers.get("content-type", "")
    media = value.split(";", 1)[0].strip().lower()
    match = re.search(r"charset=[\"']?([\w.:-]+)", value, re.IGNORECASE)
    return media, (match.group(1) if match else None)


def decode_text(body: bytes, charset: Optional[str]) -> str:
    if charset:
        try:
            return body.decode(charset, errors="replace")
        except LookupError:
            pass
    try:
        return body.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(body[:200_000]).best()
        if best is not None and best.encoding:
            return body.decode(best.encoding, errors="replace")
    except Exception:  # noqa: BLE001 - a guess that fails falls through to latin-1
        pass
    return body.decode("latin-1", errors="replace")


def page_text(response: Response, raw: bool) -> tuple[str, str, str]:
    """(title, text, kind) for a successful response; ToolError for a type it cannot read."""
    media, charset = _media_type(response.headers)
    body = response.body
    head = body.lstrip()[:200].lower()
    looks_html = head.startswith((b"<!doctype html", b"<html")) or (not media and head.startswith(b"<"))
    if media in ("text/html", "application/xhtml+xml") or looks_html:
        if raw:
            return "", decode_text(body, charset), "HTML source"
        title, text = html_to_text(body, charset, response.url)
        return title, text, "html"
    if media == "application/pdf" or body[:5] == b"%PDF-":
        return "", pdf_text(body), "PDF"
    if media.startswith("text/") or media.endswith(("+json", "/json", "+xml", "/xml")) or media in (
        "application/javascript", "application/x-ndjson", "application/rss+xml", "application/atom+xml",
    ):
        text = decode_text(body, charset)
        if media.endswith("json") and len(text) < 1_000_000:
            with contextlib.suppress(ValueError):
                text = json.dumps(json.loads(text), indent=1, ensure_ascii=False)
        return "", text, media
    raise ToolError(
        f"That address is a {media or 'binary'} file, not a page this tool can read "
        "(it reads web pages, PDFs and text)."
    )


def pdf_text(body: bytes) -> str:
    # PyMuPDF prints some warnings to stdout, which carries protocol messages
    # only, so its chatter goes to stderr.
    with contextlib.redirect_stdout(sys.stderr):
        try:
            import pymupdf  # type: ignore[import-not-found]
        except ImportError:
            try:
                import fitz as pymupdf  # type: ignore[import-not-found,no-redef]
            except ImportError:
                raise ToolError("Reading PDFs needs PyMuPDF on this machine (pip install pymupdf).") from None
        try:
            document = pymupdf.open(stream=body, filetype="pdf")
        except Exception:  # noqa: BLE001
            raise ToolError("The PDF could not be opened; it may be damaged.") from None
        parts: list[str] = []
        total = 0
        with document:
            if document.needs_pass:
                raise ToolError("The PDF is password-protected.")
            for number, page in enumerate(document, start=1):
                text = page.get_text().strip()
                if text:
                    parts.append(f"[Page {number}]\n{text}")
                    total += len(text)
                if total > MAX_TEXT_CHARS:
                    break
    if not parts:
        return "(This PDF has no text layer - it is probably scanned images.)"
    return "\n\n".join(parts)


# ---- HTML ------------------------------------------------------------

# Never content, anywhere in the page.
_DROP_EVERYWHERE = (
    "script", "style", "noscript", "template", "svg", "canvas", "iframe", "object",
    "embed", "link", "meta", "source", "track", "map", "audio", "video", "picture", "head",
)
# Inside the content: chrome, controls and asides.
_DROP_IN_CONTENT = ("nav", "footer", "aside", "form", "button", "select", "textarea", "input", "dialog", "menu")
_NOISE_ROLES = {
    "navigation", "banner", "contentinfo", "complementary", "search", "dialog",
    "alertdialog", "menu", "menubar", "toolbar", "tablist",
}
# Matched against the parts of a class or id ("cookie-banner" -> "cookie", "banner").
_NOISE_WORDS = {
    "cookie", "cookies", "consent", "gdpr", "newsletter", "subscribe", "subscription",
    "advert", "advertisement", "ad", "ads", "adslot", "sponsor", "sponsored", "promo",
    "popup", "modal", "share", "sharing", "social", "breadcrumb", "breadcrumbs",
    "related", "recommended", "comments", "sidebar", "outbrain", "taboola", "paywall",
    "navbox", "navbar", "navigation", "toolbar",
    # MediaWiki (Wikipedia, Fandom...): what the site itself leaves out of a printout
    "noprint", "editsection", "catlinks", "printfooter",
}
_NOISE_TOKENS = {"sr-only", "visually-hidden", "screen-reader-text", "skip-link", "no-print", "d-print-none"}
_LANGUAGE_MENU_LINKS = 5     # a block of at least this many links, nearly all to translations
_BLOCKS = {
    "p", "div", "section", "article", "main", "header", "footer", "aside", "figure",
    "figcaption", "dl", "dt", "dd", "details", "summary", "address", "center",
    "fieldset", "hgroup", "body", "html", "caption",
}
_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


def _load_bs4() -> Any:
    try:
        import bs4  # type: ignore[import-not-found]
    except ImportError:
        raise ToolError("Reading web pages needs BeautifulSoup on this machine (pip install beautifulsoup4).") from None
    return bs4


def _parser_name() -> str:
    """lxml when it is installed (ddgs brings it; faster and more forgiving), else Python's own parser."""
    import importlib.util

    return "lxml" if importlib.util.find_spec("lxml") is not None else "html.parser"


def _clean(text: str) -> str:
    return _WS.sub(" ", text or "").strip()


def html_to_text(body: bytes, charset: Optional[str], base_url: str) -> tuple[str, str]:
    """(title, readable text) of an HTML page."""
    bs4 = _load_bs4()
    with warnings_quiet():
        soup = bs4.BeautifulSoup(body, _parser_name(), from_encoding=charset)

    title = _clean(soup.title.get_text()) if soup.title else ""
    if not title:
        og = soup.find("meta", attrs={"property": "og:title"})
        title = _clean(str(og.get("content", ""))) if og else ""
    summary = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    summary_text = _clean(str(summary.get("content", ""))) if summary else ""
    base = soup.find("base", href=True)
    if base is not None:
        base_url = urljoin(base_url, str(base["href"]))

    for tag in soup.find_all(_DROP_EVERYWHERE):
        if not getattr(tag, "decomposed", False):
            tag.decompose()

    root, root_is_page = _content_root(soup)
    _strip_noise(root, root_is_page)
    text = _Renderer(bs4, base_url).render(root)
    if len(text) < THIN_PAGE_CHARS and summary_text and summary_text not in text:
        text = (summary_text + "\n\n" + text).strip()
    return title[:200], text[:MAX_TEXT_CHARS]


@contextlib.contextmanager
def warnings_quiet():
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # bs4's "looks like a URL / XML" guesses
        yield


def _content_root(soup: Any) -> tuple[Any, bool]:
    """The element holding the page's main content, and whether it is the whole page."""
    page = soup.body or soup
    page_size = len(page.get_text(" ", strip=True))

    def big_enough(element: Any) -> bool:
        if element is None:
            return False
        size = len(element.get_text(" ", strip=True))
        return size >= 200 and (size >= 0.25 * page_size or size >= 2000)

    articles = soup.find_all("article")
    options = [articles[0]] if len(articles) == 1 else []
    options += [soup.find("main"), soup.find(attrs={"role": "main"})]
    for element in options:
        if big_enough(element):
            return element, False
    return page, True


def _strip_noise(root: Any, root_is_page: bool) -> None:
    """Removes menus, banners, hidden parts... - never one that holds most of the text."""
    root_size = len(root.get_text(" ", strip=True)) or 1
    victims = [element for element in root.find_all(True) if _is_noise(element, root_is_page)]
    for element in victims:
        if getattr(element, "decomposed", False):
            continue  # inside one removed already
        if len(element.get_text(" ", strip=True)) > 0.5 * root_size:
            continue  # a site that wraps its whole article in a <form> or a "has-sidebar" div
        element.decompose()


def _is_noise(element: Any, root_is_page: bool) -> bool:
    name = element.name
    if name in _DROP_IN_CONTENT:
        return True
    if name == "header" and root_is_page and element.find_parent(["article", "main"]) is None:
        return True  # the site's header; an article's own header (its title) stays
    attrs = element.attrs or {}
    if "hidden" in attrs or str(attrs.get("aria-hidden", "")).lower() == "true":
        return True
    style = str(attrs.get("style", "")).replace(" ", "").lower()
    if "display:none" in style or "visibility:hidden" in style:
        return True
    if str(attrs.get("role", "")).lower() in _NOISE_ROLES:
        return True
    classes = attrs.get("class", [])
    tokens = [*(classes if isinstance(classes, list) else [classes]), attrs.get("id", "")]
    for token in tokens:
        lowered = str(token).lower()
        if lowered in _NOISE_TOKENS:
            return True
        if any(part in _NOISE_WORDS for part in re.split(r"[-_\s]+", lowered)):
            return True
    if name in ("ul", "ol", "div", "section"):
        # A language switcher: links to the same page in other languages carry
        # hreflang (or lang). Wikipedia's runs to 350 entries before the article.
        links = element.find_all("a", href=True, limit=60)
        if len(links) >= _LANGUAGE_MENU_LINKS:
            translated = sum(1 for link in links if link.get("hreflang") or link.get("lang"))
            if translated >= 0.8 * len(links):
                return True
    return False


class _Renderer:
    """HTML elements -> Markdown-flavoured plain text."""

    def __init__(self, bs4: Any, base_url: str) -> None:
        self.base = base_url
        self.string = bs4.NavigableString
        self.skip_strings = (bs4.Comment, bs4.Declaration, bs4.Doctype, bs4.ProcessingInstruction)

    def render(self, root: Any) -> str:
        text = self._node(root, 0, False)
        # Tidy everything except code blocks, whose spacing is the content.
        parts = re.split(r"(```\n.*?\n```)", text, flags=re.DOTALL)
        for index in range(0, len(parts), 2):
            part = re.sub(r"[ \t]+\n", "\n", parts[index])
            # Stray indents from inline whitespace go; a nested list item's indent stays.
            # (The lookahead also refuses a space, or the regex would backtrack one space short.)
            part = re.sub(r"(?m)^[ \t]+(?![ \t]|(?:[-*]|\d+\.) )", "", part)
            parts[index] = re.sub(r"\n{3,}", "\n\n", part)
        return "".join(parts).strip()

    def _children(self, node: Any, depth: int, pre: bool) -> str:
        return "".join(self._node(child, depth + 1, pre) for child in node.children)

    def _node(self, node: Any, depth: int, pre: bool) -> str:
        if isinstance(node, self.string):
            if isinstance(node, self.skip_strings):
                return ""
            return str(node) if pre else _WS.sub(" ", str(node))
        name = node.name
        if depth > MAX_DEPTH:
            return " " + _clean(node.get_text(" ")) + " "
        if name == "br":
            return "\n"
        if name == "hr":
            return "\n\n"
        if name == "img":
            return ""
        if name == "pre":
            code = node.get_text().strip("\n")
            return f"\n\n```\n{code}\n```\n\n" if code.strip() else ""
        if name == "code" and not pre:
            code = _clean(node.get_text())
            return f"`{code}`" if code else ""
        if name in ("ul", "ol"):
            return self._list(node, depth, pre)
        if name == "table":
            return self._table(node, depth, pre)

        inner = self._children(node, depth, pre)
        if name in _HEADINGS:
            heading = _clean(inner)
            return f"\n\n{'#' * int(name[1])} {heading}\n\n" if heading else ""
        if name == "a":
            return self._link(node, inner)
        if name == "blockquote":
            quoted = inner.strip()
            if not quoted:
                return ""
            return "\n\n" + "\n".join(f"> {line}" if line.strip() else ">" for line in quoted.split("\n")) + "\n\n"
        if name == "li":  # an item outside any list
            item = inner.strip()
            return f"\n- {item}\n" if item else ""
        if name == "tr":
            return "\n" + inner.strip() + "\n"
        if name in ("td", "th"):
            return " " + inner.strip() + " "
        if name in _BLOCKS:
            block = inner.strip()
            return f"\n\n{block}\n\n" if block else ""
        return inner

    def _list(self, node: Any, depth: int, pre: bool) -> str:
        ordered = node.name == "ol"
        try:
            number = int(node.get("start", 1))
        except (TypeError, ValueError):
            number = 1
        items: list[str] = []
        for child in node.children:
            if isinstance(child, self.string):
                continue
            if child.name != "li":  # sites put <div>s straight inside lists
                stray = self._node(child, depth + 1, pre).strip()
                if stray:
                    items.append(stray)
                continue
            body = re.sub(r"\n{2,}", "\n", self._children(child, depth + 1, pre).strip())
            if not body:
                continue
            marker = f"{number}." if ordered else "-"
            number += 1
            first, *rest = body.split("\n")
            items.append(f"{marker} {first}" + "".join(f"\n  {line}" for line in rest if line.strip()))
        return "\n\n" + "\n".join(items) + "\n\n" if items else ""

    def _table(self, node: Any, depth: int, pre: bool) -> str:
        rows = [row for row in node.find_all("tr") if row.find_parent("table") is node]
        cells_all = node.find_all(["td", "th"])
        layout = node.find("table") is not None or any(
            len(cell.get_text(" ", strip=True)) > 400 for cell in cells_all
        )
        if not rows or layout:  # a table used for page layout: its text, as blocks
            block = self._children(node, depth, pre).strip()
            return f"\n\n{block}\n\n" if block else ""
        lines: list[str] = []
        for row in rows[:MAX_TABLE_ROWS]:
            cells = row.find_all(["td", "th"], recursive=False)
            values = [
                _clean(self._children(cell, depth + 1, pre)).replace("|", "/")[:200]
                for cell in cells[:MAX_TABLE_COLS]
            ]
            if not any(values):
                continue
            lines.append("| " + " | ".join(values) + " |")
            if len(lines) == 1 and cells and all(cell.name == "th" for cell in cells):
                lines.append("|" + " --- |" * len(values))
        if len(rows) > MAX_TABLE_ROWS:
            lines.append(f"[... {len(rows) - MAX_TABLE_ROWS} more rows]")
        caption = node.find("caption")
        if caption is not None and _clean(caption.get_text()):
            lines.insert(0, _clean(caption.get_text()))
        return "\n\n" + "\n".join(lines) + "\n\n" if lines else ""

    def _link(self, node: Any, inner: str) -> str:
        text = _clean(inner)
        if not text:
            image = node.find("img", alt=True)
            text = _clean(str(image["alt"])) if image is not None else ""
        if not text:
            return ""
        lead = " " if inner[:1].isspace() else ""
        tail = " " if inner[-1:].isspace() else ""
        href = str(node.get("href") or "").strip()
        if not href or href.startswith("#") or re.match(r"^(javascript|mailto|tel|data):", href, re.IGNORECASE):
            return f"{lead}{text}{tail}"
        url = urljoin(self.base, href).split("#", 1)[0]
        if not re.match(r"^https?://", url, re.IGNORECASE) or len(url) > MAX_LINK_CHARS or url == self.base:
            return inner if "\n" in inner.strip() else f"{lead}{text}{tail}"
        lines = [line.strip() for line in inner.strip().split("\n") if line.strip()]
        if len(lines) > 1:
            # A "card": a whole teaser (heading, summary, date) wrapped in one link,
            # as news front pages do. The heading becomes the link, the rest follows it.
            hashes, title = re.match(r"^(#*)\s*(.*)$", lines[0]).groups()  # type: ignore[union-attr]
            heading = f"{hashes} [{title}]({url})" if hashes else f"[{title}]({url})"
            return f"\n\n{heading}\n" + " ".join(lines[1:]) + "\n\n"
        if url == text:
            return f"{lead}{url}{tail}"
        return f"{lead}[{text}]({url}){tail}"


# =====================================================================
# The tool
# =====================================================================


@dataclass
class Page:
    url: str          # where the content came from, after redirects
    title: str
    text: str
    kind: str
    truncated: bool   # the download stopped at the size or time limit
    at: float


_pages: "OrderedDict[tuple[str, bool], Page]" = OrderedDict()


def _cached(url: str, raw: bool) -> Optional[Page]:
    with _lock:
        page = _pages.get((url, raw))
        if page is not None and time.monotonic() - page.at < CACHE_SECONDS:
            _pages.move_to_end((url, raw))
            return page
        _pages.pop((url, raw), None)
        return None


def _remember(url: str, raw: bool, page: Page) -> None:
    with _lock:
        _pages[(url, raw)] = page
        while len(_pages) > CACHE_ENTRIES:
            _pages.popitem(last=False)


def _status_message(response: Response) -> str:
    status = response.status
    if status == 404 or status == 410:
        return f"The page was not found (HTTP {status}). Check the address."
    if status in (401, 403):
        return (
            f"The site refused to show this page to an automated reader (HTTP {status}). "
            "It may need a login, or it blocks tools like this one; the person can open it in a browser."
        )
    if status == 429:
        return "The site is limiting requests right now (HTTP 429). Try again in a minute."
    if status >= 500:
        return f"The site had an error (HTTP {status}). Try again later."
    return f"The site answered HTTP {status} {response.reason}".rstrip() + ", so there is no page to read."


def load_page(url: str, raw: bool) -> Page:
    deadline = time.monotonic() + DEADLINE_SECONDS - 1.0
    if not robots_allows(url, deadline):
        raise ToolError(
            "This site's robots.txt asks automated tools not to read this page, so it was not fetched. "
            "The person can open it in their browser."
        )
    response = download(url, deadline)
    if response.status >= 400:
        raise ToolError(_status_message(response))
    if response.status in _REDIRECTS:
        raise ToolError("The site redirected without saying where to.")
    title, text, kind = page_text(response, raw)
    return Page(response.url, title, text[:MAX_TEXT_CHARS], kind, response.truncated, time.monotonic())


def _with_deadline(work: Callable[[], Page], seconds: float) -> Page:
    """Runs `work` on its own thread so nothing (a slow DNS answer) holds the server past the deadline."""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["page"] = work()
        except BaseException as exc:  # noqa: BLE001 - handed to the caller below
            box["error"] = exc

    worker = threading.Thread(target=run, name="fetch", daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        raise ToolError("The site took too long to answer. Try again later.")
    if "error" in box:
        raise box["error"]
    return box["page"]


def _whole_number(value: Any, name: str, default: int) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ToolError(f"'{name}' must be a whole number.")
    try:
        number = int(str(value).strip()) if isinstance(value, str) else int(value)
    except (TypeError, ValueError, OverflowError):
        raise ToolError(f"'{name}' must be a whole number.") from None
    if number < 0:
        raise ToolError(f"'{name}' cannot be negative.")
    return number


def format_page(page: Page, start: int, length: int) -> str:
    text = page.text
    total = len(text)
    if total and start >= total:
        raise ToolError(f"start_index {start} is past the end: the page has {total} characters.")
    end = min(total, start + length)
    if end < total:  # end at a paragraph, line or sentence break when one is close
        for mark in ("\n\n", "\n", ". "):
            cut = text.rfind(mark, start, end)
            if cut > start + length * 0.6:
                end = cut + len(mark)
                break
    piece = text[start:end].strip() or "(The page has no readable text.)"

    header = [f"Title: {page.title}"] if page.title else []
    header.append(f"URL: {page.url[:MAX_LINK_CHARS]}")
    if page.kind != "html":
        header.append(f"Type: {page.kind}")
    notes: list[str] = []
    if page.kind == "html" and start == 0 and total < THIN_PAGE_CHARS:
        notes.append(
            "[Very little text: this page may build itself with JavaScript, which this tool does not run, "
            "or may be mostly images.]"
        )
    if end < total:
        notes.append(
            f"[Characters {start}-{end} of {total}. More follows: call fetch_url again with the same url "
            f"and start_index={end}.]"
        )
    elif start > 0:
        notes.append(f"[Characters {start}-{end} of {total}: the end of the page.]")
    if page.truncated:
        notes.append("[The page was larger than this tool downloads, so its end is missing.]")
    return "\n".join(header) + "\n\n" + piece + ("\n\n" + "\n".join(notes) if notes else "")


def fetch_url(arguments: dict) -> str:
    url = normalize_url(arguments.get("url"))
    length = min(MAX_LENGTH, max(MIN_LENGTH, _whole_number(arguments.get("max_length"), "max_length", DEFAULT_LENGTH)))
    start = _whole_number(arguments.get("start_index"), "start_index", 0)
    raw_flag = arguments.get("raw")
    raw = raw_flag is True or (isinstance(raw_flag, str) and raw_flag.strip().lower() == "true")

    page = _cached(url, raw)
    if page is None:
        page = _with_deadline(lambda: load_page(url, raw), DEADLINE_SECONDS)
        _remember(url, raw, page)
    return format_page(page, start, length)


# =====================================================================
# The server
# =====================================================================

INSTRUCTIONS = (
    "Reads one public web page (or PDF) from its URL and returns its text. "
    "Use it when the person gives a link or asks what a page says, or to read a web_search result in full."
)

TOOLS = [
    {
        "name": "fetch_url",
        "title": "Read a web page",
        "description": (
            "Downloads one public web page (http or https) and returns its readable content as text: "
            "title, headings, paragraphs, lists, tables and links, with menus, ads and scripts removed. "
            "PDFs and plain-text, JSON or XML addresses work too. Use it when the person gives you a URL "
            "or asks what a page says, or after web_search when a result looks right but its snippet does "
            "not answer the question - then fetch that result's URL. Long pages arrive in parts: the reply "
            "ends with the start_index that continues it. It cannot log in, run JavaScript-only sites, or "
            "reach private or local network addresses. It is not a search engine: use web_search to find pages."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The page's address, like https://example.com/article."},
                "max_length": {
                    "type": "integer",
                    "description": f"Characters to return, {MIN_LENGTH} to {MAX_LENGTH}. Default {DEFAULT_LENGTH}.",
                },
                "start_index": {
                    "type": "integer",
                    "description": "Where to continue a long page: the number the previous reply gave. Default 0.",
                },
                "raw": {
                    "type": "boolean",
                    "description": "True returns the page's HTML source instead of readable text. Rarely needed.",
                },
            },
            "required": ["url"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    }
]

SERVER = Server(
    name="jarvis-fetch",
    version="1.0.0",
    instructions=INSTRUCTIONS,
    tools=TOOLS,
    handlers={"fetch_url": fetch_url},
    log_prefix="mcp-fetch",
)


if __name__ == "__main__":
    SERVER.serve()
