"""
The Head of Publishing: presentations as real files, built from a slide plan.

Phase 2 of the capability orchestrator gave Jarvis python-pptx decks; Phase 4 (browser
agents) has it build them in Canva instead, in a browser it drives, whenever it is signed in
to Canva - with python-pptx kept as the engine that always works.

    manager (llm_brain) --delegate_to_publishing(presentation_data)--> PublishingAgent
        -> the JSON read and checked against the shape below (parse_deck)
        -> a text size chosen for every slide, or an ERROR when it cannot fit (plan_deck)
        -> CANVA (agents/canva_browser.py), when Jarvis's own Chrome profile is signed in
           (python -m utils.canva_auth): a new presentation on canva.com in a hidden Chrome
           with playwright-stealth, one page per slide, Share > Download > PPTX (or PDF),
           checked: every slide there, every title in it
        -> PYTHON-PPTX, when Canva is not set up or its run fails (the reason goes back
           with the deck): a 16:9 deck laid out here, opened again and checked
        -> saved as artifacts/presentation_YYYYMMDD_HHMMSS.pptx (or .pdf from Canva)
        <- "SUCCESS: ...\nPath: <absolute path>\nURL: /artifacts/<file name>"
           or "ERROR: <what was wrong and what to change>"

JARVIS_PUBLISHING_ENGINE picks: auto (Canva when signed in, else python-pptx) [default],
canva (Canva only: its failure is the answer) or pptx (python-pptx only). Why python-pptx
stays: a website's editor changes without notice, a Canva session ends, the Docker image has
no browser, and a Canva run takes a minute where python-pptx takes a tenth of a second - none
of which may cost Jarvis the ability to hand over a deck.

In the web chat the brain swaps the Path and URL lines for a Markdown download link,
[Download Presentation](/artifacts/<file name>) (markdown_link): api_server.py serves the
folder at that path, and the web app hands the file to the person whose chat shows the
link (TripPlanner_Web/app/artifacts/[name]).

NOT A LANGUAGE-MODEL LOOP, like the Head of Creative. The manager plans the content -
with the Head of Research when it needs facts - and writes the JSON; this lays it out.
No API key. Canva decks are made in the Canva account Jarvis's Chrome is signed in to - every
web user's included - so they are in that account's designs too.

THE DATA. One JSON object, as a string or as the dict itself:

    {"title": "Solar power in Thailand",              optional: makes the opening slide
     "subtitle": "Market briefing, November 2026",    optional: under that title
     "slides": [                                       1 to MAX_SLIDES, numbered from 1
        {"title": "Where we are",                      required, a few words
         "bullet_points": ["...", "..."]},             0 to MAX_BULLETS strings; [] = a section slide
        ...]}

A bare list is read as the slides. Strictly typed: a missing field, a wrong type or text
that cannot fit is an ERROR naming the slide and what to change, so the manager can fix
the data and call again. A field the deck has no place for is left out and named in the
SUCCESS answer instead of failing the whole deck. Leading bullet characters ("- ", "• "),
Markdown bold and heading marks are dropped: a slide shows the words, not the markup.

THE LAYOUT. python-pptx's default template at PowerPoint's own 16:9 size, every box placed
for that width: a dark opening slide (section slides match it), white content slides with
a rose rule under the title and the slide number. The text size is picked per slide - the
largest that fits by an estimate of how each line wraps (combining marks take no width,
CJK characters twice the usual) - and the boxes still shrink text edited in PowerPoint
later (TEXT_TO_FIT_SHAPE). Thai is set in Leelawadee UI: the template's own Thai fonts,
Angsana New and Cordia New, look much smaller than the Latin text beside them. A slide
whose text does not fit even at the smallest size is an ERROR ("split it into two
slides"), never a slide that runs off the page.

VERIFIED. The saved file is opened again: the slide count and every title must be what
was asked for, or the file is deleted and the answer is an ERROR. A file is never
overwritten (_2, _3... when two decks land in the same second), and the same deck asked
for again within SAME_REQUEST_SECONDS returns the file already made.

NEVER RAISES. Every failure comes back as one sentence starting "ERROR:".

    python -m agents.publishing_agent --check            both engines, a test deck in memory, the folder
    python -m agents.publishing_agent deck.json          builds one deck from a JSON file
    python -m agents.publishing_agent --canva-test       a two-slide deck in Canva only (add --headed to watch)
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import request_context
from utils import canva_auth

from . import canva_browser
from .canva_browser import CanvaError, CanvaSettings, CanvaSlide
from .creative_agent import ARTIFACTS_DIR, web_url

try:
    import pptx
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
    from pptx.oxml.ns import qn
    from pptx.util import Emu, Pt

    _PPTX_IMPORT_ERROR = ""
except ImportError as exc:  # pragma: no cover - requirements.txt has it; an old environment may not
    pptx = None  # type: ignore[assignment]
    _PPTX_IMPORT_ERROR = str(exc)

# What a .pptx is. api_server.py registers it for the files it serves: Python's own table
# has no entry for it on Linux (the Docker image), where it would go out as
# application/octet-stream.
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
FILE_PREFIX = "presentation"
DOWNLOAD_LABEL = "Download Presentation"

MAX_INPUT_CHARS = 200_000
MAX_SLIDES = 40
MAX_BULLETS = 12
MAX_TITLE_CHARS = 200
MAX_SUBTITLE_CHARS = 300
MAX_BULLET_CHARS = 600
SAME_REQUEST_SECONDS = 120

DECK_FIELDS = ("title", "subtitle", "slides")
SLIDE_FIELDS = ("title", "bullet_points")

# --- Geometry (EMU: 914400 to the inch, 12700 to the point) ----------------------------
EMU_PER_INCH = 914400
EMU_PER_PT = 12700


def _inches(value: float) -> int:
    return int(round(value * EMU_PER_INCH))


SLIDE_WIDTH = 12192000  # 13.333 in: PowerPoint's own 16:9
SLIDE_HEIGHT = 6858000  # 7.5 in
MARGIN_X = _inches(0.7)
CONTENT_WIDTH = SLIDE_WIDTH - 2 * MARGIN_X

# A text box's own padding (python-pptx's and PowerPoint's default insets), and the
# template's first-level bullet indent - both taken off the room the text has.
INSET_X = 91440
INSET_Y = 45720
BULLET_INDENT = 342900

# (left, top, width, height). Titles sit on the bottom of their box, so the rule under
# them stays put whether a title takes one line or two; the rules start where the text
# does, inside the boxes' padding.
TITLE_BOX = (MARGIN_X, _inches(0.35), CONTENT_WIDTH, _inches(1.3))
TITLE_RULE = (MARGIN_X + INSET_X, _inches(1.8), _inches(0.9), _inches(0.06))
BODY_BOX = (MARGIN_X, _inches(2.1), CONTENT_WIDTH, _inches(4.6))
NUMBER_BOX = (SLIDE_WIDTH - MARGIN_X - _inches(1.0), _inches(6.85), _inches(1.0), _inches(0.4))
COVER_TITLE_BOX = (MARGIN_X, _inches(1.3), CONTENT_WIDTH, _inches(2.6))
COVER_RULE = (MARGIN_X + INSET_X, _inches(4.08), _inches(1.1), _inches(0.08))
COVER_SUBTITLE_BOX = (MARGIN_X, _inches(4.35), CONTENT_WIDTH, _inches(1.5))

# --- Type -------------------------------------------------------------------------------
# Largest first; the first size at which the text fits is used.
COVER_TITLE_SIZES = (44, 40, 36, 32)
SUBTITLE_SIZES = (24, 22, 20, 18)
SLIDE_TITLE_SIZES = (36, 32, 28, 24)
BODY_SIZES = (28, 24, 22, 20, 18, 16)
NUMBER_SIZE = 12
COVER_TITLE_LINES = 3
SUBTITLE_LINES = 3
SLIDE_TITLE_LINES = 2
BULLET_GAP = 0.5  # space after each bullet point, as a share of its text size
THAI_FONT = "Leelawadee UI"

# Height of one line as a share of the text size. PowerPoint measured 1.2 for Calibri and
# for Leelawadee UI alike (2026-11-01); Thai keeps more room for an app that sets it in
# another font, whose stacked vowels and tone marks need taller lines.
LATIN_LINE = 1.22
THAI_LINE = 1.3
# Lines break between words (Thai between words with no space either), so a line rarely
# fills the whole width: the estimate leaves this share of it unused.
WRAP_SLACK = 0.93

# --- Colours: the Jarvis hub's (TripPlanner_Web) slate and rose ------------------------
INK = (0x0F, 0x17, 0x2A)  # slate-900: titles, the opening slide's background
BODY_INK = (0x1E, 0x29, 0x3B)  # slate-800: bullet points
MUTED = (0x64, 0x74, 0x8B)  # slate-500: slide numbers on white
COVER_TEXT = (0xFF, 0xFF, 0xFF)
COVER_SUBTEXT = (0xCB, 0xD5, 0xE1)  # slate-300
COVER_MUTED = (0x94, 0xA3, 0xB8)  # slate-400: slide numbers on the dark slides
ACCENT = (0xEC, 0x00, 0x3F)  # rose-600, the hub's accent

_THAI = re.compile("[฀-๿]")
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f\ud800-\udfff￾￿]")
_BULLET_MARK = re.compile(r"^(?:[•‣◦▪▫·●○■□➢➤►▶✓✔]\s*|[-*–—]\s+)+")
_HEADING_MARK = re.compile(r"^#{1,6}\s+")
_FENCE = re.compile(r"^```[a-zA-Z]*\s*(.*?)\s*```$", re.DOTALL)


class DeckError(Exception):
    """The data cannot become a deck. The message is one sentence for the manager: what is wrong, what to change."""


@dataclass(frozen=True)
class SlideSpec:
    title: str
    bullets: tuple[str, ...]


@dataclass(frozen=True)
class DeckSpec:
    title: str  # "" = no opening slide
    subtitle: str
    slides: tuple[SlideSpec, ...]
    left_out: tuple[str, ...]  # fields the deck has no place for, named in the SUCCESS answer


@dataclass(frozen=True)
class PlannedSlide:
    """One slide of the finished deck, with the text sizes it gets."""

    kind: str  # "cover" (the opening slide), "section" (a slide with no bullets) or "content"
    title: str
    title_size: int
    bullets: tuple[str, ...] = ()
    body_size: int = 0
    subtitle: str = ""
    subtitle_size: int = 0


# ---------------------------------------------------------------------------------------
# Reading the data
# ---------------------------------------------------------------------------------------


def parse_deck(data: Any) -> DeckSpec:
    """`data` (a JSON string, a dict or a list of slides) as a DeckSpec. Raises DeckError."""
    if isinstance(data, (bytes, bytearray)):
        data = bytes(data).decode("utf-8", errors="replace")
    if isinstance(data, str):
        data = _load_json(data)
    if isinstance(data, list):
        data = {"slides": data}
    if not isinstance(data, dict):
        raise DeckError(f"presentation_data must be a JSON object with a \"slides\" list, not {_kind(data)}.")

    left_out = [f'"{key}"' for key in data if key not in DECK_FIELDS]
    title = _text_field(data.get("title"), "the deck's title", MAX_TITLE_CHARS, heading=True)
    subtitle = _text_field(data.get("subtitle"), "the subtitle", MAX_SUBTITLE_CHARS)
    if subtitle and not title:
        left_out.append('"subtitle" (there is no deck title to put it under)')
        subtitle = ""

    raw_slides = data.get("slides")
    if raw_slides is None:
        raise DeckError(
            'presentation_data has no "slides" list. Send {"title": "...", "slides": [{"title": "...", '
            '"bullet_points": ["...", "..."]}]}.'
        )
    if not isinstance(raw_slides, list):
        raise DeckError(f'"slides" must be a list of slide objects, not {_kind(raw_slides)}.')
    if not raw_slides:
        raise DeckError('"slides" is empty: a presentation needs at least one slide.')
    if len(raw_slides) > MAX_SLIDES:
        raise DeckError(f"the deck has {len(raw_slides)} slides, more than the {MAX_SLIDES} allowed. Make it shorter.")

    slides: list[SlideSpec] = []
    extra: dict[str, int] = {}
    for number, raw in enumerate(raw_slides, start=1):
        where = f"slide {number}"
        if not isinstance(raw, dict):
            raise DeckError(f'{where} must be an object with "title" and "bullet_points", not {_kind(raw)}.')
        for key in raw:
            if key not in SLIDE_FIELDS:
                extra[key] = extra.get(key, 0) + 1
        slide_title = _text_field(raw.get("title"), f"{where}'s title", MAX_TITLE_CHARS, heading=True)
        if not slide_title:
            raise DeckError(f'{where} has no title: every slide needs a short "title".')
        if "bullet_points" not in raw:
            raise DeckError(f'{where} has no "bullet_points" list (send [] for a section slide with only a title).')
        raw_bullets = raw["bullet_points"]
        if not isinstance(raw_bullets, list):
            raise DeckError(f'{where}\'s "bullet_points" must be a list of strings, not {_kind(raw_bullets)}.')
        if len(raw_bullets) > MAX_BULLETS:
            raise DeckError(
                f"{where} has {len(raw_bullets)} bullet points, more than the {MAX_BULLETS} that fit on a slide. "
                "Split it into two slides."
            )
        bullets: list[str] = []
        for index, raw_bullet in enumerate(raw_bullets, start=1):
            if not isinstance(raw_bullet, str):
                raise DeckError(f"{where}, bullet point {index} must be text, not {_kind(raw_bullet)}.")
            bullet = _clean(raw_bullet, bullet=True)
            if len(bullet) > MAX_BULLET_CHARS:
                raise DeckError(
                    f"{where}, bullet point {index} is {len(bullet)} characters long, more than the "
                    f"{MAX_BULLET_CHARS} allowed. Shorten it or split it into several points."
                )
            if bullet:
                bullets.append(bullet)
        slides.append(SlideSpec(slide_title, tuple(bullets)))

    for key, count in extra.items():
        left_out.append(f'"{key}" on {count} slide{"s" if count != 1 else ""}')
    return DeckSpec(title, subtitle, tuple(slides), tuple(left_out))


def _load_json(text: str) -> Any:
    text = text.strip()
    if not text:
        raise DeckError("no presentation data was given. Call again with the whole deck as JSON in presentation_data.")
    if len(text) > MAX_INPUT_CHARS:
        raise DeckError(
            f"the presentation data is {len(text):,} characters long, more than the {MAX_INPUT_CHARS:,} allowed. "
            "Make the deck shorter."
        )
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1)
    try:
        value = json.loads(text)
        # JSON sent as a JSON string ("{\"title\": ...}"): read once more.
        if isinstance(value, str) and value.strip()[:1] in ("{", "["):
            value = json.loads(value)
    except json.JSONDecodeError as exc:
        cut = " - it looks cut off before the end" if exc.pos >= len(text.rstrip()) - 1 else ""
        raise DeckError(
            f"presentation_data is not valid JSON ({exc.msg} at character {exc.pos}){cut}. "
            "Send the whole deck again as one JSON object."
        ) from exc
    return value


def _text_field(value: Any, what: str, limit: int, heading: bool = False) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise DeckError(f"{what} must be text, not {_kind(value)}.")
    text = _clean(value, heading=heading)
    if len(text) > limit:
        raise DeckError(f"{what} is {len(text)} characters long, more than the {limit} allowed. Shorten it.")
    return text


def _clean(text: str, bullet: bool = False, heading: bool = False) -> str:
    """One line of plain text: control characters and markup out, whitespace collapsed."""
    text = " ".join(_CONTROL.sub(" ", text).split())
    # Markdown bold. Not "__": that is also how "__init__.py" is written.
    text = text.replace("**", "")
    if bullet:
        text = _BULLET_MARK.sub("", text)
    if heading:
        text = _HEADING_MARK.sub("", text)
    return text.strip()


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true/false"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, str):
        return "text"
    if isinstance(value, list):
        return "a list"
    if isinstance(value, dict):
        return "an object"
    return type(value).__name__


# ---------------------------------------------------------------------------------------
# Fitting the text
# ---------------------------------------------------------------------------------------


def _char_em(char: str) -> float:
    """Roughly how wide `char` is, in ems."""
    if unicodedata.combining(char) or unicodedata.category(char) in ("Mn", "Me", "Cf"):
        return 0.0  # Thai vowel and tone marks sit on the letter before them
    if unicodedata.east_asian_width(char) in ("W", "F"):
        return 1.0
    if char.isspace():
        return 0.25
    if _THAI.match(char):
        return 0.58
    if char.isupper() or char in "mwMW@%&":
        return 0.62
    return 0.5


def count_lines(text: str, size: float, width_pt: float) -> int:
    """How many lines `text` wraps to at `size` points in a column `width_pt` wide (an estimate)."""
    limit = width_pt * WRAP_SLACK
    lines, used = 1, 0.0
    for token in re.findall(r"\s+|\S+", text):
        width = sum(_char_em(char) for char in token) * size
        if token.isspace():
            if used:
                used += width
            continue
        if used + width <= limit:
            used += width
        elif width <= limit:
            lines += 1
            used = width
        else:
            # A word longer than the line - Thai writes a whole clause without a space.
            for char in token:
                step = _char_em(char) * size
                if used and used + step > limit:
                    lines += 1
                    used = step
                else:
                    used += step
    return lines


def _line_height(text: str, size: float) -> float:
    return size * (THAI_LINE if _THAI.search(text) else LATIN_LINE)


def fit_size(
    texts: tuple[str, ...] | list[str],
    sizes: tuple[int, ...],
    box: tuple[int, int, int, int],
    *,
    indent: int = 0,
    gap: float = 0.0,
    max_lines: Optional[int] = None,
) -> Optional[int]:
    """The largest of `sizes` at which `texts`, one paragraph each, fit in `box`; None when none does."""
    width_pt = (box[2] - 2 * INSET_X - indent) / EMU_PER_PT
    height_pt = (box[3] - 2 * INSET_Y) / EMU_PER_PT
    for size in sizes:
        total = 0.0
        for text in texts:
            lines = count_lines(text, size, width_pt)
            if max_lines is not None and lines > max_lines:
                break
            total += lines * _line_height(text, size)
        else:
            total += gap * size * max(0, len(texts) - 1)
            if total <= height_pt:
                return size
    return None


def plan_deck(deck: DeckSpec) -> list[PlannedSlide]:
    """Every slide of the finished deck with its text sizes. Raises DeckError for text that cannot fit."""
    planned: list[PlannedSlide] = []
    if deck.title:
        size = fit_size([deck.title], COVER_TITLE_SIZES, COVER_TITLE_BOX, max_lines=COVER_TITLE_LINES)
        if size is None:
            raise DeckError(
                f"the deck's title is too long for the opening slide ({len(deck.title)} characters). Shorten it "
                "and put the rest in the subtitle."
            )
        subtitle_size = 0
        if deck.subtitle:
            subtitle_size = fit_size([deck.subtitle], SUBTITLE_SIZES, COVER_SUBTITLE_BOX, max_lines=SUBTITLE_LINES) or 0
            if not subtitle_size:
                raise DeckError(f"the subtitle is too long ({len(deck.subtitle)} characters). Shorten it to a line or two.")
        planned.append(PlannedSlide("cover", deck.title, size, subtitle=deck.subtitle, subtitle_size=subtitle_size))

    for number, slide in enumerate(deck.slides, start=1):
        if not slide.bullets:
            size = fit_size([slide.title], COVER_TITLE_SIZES, COVER_TITLE_BOX, max_lines=COVER_TITLE_LINES)
            if size is None:
                raise DeckError(f"slide {number}'s title is too long for a section slide ({len(slide.title)} characters). Shorten it.")
            planned.append(PlannedSlide("section", slide.title, size))
            continue
        title_size = fit_size([slide.title], SLIDE_TITLE_SIZES, TITLE_BOX, max_lines=SLIDE_TITLE_LINES)
        if title_size is None:
            raise DeckError(
                f"slide {number}'s title is too long for a slide title ({len(slide.title)} characters). Shorten it to "
                "a few words and move the detail into a bullet point."
            )
        body_size = fit_size(slide.bullets, BODY_SIZES, BODY_BOX, indent=BULLET_INDENT, gap=BULLET_GAP)
        if body_size is None:
            characters = sum(len(bullet) for bullet in slide.bullets)
            raise DeckError(
                f"slide {number} has too much text to fit on one slide ({len(slide.bullets)} bullet points, "
                f"{characters:,} characters). Split it into two slides or shorten the points."
            )
        planned.append(PlannedSlide("content", slide.title, title_size, slide.bullets, body_size))
    return planned


# ---------------------------------------------------------------------------------------
# Building the file
# ---------------------------------------------------------------------------------------


def build_deck(deck: DeckSpec, planned: list[PlannedSlide], now: Optional[datetime] = None) -> Any:
    """A python-pptx Presentation holding `planned`, not yet saved."""
    prs = Presentation()
    prs.slide_width = SLIDE_WIDTH
    prs.slide_height = SLIDE_HEIGHT
    layouts = {layout.name: layout for layout in prs.slide_layouts}

    for position, item in enumerate(planned, start=1):
        if item.kind == "cover":
            slide = prs.slides.add_slide(layouts["Title Slide"])
            _dark(slide)
            _text(slide.shapes.title, COVER_TITLE_BOX, item.title, item.title_size, COVER_TEXT, bold=True, anchor="bottom")
            _rule(slide, COVER_RULE)
            subtitle = slide.placeholders[1]
            if item.subtitle:
                _text(subtitle, COVER_SUBTITLE_BOX, item.subtitle, item.subtitle_size, COVER_SUBTEXT, anchor="top")
            else:
                # An empty placeholder shows "Click to add subtitle" while the deck is edited.
                subtitle._element.getparent().remove(subtitle._element)
        elif item.kind == "section":
            slide = prs.slides.add_slide(layouts["Title Only"])
            _dark(slide)
            _text(slide.shapes.title, COVER_TITLE_BOX, item.title, item.title_size, COVER_TEXT, bold=True, anchor="bottom")
            _rule(slide, COVER_RULE)
            _number(slide, position, COVER_MUTED)
        else:
            slide = prs.slides.add_slide(layouts["Title and Content"])
            _text(slide.shapes.title, TITLE_BOX, item.title, item.title_size, INK, bold=True, anchor="bottom")
            _rule(slide, TITLE_RULE)
            _bullets(slide.placeholders[1], item.bullets, item.body_size)
            _number(slide, position, MUTED)

    # The template's own properties name its author, the library and 2013.
    props = prs.core_properties
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(tzinfo=None)
    props.title = (deck.title or deck.slides[0].title)[:255]
    props.subject = deck.subtitle[:255]
    props.author = "Jarvis"
    props.last_modified_by = "Jarvis"
    props.comments = ""
    props.created = stamp
    props.modified = stamp
    props.revision = 1
    return prs


def _rgb(colour: tuple[int, int, int]) -> Any:
    return RGBColor(*colour)


def _place(shape: Any, box: tuple[int, int, int, int]) -> None:
    shape.left, shape.top, shape.width, shape.height = (Emu(value) for value in box)


def _frame(shape: Any, anchor: str) -> Any:
    frame = shape.text_frame
    frame.clear()
    frame.word_wrap = True
    frame.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
    frame.vertical_anchor = MSO_ANCHOR.BOTTOM if anchor == "bottom" else MSO_ANCHOR.TOP
    frame.margin_left = frame.margin_right = Emu(INSET_X)
    frame.margin_top = frame.margin_bottom = Emu(INSET_Y)
    return frame


def _style(run: Any, size: int, colour: tuple[int, int, int], bold: bool = False) -> None:
    font = run.font
    font.size = Pt(size)
    font.bold = bold
    font.color.rgb = _rgb(colour)
    # Thai (a complex script to PowerPoint) is drawn in the cs typeface. a:cs comes after
    # a:solidFill and a:latin in a:rPr and nothing that follows it is ever set here.
    properties = run._r.get_or_add_rPr()
    complex_script = properties.find(qn("a:cs"))
    if complex_script is None:
        complex_script = properties.makeelement(qn("a:cs"), {})
        properties.append(complex_script)
    complex_script.set("typeface", THAI_FONT)


def _text(shape: Any, box: tuple[int, int, int, int], text: str, size: int, colour: tuple[int, int, int], *, bold: bool = False, anchor: str = "top") -> None:
    _place(shape, box)
    paragraph = _frame(shape, anchor).paragraphs[0]
    paragraph.alignment = PP_ALIGN.LEFT
    run = paragraph.add_run()
    run.text = text
    _style(run, size, colour, bold)


def _bullets(shape: Any, bullets: tuple[str, ...], size: int) -> None:
    _place(shape, BODY_BOX)
    frame = _frame(shape, "top")
    for index, bullet in enumerate(bullets):
        paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        paragraph.level = 0
        paragraph.alignment = PP_ALIGN.LEFT
        paragraph.space_before = Pt(0)
        paragraph.space_after = Pt(round(size * BULLET_GAP))
        run = paragraph.add_run()
        run.text = bullet
        _style(run, size, BODY_INK)


def _rule(slide: Any, box: tuple[int, int, int, int]) -> None:
    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, *(Emu(value) for value in box))
    shape.name = "Accent rule"
    shape.fill.solid()
    shape.fill.fore_color.rgb = _rgb(ACCENT)
    shape.line.fill.background()
    shape.shadow.inherit = False


def _number(slide: Any, position: int, colour: tuple[int, int, int]) -> None:
    box = slide.shapes.add_textbox(*(Emu(value) for value in NUMBER_BOX))
    box.name = "Slide number"
    frame = box.text_frame
    frame.word_wrap = False
    paragraph = frame.paragraphs[0]
    paragraph.alignment = PP_ALIGN.RIGHT
    run = paragraph.add_run()
    run.text = str(position)
    _style(run, NUMBER_SIZE, colour)


def _dark(slide: Any) -> None:
    fill = slide.background.fill
    fill.solid()
    fill.fore_color.rgb = _rgb(INK)


# ---------------------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------------------


ENGINES = ("auto", "canva", "pptx")
# After a Canva run that needs a person - the session ended, Canva's bot check stopped the browser -
# Canva is not tried again for this long (python-pptx makes the decks meanwhile, at once), unless
# you sign in again (python -m utils.canva_auth rewrites its note in the profile): that ends the pause.
CANVA_REST_SECONDS = 30 * 60
NEEDS_A_PERSON = ("session", "blocked")
# A web request tells how much of its wait is left (llm_brain, WEB_CHAT_SECONDS): Canva gets at most
# that, and is not started with less than this - python-pptx answers at once instead.
MIN_CANVA_SECONDS = 30.0


def _engine_from_env() -> str:
    value = canva_auth.setting("JARVIS_PUBLISHING_ENGINE", "auto").lower()
    if value not in ENGINES:
        print(f"[Agent:publishing] JARVIS_PUBLISHING_ENGINE={value!r} is not auto, canva or pptx; using auto.")
        return "auto"
    return value


class PublishingAgent:
    """
    Builds one presentation file per call and saves it in artifacts/: in Canva when Jarvis's
    Chrome is signed in to it (and JARVIS_PUBLISHING_ENGINE allows it), else - or when Canva fails -
    with python-pptx. Building one makes no network call.
    """

    name = "publishing"
    title = "Head of Publishing"
    tool_name = "delegate_to_publishing"

    def __init__(
        self,
        *,
        artifacts_dir: Optional[Path | str] = None,
        now: Optional[Callable[[], datetime]] = None,
        engine: Optional[str] = None,
        canva: Optional[CanvaSettings] = None,
        canva_runner: Optional[Callable[[list[CanvaSlide], Path, CanvaSettings], Path]] = None,
    ) -> None:
        self.artifacts_dir = Path(artifacts_dir) if artifacts_dir else ARTIFACTS_DIR
        self._now = now or datetime.now
        self._lock = threading.Lock()  # the list of recent decks, and python-pptx's saving
        self._canva_lock = threading.Lock()  # one Canva run at a time: one account, one browser
        self._recent: dict[str, tuple[float, Path]] = {}  # the deck's fingerprint -> (when, file)
        self.engine = engine if engine in ENGINES else _engine_from_env()
        self.canva = canva or CanvaSettings.from_env()
        self._canva_runner = canva_runner or canva_browser.make_presentation
        # (until, why, the sign-in note's mtime then) while Canva rests after needing a person.
        self._canva_rest: Optional[tuple[float, str, float]] = None

    @property
    def canva_problem(self) -> str:
        """"" when a deck would be tried in Canva first; else why not, in a few words."""
        if self.engine == "pptx":
            return "turned off (JARVIS_PUBLISHING_ENGINE=pptx)"
        return canva_browser.readiness(self.canva)

    @property
    def unavailable_reason(self) -> str:
        if pptx is None:
            return f"python-pptx is not installed ({_PPTX_IMPORT_ERROR})"
        if self.engine == "canva" and self.canva_problem:
            return f"JARVIS_PUBLISHING_ENGINE=canva, and Canva cannot be used: {self.canva_problem}"
        return ""

    @property
    def available(self) -> bool:
        return not self.unavailable_reason

    def describe(self) -> str:
        if not self.available:
            return f"off ({self.unavailable_reason})"
        local = f"python-pptx {getattr(pptx, '__version__', '?')}"
        canva = f"Canva (Chrome {self.canva.window}, {self.canva.fmt.upper()}, up to {self.canva.timeout:.0f} s)"
        if self.engine == "canva":
            where = f"{canva} only"
        elif self.canva_problem:
            where = f"{local} (Canva: {self.canva_problem})"
        else:
            where = f"{canva}, then {local} when Canva fails"
        return f"{where}; into {self.artifacts_dir}"

    # -- the work ------------------------------------------------------------

    def build_presentation(self, presentation_data: Any, time_left: Optional[float] = None) -> str:
        """
        Builds a deck from `presentation_data` and saves it. Returns SUCCESS + its path and URL,
        or ERROR + why. `time_left` - seconds the caller can still wait (a web chat request) -
        caps a Canva run; None means no limit beyond JARVIS_CANVA_TIMEOUT (the voice assistant).
        """
        if not self.available:
            return f"ERROR: presentations are not set up ({self.unavailable_reason}). Tell the user; do not try again."
        try:
            deck = parse_deck(presentation_data)
            planned = plan_deck(deck)
        except DeckError as err:
            print(f"[Agent:publishing] Not built: {_shown(str(err), 240)}")
            return f"ERROR: {err}"

        fingerprint = hashlib.sha256(
            json.dumps([deck.title, deck.subtitle, [[s.title, s.bullets] for s in deck.slides]], ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        summary = _summary(planned)
        with self._lock:
            again = self._already_made(fingerprint)
        if again:
            return again

        canva_note = ""
        if not self.canva_problem:
            resting = self._canva_resting()
            if resting:
                canva_note = f"it was not tried: {resting}"
            elif time_left is not None and time_left < MIN_CANVA_SECONDS:
                canva_note = f"there was not enough time left in this request for Canva ({max(0.0, time_left):.0f} s)"
            # Not blocking: a second deck asked for while Canva works on one is built here instead.
            elif self._canva_lock.acquire(blocking=False):
                try:
                    made, canva_note = self._in_canva(planned, summary, time_left)
                finally:
                    self._canva_lock.release()
                if made is not None:
                    with self._lock:
                        self._recent[fingerprint] = (time.monotonic(), made)
                    kind = "a PDF" if made.suffix == ".pdf" else "a PowerPoint file"
                    return self._answer(made, f"made in Canva and downloaded as {kind}", summary, deck)
            else:
                canva_note = "Canva was busy making another presentation"
            if self.engine == "canva":
                return (
                    f"ERROR: Canva could not make the presentation ({canva_note}). Tell the user in one sentence; "
                    "asking again later may work."
                )
        return self._with_pptx(deck, planned, summary, fingerprint, canva_note)

    def _in_canva(self, planned: list[PlannedSlide], summary: str, time_left: Optional[float] = None) -> tuple[Optional[Path], str]:
        """(the checked file Canva made, "") or (None, why it did not). The run never outlasts `time_left`."""
        started = time.monotonic()
        settings = self.canva
        if time_left is not None and time_left < settings.timeout:
            settings = replace(settings, timeout=time_left)
        try:
            target = self._reserve(settings.extension)
        except OSError as exc:
            return None, f"no file could be made in {self.artifacts_dir} ({exc})"
        slides = [CanvaSlide(item.kind, item.title, item.subtitle, item.bullets) for item in planned]
        print(f"[Agent:publishing] Building {summary} in Canva (up to {settings.timeout:.0f} s): {_shown(planned[0].title)}")
        try:
            self._canva_runner(slides, target, settings)
        except request_context.Cancelled:
            # The request was stopped: Canva's browser is closed; no python-pptx deck either.
            target.unlink(missing_ok=True)
            raise
        except CanvaError as err:
            target.unlink(missing_ok=True)
            kept = f" Screenshot and page outline: {err.debug}.png / .txt" if err.debug else ""
            print(f"[Agent:publishing] Canva stopped at {err.stage} ({time.monotonic() - started:.0f} s): {_shown(err.message, 200)}.{kept}")
            if err.stage in NEEDS_A_PERSON:
                self._canva_rest = (time.monotonic() + CANVA_REST_SECONDS, err.message, _mtime(self._signed_in_note))
                print(f"[Agent:publishing] Canva rests for {CANVA_REST_SECONDS // 60} min, or until you sign in again.")
            return None, f"it stopped at {err.stage}: {err.message}"
        except Exception as exc:  # noqa: BLE001 - a fault in the browser code must not cost the deck
            target.unlink(missing_ok=True)
            print(f"[Agent:publishing] Canva run failed: {type(exc).__name__}: {exc}")
            return None, f"{type(exc).__name__}: {_shown(str(exc), 160)}"
        problem = _verify_download(target, planned)
        if problem:
            print(f"[Agent:publishing] Canva's file {target.name} failed its check ({problem}); deleted.")
            target.unlink(missing_ok=True)
            return None, f"the file it made did not check out ({problem})"
        size_kb = max(1, round(target.stat().st_size / 1024))
        print(f"[Agent:publishing] Saved {target} from Canva ({size_kb} KB, {time.monotonic() - started:.0f} s).")
        return target, ""

    def _canva_resting(self) -> str:
        """Why Canva is skipped for now (a run that needed a person failed lately), or ""."""
        if self._canva_rest is None:
            return ""
        until, why, mtime = self._canva_rest
        if time.monotonic() >= until or _mtime(self._signed_in_note) != mtime:
            self._canva_rest = None
            return ""
        minutes = max(1, round((until - time.monotonic()) / 60))
        return f"{why} (Canva is tried again in {minutes} min, or as soon as you sign in again)"

    @property
    def _signed_in_note(self) -> Path:
        return canva_auth.signed_in_file(self.canva.profile)

    def _with_pptx(self, deck: DeckSpec, planned: list[PlannedSlide], summary: str, fingerprint: str, canva_note: str) -> str:
        """The deck laid out here with python-pptx, saved and checked; SUCCESS or ERROR."""
        with self._lock:
            started = time.monotonic()
            print(f"[Agent:publishing] Building {summary} with python-pptx: {_shown(planned[0].title)}")
            try:
                prs = build_deck(deck, planned)
            except Exception as exc:  # noqa: BLE001 - python-pptx/lxml refusing something unexpected
                print(f"[Agent:publishing] python-pptx failed: {type(exc).__name__}: {exc}")
                return f"ERROR: the presentation could not be built ({type(exc).__name__}: {_shown(str(exc), 160)})."
            try:
                path = self._save(prs)
            except OSError as exc:
                return f"ERROR: the presentation was built but could not be saved in {self.artifacts_dir} ({exc})."

            problem = _verify(path, [item.title for item in planned])
            if problem:
                print(f"[Agent:publishing] {path.name} failed its check ({problem}); deleted.")
                path.unlink(missing_ok=True)
                return f"ERROR: the presentation was saved but did not check out ({problem}), so it was deleted."

            self._recent[fingerprint] = (time.monotonic(), path)
            size_kb = max(1, round(path.stat().st_size / 1024))
            print(f"[Agent:publishing] Saved {path} ({size_kb} KB, {time.monotonic() - started:.1f} s).")
            return self._answer(path, "created and saved as a PowerPoint file", f"{summary}, 16:9", deck, canva_note)

    @staticmethod
    def _answer(path: Path, how: str, summary: str, deck: DeckSpec, canva_note: str = "") -> str:
        lines = [f"SUCCESS: the presentation was {how} ({summary}).", f"Path: {path}", f"URL: {web_url(path)}"]
        if canva_note:
            lines.append(f"Canva could not make it, so it was built with python-pptx instead: {canva_note}.")
        if deck.left_out:
            lines.append(
                "Left out (a slide only has a title and bullet points; the deck a title and subtitle): "
                + ", ".join(deck.left_out)
                + "."
            )
        return "\n".join(lines)

    def _already_made(self, fingerprint: str) -> str:
        """The answer for the same deck made moments ago (the file still there), or ""."""
        now = time.monotonic()
        for key, (when, _path) in list(self._recent.items()):
            if now - when > SAME_REQUEST_SECONDS:
                del self._recent[key]
        hit = self._recent.get(fingerprint)
        if hit is None or not hit[1].exists():
            return ""
        print(f"[Agent:publishing] Same deck {now - hit[0]:.0f} s ago; not made again.")
        return (
            f"SUCCESS: this exact presentation was already created {now - hit[0]:.0f} seconds ago, so it was not "
            f"made again.\nPath: {hit[1]}\nURL: {web_url(hit[1])}"
        )

    def _reserve(self, extension: str) -> Path:
        """A new, empty artifacts/presentation_YYYYMMDD_HHMMSS<extension> (_2, _3... if taken): never another file's name."""
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        stamp = self._now().strftime("%Y%m%d_%H%M%S")
        for number in range(1, 100):
            path = self.artifacts_dir / (
                f"{FILE_PREFIX}_{stamp}{extension}" if number == 1 else f"{FILE_PREFIX}_{stamp}_{number}{extension}"
            )
            try:
                open(path, "xb").close()  # claims the name; never overwrites
            except FileExistsError:
                continue
            return path.resolve()
        raise OSError(f"no free file name for {FILE_PREFIX}_{stamp}")

    def _save(self, prs: Any) -> Path:
        """Writes `prs` as a new artifacts/presentation_YYYYMMDD_HHMMSS.pptx and returns its absolute path."""
        path = self._reserve(".pptx")
        try:
            with open(path, "wb") as handle:
                prs.save(handle)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path

    # -- setup check (python -m agents.publishing_agent --check) ---------------

    def check(self) -> list[str]:
        """Lines on the setup: python-pptx and a test deck built in memory, the folder, Canva. Saves nothing."""
        lines = [f"Presentations: {self.describe()}"]
        if pptx is not None:
            try:
                sample = parse_deck({"title": "Setup check", "slides": [{"title": "It works", "bullet_points": ["ทดสอบภาษาไทย", "English"]}]})
                buffer = io.BytesIO()
                build_deck(sample, plan_deck(sample)).save(buffer)
                lines.append(f"  python-pptx test deck: built in memory ({len(buffer.getvalue()) // 1024} KB)")
            except Exception as exc:  # noqa: BLE001
                lines.append(f"  python-pptx test deck: FAILED ({type(exc).__name__}: {exc})")
        try:
            self.artifacts_dir.mkdir(parents=True, exist_ok=True)
            probe = self.artifacts_dir / ".write-check"
            probe.write_bytes(b"")
            probe.unlink()
            lines.append(f"  folder: {self.artifacts_dir} is writable")
        except OSError as exc:
            lines.append(f"  folder: {self.artifacts_dir} is NOT writable ({exc})")
        if self.engine == "pptx":
            lines.append("  Canva: off (JARVIS_PUBLISHING_ENGINE=pptx)")
            return lines
        lines.append(f"  Canva sign-in: {_session_note(self.canva.profile)}")
        lines.append(f"  Canva browser: {canva_browser.check_browser(self.canva)}, {self.canva.window}")
        lines.append(f"  Canva stealth: {_stealth_note()}")
        return lines


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _session_note(profile: Path) -> str:
    """Whether Jarvis's Chrome profile is signed in to Canva, and since when - never a cookie."""
    note = canva_auth.signed_in_file(profile)
    if not note.is_file():
        return f"not signed in (profile {profile}) - sign in once: python -m utils.canva_auth"
    record = canva_auth.signed_in_record(profile)
    if record is None:
        return f"{note} cannot be read - sign in again: python -m utils.canva_auth"
    hours = max(0.0, (time.time() - note.stat().st_mtime) / 3600)
    age = f"{hours:.0f} hours" if hours < 48 else f"{hours / 24:.0f} days"
    return f"profile {profile}, signed in {age} ago, {record.get('canva_cookies', '?')} Canva cookies then"


