"""
The Head of Publishing's hands in Canva: a browser that builds the deck on canva.com and downloads it.

    PublishingAgent (agents/publishing_agent.py)
        -> make_presentation(slides, target)       blocking; the browser runs on its own thread
            Playwright opens Jarvis's own Chrome profile, signed in by hand once
            (.data/canva_profile, utils/canva_auth.py), playwright-stealth applied, hidden
            -> a new presentation: Create a design > Presentation, or JARVIS_CANVA_DESIGN_URL
            -> one page per slide; its heading, subheading or bullet points typed in
            -> Share > Download > PPTX (or PDF) -> saved as `target`
        <- `target`, or CanvaError(stage, message) saying where it stopped

HOW IT FINDS THINGS. Canva's editor changes its markup all the time, so nothing here relies on
class names or generated ids. Controls are found by their accessible names - what a screen
reader announces: "Share", "Download", "Text", "Add a heading", "Add page" - and text goes in
through the keyboard, the way a person edits: a text box is added (selected), Enter edits it,
Ctrl+A selects its text, typing replaces it, Escape ends the edit, Shift+arrows move it.
Pages come from Canva's own "Add page" button or its shortcut, Ctrl+Enter.

WHEN IT BREAKS - it will, whenever Canva renames a button - the error names the stage, and a
screenshot plus the page's accessibility tree (every control's real name at that moment) are
kept in .data/canva_debug/ (git ignores it; it can show your Canva account). The tree is what
the patterns below need to be fixed against. Watch a run:
    python -m agents.publishing_agent --canva-test --headed

THE EDITOR IS NOT CHECKED AGAINST CANVA ITSELF: written on 2026-11-01 without a Canva account
to sign in with, and tested against a local stand-in of the editor that has exactly the names
and keys used here. What was checked on canva.com itself (signed out): Cloudflare's check lets
this browser through to the home page (utils/canva_auth.py, STEALTH), and a profile that is
not signed in is recognised there (stage "session").

CLOUDFLARE. Its "Just a moment..." page often clears itself after a few seconds, as in any
browser, so a run waits CHALLENGE_WAIT for that; a check still there afterwards stops the run
(stage "blocked"). Nothing here clicks or solves it. A hidden run that was stopped is tried
once more in a minimized window when JARVIS_CANVA_HEADLESS=auto. After that it takes a person:
python -m utils.canva_auth, with the check done by hand in its window.

SETTINGS (.env)
    JARVIS_CANVA_PROFILE     see utils/canva_auth.py. The browser is always Google Chrome.
    JARVIS_CANVA_DESIGN_URL  where a new deck starts: a link that makes a NEW one-page design each
                             time it is opened (a template's "use this template" link). Blank =
                             Canva's home page, Create a design > Presentation
    JARVIS_CANVA_FORMAT      pptx or pdf [pptx]
    JARVIS_CANVA_TIMEOUT     seconds for the whole run [75]; the web chat waits 120 in all
    JARVIS_CANVA_HEADLESS    auto - hidden; if Cloudflare stops it, once more in a minimized window [default]
                             1    - hidden only
                             0    - always a minimized window (opens off screen, sits in the taskbar)
                             Either way it is Jarvis's own Chrome profile, never your Chrome's windows.
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
import sys
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from utils import canva_auth

ROOT = Path(__file__).resolve().parent.parent
DEBUG_DIR = ROOT / ".data" / "canva_debug"
KEEP_DEBUG_RUNS = 10

DEFAULT_TIMEOUT = 75.0
EDITOR_TIMEOUT = 35.0  # seconds for the editor to appear (Canva is a heavy page)
STEP_TIMEOUT = 12.0  # seconds to find one control
# Every step is held to the run's own deadline; this much more is the hard stop, so a run that
# runs out of time still has the seconds to keep its screenshot and close the browser.
GRACE = 15.0
SIGNED_OUT = "Jarvis's Chrome is signed out of Canva or the session has ended - run: python -m utils.canva_auth"
# Canva sits behind Cloudflare. Its check often clears itself within seconds; this long it may try.
CHALLENGE_WAIT = 10.0
# A hidden run that Cloudflare stopped is tried again in a minimized window only with this much time left.
RETRY_MIN_SECONDS = 25.0
BLOCKED = (
    "Canva's bot check (Cloudflare's \"Verify you are human\") stopped Jarvis's browser. Run "
    "python -m utils.canva_auth and do the check by hand in its window - Jarvis's next runs use that "
    "profile; until then python-pptx makes the decks"
)
_CHALLENGE = re.compile(r"verify you are human|just a moment|checking your browser|designing again soon", re.I)
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")

# JARVIS_CANVA_FORMAT -> (how its entry in Canva's File type list is named, the file's extension)
FORMATS: dict[str, tuple[str, str]] = {"pptx": (r"pptx|powerpoint", ".pptx"), "pdf": (r"pdf\s*standard|^\s*pdf\s*$", ".pdf")}

# Where each text goes, and how far it is moved (Shift+arrow presses) from the centre of the page
# Canva drops a new text box on.
TEXT_BUTTONS = {
    "heading": r"^\s*add a heading\s*$",
    "subheading": r"^\s*add a subheading\s*$",
    "body": r"^\s*add (a little bit of )?body text\s*$",
}
MOVES = {
    ("cover", "heading"): ("ArrowUp", 6),
    ("cover", "subheading"): ("ArrowDown", 6),
    ("section", "heading"): ("ArrowUp", 0),
    ("content", "heading"): ("ArrowUp", 24),
    ("content", "body"): ("ArrowDown", 4),
}


class CanvaError(Exception):
    """A Canva run that stopped. `stage` says where; `debug` is the screenshot's path stem, if one was kept."""

    def __init__(self, stage: str, message: str, debug: Optional[str] = None) -> None:
        super().__init__(f"{stage}: {message}")
        self.stage = stage
        self.message = message
        self.debug = debug