def _stealth_note() -> str:
    """playwright-stealth's version, or what to install."""
    problem = canva_auth.stealth_problem()
    if problem:
        return f"NOT AVAILABLE - {problem}"
    try:
        from importlib.metadata import version

        return f"playwright-stealth {version('playwright-stealth')}"
    except Exception:  # noqa: BLE001 - installed without metadata
        return "playwright-stealth (version unknown)"


def _verify(path: Path, titles: list[str]) -> str:
    """"" when the saved file opens and holds exactly these slide titles, in order; else what is wrong."""
    try:
        saved = Presentation(str(path))
    except Exception as exc:  # noqa: BLE001 - a broken zip, bad XML
        return f"it does not open as a presentation ({type(exc).__name__})"
    found = []
    for slide in saved.slides:
        title = slide.shapes.title
        found.append(title.text_frame.text if title is not None and title.has_text_frame else "")
    if len(found) != len(titles):
        return f"it has {len(found)} slides instead of {len(titles)}"
    for number, (got, wanted) in enumerate(zip(found, titles), start=1):
        if got != wanted:
            return f"slide {number}'s title came out different from the one asked for"
    return ""


def _verify_download(path: Path, planned: list[PlannedSlide]) -> str:
    """
    "" when the file Canva sent holds one slide (or PDF page) per planned slide, each with its
    title in it; else what is wrong. Canva lays text out its own way, so a title is looked for
    anywhere in its slide's text, spaces ignored.
    """
    try:
        if path.stat().st_size == 0:
            return "it is empty"
    except OSError as exc:
        return f"it is not there ({exc})"
    if path.suffix == ".pdf":
        texts = _pdf_page_texts(path)
        if isinstance(texts, str):
            return texts
        if texts is None:  # PyMuPDF missing: a PDF header is all that can be checked
            return ""
    else:
        try:
            saved = Presentation(str(path))
        except Exception as exc:  # noqa: BLE001
            return f"it does not open as a presentation ({type(exc).__name__})"
        texts = [" ".join(_shape_texts(slide.shapes)) for slide in saved.slides]
    if len(texts) != len(planned):
        return f"it has {len(texts)} slides instead of {len(planned)}"
    for number, (text, item) in enumerate(zip(texts, planned), start=1):
        if _squeeze(item.title) not in _squeeze(text):
            return f"slide {number} does not hold its title"
    return ""