@dataclass(frozen=True)
class CanvaSlide:
    kind: str  # "cover", "section" or "content" (agents/publishing_agent.py, PlannedSlide.kind)
    title: str
    subtitle: str = ""
    bullets: tuple[str, ...] = ()


@dataclass(frozen=True)
class CanvaSettings:
    profile: Path  # Jarvis's Chrome profile folder (utils/canva_auth.py)
    design_url: str
    fmt: str
    timeout: float
    headless: bool
    window_fallback: bool = True  # a hidden run that Cloudflare stopped: once more in a minimized window
    minimized: bool = True  # a visible window opens off screen and minimized (False: in front, to watch)
    slow_mo: int = 0  # milliseconds between browser actions (the --headed test run, to watch it)

    @property
    def extension(self) -> str:
        return FORMATS[self.fmt][1]

    @property
    def window(self) -> str:
        """How the browser shows, in a few words (describe, --check)."""
        if self.headless:
            return "hidden, then a minimized window if Cloudflare stops it" if self.window_fallback else "hidden"
        return "in a minimized window" if self.minimized else "in a visible window"

    @classmethod
    def from_env(cls) -> "CanvaSettings":
        fmt = canva_auth.setting("JARVIS_CANVA_FORMAT", "pptx").lower()
        try:
            timeout = float(canva_auth.setting("JARVIS_CANVA_TIMEOUT", str(DEFAULT_TIMEOUT)))
        except ValueError:
            timeout = DEFAULT_TIMEOUT
        # Hidden by default: with stealth, Cloudflare let headless Chrome through to canva.com (2026-11-01).
        mode = canva_auth.setting("JARVIS_CANVA_HEADLESS", "auto").lower()
        return cls(
            profile=canva_auth.profile_path(),
            design_url=canva_auth.setting("JARVIS_CANVA_DESIGN_URL"),
            fmt=fmt if fmt in FORMATS else "pptx",
            timeout=min(max(timeout, 20.0), 600.0),
            headless=mode not in _FALSE,
            window_fallback=mode not in _TRUE + _FALSE,
        )

    def watched(self) -> "CanvaSettings":
        """The same, in a window in front of you and slowed down, with time to watch it."""
        return replace(self, headless=False, minimized=False, window_fallback=False, slow_mo=250, timeout=max(self.timeout, 240.0))


def readiness(settings: CanvaSettings) -> str:
    """"" when a Canva run can be tried; else why not, in a few words."""
    if importlib.util.find_spec("playwright") is None:
        return "Playwright is not installed (pip install -r requirements.txt)"
    problem = canva_auth.stealth_problem()
    if problem:
        return problem
    if not canva_auth.signed_in_file(settings.profile).is_file():
        return "not signed in to Canva yet (run: python -m utils.canva_auth)"
    return ""


# ---------------------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------------------


def _named(pattern: str) -> "re.Pattern[str]":
    return re.compile(pattern, re.IGNORECASE)


class _Run:
    """One Canva session in one browser page: where it is (`stage`) and the steps."""

    def __init__(self, page: Any, settings: CanvaSettings, deadline: float) -> None:
        self.page = page
        self.settings = settings
        self.deadline = deadline
        self.stage = "open"
        self.blocked_since: Optional[float] = None

    def ms(self, cap: float = STEP_TIMEOUT) -> int:
        """Milliseconds a browser action may take: `cap` seconds, never past the run's deadline."""
        return max(1, int(min(cap, self.deadline - time.monotonic()) * 1000))

    def out_of_time(self, what: str) -> CanvaError:
        return CanvaError("timeout", f"Canva took longer than {self.settings.timeout:.0f} seconds (at {self.stage}, {what})")

    # -- finding controls --------------------------------------------------------------

    def control(self, pattern: str, roles: tuple[str, ...] = ("button", "menuitem", "tab", "link", "option")) -> Any:
        """Every element with one of `roles` whose accessible name matches `pattern`."""
        name = _named(pattern)
        locator = self.page.get_by_role(roles[0], name=name)
        for role in roles[1:]:
            locator = locator.or_(self.page.get_by_role(role, name=name))
        return locator

    async def find(self, what: str, candidates: list[Callable[[], Any]], timeout: float = STEP_TIMEOUT, last: bool = False) -> Any:
        """The first visible element any candidate locator finds, within `timeout` seconds. Raises CanvaError."""
        give_up = min(time.monotonic() + timeout, self.deadline)
        while True:
            for make in candidates:
                matches = make()
                try:
                    count = await matches.count()
                    for index in reversed(range(count)) if last else range(count):
                        element = matches.nth(index)
                        if await element.is_visible():
                            return element
                except Exception:  # noqa: BLE001 - a page in the middle of changing: look again
                    pass
            if time.monotonic() >= give_up:
                if time.monotonic() >= self.deadline:
                    raise self.out_of_time(f"waiting for {what}")
                raise CanvaError(self.stage, f"could not find {what}")
            await asyncio.sleep(0.25)

    async def visible(self, locator: Any) -> bool:
        try:
            return await locator.first.is_visible()
        except Exception:  # noqa: BLE001
            return False

    def signed_out(self) -> bool:
        return bool(re.search(r"/(login|signup)\b", self.page.url))

    async def blocked(self) -> bool:
        """True while the page is Cloudflare's bot check instead of Canva."""
        try:
            if _CHALLENGE.search(await self.page.title() or ""):
                return True
            if await self.page.locator("iframe[src*='challenges.cloudflare.com']").count():
                return True
            return await self.visible(self.page.get_by_text(_CHALLENGE))
        except Exception:  # noqa: BLE001 - a page that is navigating: not known to be blocked
            return False

    async def stopped_by_bot_check(self) -> bool:
        """True once Cloudflare's check has stood for CHALLENGE_WAIT seconds; it often clears itself first."""
        if not await self.blocked():
            self.blocked_since = None
            return False
        now = time.monotonic()
        if self.blocked_since is None:
            self.blocked_since = now
        return now - self.blocked_since >= CHALLENGE_WAIT

    # -- the steps ---------------------------------------------------------------------

    async def open_design(self) -> None:
        """Opens a new presentation and waits for the editor, in the tab Canva opens it in."""
        self.stage = "open"
        url = self.settings.design_url or canva_auth.CANVA_URL
        await self.page.goto(url, wait_until="domcontentloaded", timeout=self.ms(30))
        tabs_before = list(self.page.context.pages)
        if not self.settings.design_url:
            self.stage = "create"
            create = self.control(r"^\s*create( a design)?\s*$", ("button", "link"))
            log_in = self.control(r"^\s*(log in|sign in)\s*$", ("button", "link"))
            give_up = min(time.monotonic() + EDITOR_TIMEOUT, self.deadline)
            while not await self.visible(create):
                if await self.stopped_by_bot_check():
                    raise CanvaError("blocked", BLOCKED)
                if self.signed_out() or await self.visible(log_in):
                    raise CanvaError("session", SIGNED_OUT)
                if time.monotonic() >= give_up:
                    if time.monotonic() >= self.deadline:
                        raise self.out_of_time("waiting for the Create a design button")
                    raise CanvaError("create", "could not find the Create a design button")
                await asyncio.sleep(0.25)
            await create.first.click(timeout=self.ms())
            choice = await self.find(
                "Presentation in the Create a design menu",
                [
                    lambda: self.control(r"^\s*presentation(\s*\(16:9\))?\s*$", ("menuitem", "button", "link", "option")),
                    lambda: self.page.get_by_text(_named(r"^\s*presentation(\s*\(16:9\))?\s*$")),
                ],
            )
            await choice.click(timeout=self.ms())
        await self._wait_for_editor(tabs_before)

    async def _wait_for_editor(self, tabs_before: list[Any]) -> None:
        """Waits for the editor's Share button - in a new tab, if Canva opened the design in one."""
        self.stage = "editor"
        give_up = min(time.monotonic() + EDITOR_TIMEOUT, self.deadline)
        while True:
            fresh = [tab for tab in self.page.context.pages if tab not in tabs_before]
            if fresh:
                self.page = fresh[-1]
                tabs_before = list(self.page.context.pages)
            if await self.visible(self.control(r"^\s*share\s*$", ("button",))):
                return
            if await self.stopped_by_bot_check():
                raise CanvaError("blocked", BLOCKED)
            if self.signed_out():
                raise CanvaError("session", SIGNED_OUT)
            if time.monotonic() >= give_up:
                if time.monotonic() >= self.deadline:
                    raise self.out_of_time("waiting for the editor")
                raise CanvaError("editor", "the Canva editor did not open (no Share button)")
            await asyncio.sleep(0.5)

    async def write_slide(self, index: int, slide: CanvaSlide) -> None:
        self.stage = f"slide {index + 1}"
        keyboard = self.page.keyboard
        await keyboard.press("Escape")
        if index:
            await self._add_page()
        await self._add_text(slide.kind, "heading", [slide.title])
        if slide.kind == "cover" and slide.subtitle:
            await self._add_text(slide.kind, "subheading", [slide.subtitle])
        if slide.kind == "content":
            await self._add_text(slide.kind, "body", [f"• {bullet}" for bullet in slide.bullets])

    async def _add_page(self) -> None:
        button = self.control(r"^\s*add (a )?(new )?page\s*$", ("button",))
        if await self.visible(button):
            await button.first.click(timeout=self.ms())
        else:
            await self.page.keyboard.press("ControlOrMeta+Enter")
        await asyncio.sleep(0.6)  # the new page scrolls in and becomes the current one

    async def _add_text(self, slide_kind: str, style: str, lines: list[str]) -> None:
        adder = self.control(TEXT_BUTTONS[style])
        if not await self.visible(adder):
            panel = await self.find("the Text panel in the side bar", [lambda: self.control(r"^\s*text\s*$", ("button", "tab"))])
            await panel.click(timeout=self.ms())
        adder_button = await self.find(f"Canva's {style} text button", [lambda: self.control(TEXT_BUTTONS[style])])
        await adder_button.click(timeout=self.ms())
        keyboard = self.page.keyboard
        await keyboard.press("Enter")  # edit the new text box
        await keyboard.press("ControlOrMeta+A")  # all of its placeholder text
        for number, line in enumerate(lines):
            if number:
                await keyboard.press("Enter")
            await keyboard.type(line)
        await keyboard.press("Escape")  # done editing; the box stays selected
        direction, steps = MOVES.get((slide_kind, style), ("ArrowDown", 0))
        for _ in range(steps):
            await keyboard.press(f"Shift+{direction}")
        await keyboard.press("Escape")  # nothing selected

    async def download(self, target: Path) -> Path:
        """Share > Download > the file type > Download; the file saved as `target`."""
        self.stage = "share"
        share = await self.find("the Share button", [lambda: self.control(r"^\s*share\s*$", ("button",))])
        await share.click(timeout=self.ms())
        self.stage = "download"
        entry = await self.find("Download in the Share menu", [lambda: self.control(r"^\s*download\s*$")])
        await entry.click(timeout=self.ms())
        self.stage = "file type"
        option_name = FORMATS[self.settings.fmt][0]
        picker = await self.find(
            "the File type picker",
            [
                lambda: self.control(r"file type", ("combobox", "button", "listbox")),
                lambda: self.control(r"^\s*(pdf|png|jpg|pptx|svg|gif|mp4)\b", ("combobox", "button")),
            ],
        )
        await picker.click(timeout=self.ms())
        option = await self.find(
            f"{self.settings.fmt.upper()} in the File type list",
            [
                lambda: self.control(option_name, ("option", "menuitemradio", "menuitem", "radio", "button")),
                lambda: self.page.get_by_text(_named(option_name)),
            ],
        )
        await option.click(timeout=self.ms())
        self.stage = "export"
        go = await self.find(
            "the Download button", [lambda: self.page.get_by_role("button", name=_named(r"^\s*download\s*$"))], last=True
        )
        # Canva prepares the file before the browser gets it: the rest of the run's time.
        wait_ms = self.ms(cap=self.settings.timeout)
        async with self.page.expect_download(timeout=wait_ms) as info:
            await go.click(timeout=wait_ms)
        download = await info.value
        failure = await download.failure()
        if failure:
            raise CanvaError("export", f"the download failed ({failure})")
        await download.save_as(str(target))
        return target

    async def keep_debug(self, problem: str) -> Optional[str]:
        """A screenshot and the accessibility tree of where it stopped, in DEBUG_DIR; returns the files' stem."""
        try:
            DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            stem = DEBUG_DIR / f"{datetime.now():%Y%m%d_%H%M%S}-{re.sub(r'[^a-z0-9]+', '-', self.stage.lower()).strip('-')}"
            try:
                await self.page.screenshot(path=f"{stem}.png", timeout=2_500)  # short: a page mid-navigation stalls it
            except Exception:  # noqa: BLE001
                pass
            try:
                tree = await self.page.locator("body").aria_snapshot(timeout=2_500)
            except Exception as exc:  # noqa: BLE001
                tree = f"(no accessibility tree: {type(exc).__name__})"
            Path(f"{stem}.txt").write_text(
                f"stage: {self.stage}\nproblem: {problem}\nurl: {self.page.url}\n\n{tree}\n", encoding="utf-8"
            )
            _prune_debug()
            return str(stem)
        except Exception:  # noqa: BLE001 - a debug aid must never hide the real error
            return None