def _shape_texts(shapes: Any) -> list[str]:
    """The text of every shape, groups included."""
    texts: list[str] = []
    for shape in shapes:
        if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
            texts.append(shape.text_frame.text)
        inner = getattr(shape, "shapes", None)
        if inner is not None:
            texts.extend(_shape_texts(inner))
    return texts


def _pdf_page_texts(path: Path) -> "list[str] | str | None":
    """Each page's text; a string saying what is wrong; or None when PyMuPDF is not installed."""
    with open(path, "rb") as handle:
        if handle.read(5) != b"%PDF-":
            return "it is not a PDF"
    try:
        import pymupdf
    except ImportError:
        try:
            import fitz as pymupdf  # type: ignore[no-redef]  # PyMuPDF before 1.24.3
        except ImportError:
            return None
    try:
        with pymupdf.open(str(path)) as document:
            return [page.get_text() for page in document]
    except Exception as exc:  # noqa: BLE001
        return f"it does not open as a PDF ({type(exc).__name__})"


def _squeeze(text: str) -> str:
    return "".join(unicodedata.normalize("NFC", text).split()).casefold()


def _summary(planned: list[PlannedSlide]) -> str:
    """"9 slides: an opening slide, 7 content slides and 1 section slide"."""
    counts = {kind: sum(1 for item in planned if item.kind == kind) for kind in ("cover", "content", "section")}
    parts = []
    if counts["cover"]:
        parts.append("an opening slide")
    if counts["content"]:
        parts.append(f"{counts['content']} content slide{'s' if counts['content'] != 1 else ''}")
    if counts["section"]:
        parts.append(f"{counts['section']} section slide{'s' if counts['section'] != 1 else ''}")
    listed = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    return f"{len(planned)} slide{'s' if len(planned) != 1 else ''}: {listed}"