def _prune_debug() -> None:
    stems = sorted({path.with_suffix("") for path in DEBUG_DIR.glob("*-*.*")})
    for stem in stems[:-KEEP_DEBUG_RUNS]:
        for path in DEBUG_DIR.glob(f"{stem.name}.*"):
            path.unlink(missing_ok=True)


async def _make(slides: list[CanvaSlide], target: Path, settings: CanvaSettings, progress: dict[str, Any]) -> Path:
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import TimeoutError as PlaywrightTimeout
    from playwright.async_api import async_playwright

    deadline = time.monotonic() + settings.timeout
    async with async_playwright() as playwright:
        try:
            # Jarvis's own profile: the sign-in lives there, and Chrome renews Canva's cookies in it as it goes.
            context = await canva_auth.open_profile(
                playwright,
                headless=settings.headless,
                minimized=settings.minimized,
                slow_mo=settings.slow_mo,
                viewport={"width": 1600, "height": 1000},
                accept_downloads=True,
                profile=settings.profile,
            )
        except RuntimeError as exc:
            raise CanvaError("browser", str(exc)) from exc
        run: Optional[_Run] = None
        try:
            context.set_default_timeout(STEP_TIMEOUT * 1000)
            page = context.pages[0] if context.pages else await context.new_page()
            if settings.slow_mo:
                page.set_default_timeout(STEP_TIMEOUT * 1000 * 3)
            run = _Run(page, settings, deadline)
            progress["run"] = run
            await run.open_design()
            for index, slide in enumerate(slides):
                await run.write_slide(index, slide)
                if settings.slow_mo:
                    await asyncio.sleep(settings.slow_mo / 1000)
            await run.download(target)
            return target
        except CanvaError as err:
            if run is not None and err.debug is None:
                err.debug = await run.keep_debug(err.message)
            raise
        except PlaywrightTimeout as exc:
            stage = run.stage if run else "open"
            debug = await run.keep_debug(_first_line(exc)) if run else None
            if time.monotonic() >= deadline - 0.5:
                error = run.out_of_time(_first_line(exc)) if run else CanvaError("timeout", _first_line(exc))
                error.debug = debug
                raise error from exc
            raise CanvaError(stage, f"Canva did not respond in time ({_first_line(exc)})", debug) from exc
        except PlaywrightError as exc:
            stage = run.stage if run else "open"
            debug = await run.keep_debug(_first_line(exc)) if run else None
            raise CanvaError(stage, _first_line(exc), debug) from exc
        except asyncio.CancelledError:
            if run is not None:
                progress["debug"] = await run.keep_debug("ran out of time")
            raise
        finally:
            try:
                await context.close()  # ends Jarvis's Chrome and writes the profile; yours is not touched
            except Exception:  # noqa: BLE001
                pass