def markdown_link(path: Path | str, label: str = DOWNLOAD_LABEL) -> str:
    """The Markdown line the web chat shows as a download: [Download Presentation](/artifacts/<file name>)."""
    return f"[{label}]({web_url(path)})"


def _shown(text: str, limit: int = 120) -> str:
    """`text` cut short and safe for a console that cannot print Thai."""
    text = text if len(text) <= limit else text[: limit - 1] + "…"
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return text.encode(encoding, errors="backslashreplace").decode(encoding)


def build() -> PublishingAgent:
    return PublishingAgent()


CANVA_TEST_DECK = {
    "title": "Jarvis Canva test",
    "subtitle": "Made by the Publishing Agent",
    "slides": [{"title": "It works", "bullet_points": ["Typed into Canva by Jarvis", "Downloaded as a file"]}],
}


if __name__ == "__main__":
    agent = build()
    arguments = sys.argv[1:]
    if not arguments or arguments == ["--check"]:
        print("\n".join(agent.check()))
        raise SystemExit(0 if agent.available else 1)
    if arguments[0] == "--canva-test":
        settings = agent.canva.watched() if "--headed" in arguments else agent.canva
        tester = PublishingAgent(engine="canva", canva=settings)
        result = tester.build_presentation(CANVA_TEST_DECK)
        print(_shown(result, 2000))
        if not result.startswith("SUCCESS"):
            print(f"Where it stopped: the newest screenshot and page outline in {canva_browser.DEBUG_DIR}")
        raise SystemExit(0 if result.startswith("SUCCESS") else 1)
    source = Path(arguments[0])
    result = agent.build_presentation(source.read_text(encoding="utf-8"))
    print(_shown(result, 2000))
    raise SystemExit(0 if result.startswith("SUCCESS") else 1)