def _first_line(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()
    return text[0][:200] if text else type(exc).__name__


def make_presentation(slides: list[CanvaSlide], target: Path, settings: Optional[CanvaSettings] = None) -> Path:
    """
    Builds `slides` in Canva and saves the download as `target`. Blocks until it is done or
    settings.timeout has passed. Raises CanvaError, never anything else.

    The browser runs on a thread of its own with its own event loop, so this can be called
    from anywhere - the brain's tool handlers are plain functions, and a thread that already
    runs an event loop (or one whose loop cannot start a browser: the selector loop uvicorn
    may set on Windows) cannot host Playwright's.

    A hidden run that Cloudflare's check stopped is tried once more in a minimized window
    (settings.window_fallback), in the time that is left.
    """
    settings = settings or CanvaSettings.from_env()
    problem = readiness(settings)
    if problem:
        raise CanvaError("setup", problem)
    started = time.monotonic()
    try:
        return _run_on_a_thread(slides, target, settings)
    except CanvaError as err:
        left = settings.timeout - (time.monotonic() - started)
        if err.stage != "blocked" or not (settings.headless and settings.window_fallback) or left < RETRY_MIN_SECONDS:
            raise
    print(f"[Agent:publishing] Cloudflare stopped the hidden browser; once more in a minimized window ({left:.0f} s left).")
    return _run_on_a_thread(slides, target, replace(settings, headless=False, minimized=True, window_fallback=False, timeout=left))


def _run_on_a_thread(slides: list[CanvaSlide], target: Path, settings: CanvaSettings) -> Path:
    """One Canva run in one browser, on a thread of its own (make_presentation). Raises CanvaError only."""
    progress: dict[str, Any] = {}
    outcome: dict[str, Any] = {}

    def work() -> None:
        loop = asyncio.ProactorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            outcome["path"] = loop.run_until_complete(
                asyncio.wait_for(_make(slides, target, settings, progress), timeout=settings.timeout + GRACE)
            )
        except BaseException as exc:  # noqa: BLE001 - handed to the caller's thread below
            outcome["error"] = exc
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()

    worker = threading.Thread(target=work, name="canva-browser", daemon=True)
    worker.start()
    worker.join(settings.timeout + GRACE + 20)  # the browser's own shutdown after a hard stop gets a little more
    if worker.is_alive():
        raise CanvaError("timeout", f"Canva took longer than {settings.timeout:.0f} seconds and did not stop")
    error = outcome.get("error")
    if error is None:
        return outcome["path"]
    if isinstance(error, CanvaError):
        raise error
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        run = progress.get("run")
        stage = run.stage if run is not None else "open"
        raise CanvaError("timeout", f"Canva took longer than {settings.timeout:.0f} seconds (at {stage})", progress.get("debug"))
    raise CanvaError("error", f"{type(error).__name__}: {_first_line(error)}")


async def _browser_check() -> str:
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        try:
            # A throwaway browser, not the profile: a check must not fail because the sign-in window is open.
            browser = await playwright.chromium.launch(channel=canva_auth.CHANNEL, headless=True)
        except Exception as exc:  # noqa: BLE001
            problem = canva_auth.launch_problem(exc)
            if problem:
                raise RuntimeError(problem) from exc
            raise
        version = browser.version
        await browser.close()
    return version


def check_browser(settings: Optional[CanvaSettings] = None) -> str:
    """Chrome's version when it starts, else why it does not (for --check). Opens no website."""
    if importlib.util.find_spec("playwright") is None:
        return "Playwright is not installed (pip install -r requirements.txt)"
    outcome: dict[str, Any] = {}

    def work() -> None:
        loop = asyncio.ProactorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
        try:
            outcome["version"] = loop.run_until_complete(asyncio.wait_for(_browser_check(), 60))
        except BaseException as exc:  # noqa: BLE001
            outcome["error"] = exc
        finally:
            loop.close()

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(75)
    if "version" in outcome:
        return f"Google Chrome {outcome['version']} starts"
    error = outcome.get("error")
    return f"Google Chrome does not start ({_first_line(error) if error else 'timed out'})"
