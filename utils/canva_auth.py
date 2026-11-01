"""
Signs Jarvis in to Canva - once, by hand - and opens the Chrome the Publishing Agent drives.

    python -m utils.canva_auth

opens Google Chrome at https://www.canva.com/ in a window of its own. Sign in there yourself
(email, Google, Apple, a code, a "Verify you are human" check: whatever Canva asks), come back
to this terminal and press Enter. The sign-in stays in Jarvis's own Chrome profile,
.data/canva_profile, and the Publishing Agent opens Canva with that profile, already signed
in (agents/canva_browser.py).

JARVIS'S OWN CHROME. The profile is a separate Chrome user-data folder - its own cookies,
history and windows - so Jarvis's Chrome and the Chrome you use run side by side without
touching each other: yours keeps its windows, tabs and accounts while Jarvis works, and
Jarvis's runs never open in your windows. Only one program can have the profile open at a
time: close this script's window before Jarvis makes a deck.

THE FOLDER IS A KEY TO YOUR CANVA ACCOUNT: whoever has a copy is signed in as you. .data/ is
git-ignored (the repository is public) and the Docker image never copies it; do not share it.
Signing out of Canva on all devices, or changing the password, makes it useless; deleting
the folder signs Jarvis out. Canva ends sessions after a while: when Jarvis says the Canva
session has ended, run this again.

STEALTH. Canva sits behind Cloudflare's bot check, and a browser that Playwright drives says
so: navigator.webdriver is true, and headless Chrome names itself "HeadlessChrome" in every
request. Measured on canva.com on 2026-11-01, one page load each, signed out:
    headless Chrome                                      "Just a moment..." for 20 s, never the page
    + playwright-stealth                                 the same
    + playwright-stealth + the user agent of a window    Canva's home page at once
    a visible window + playwright-stealth                Canva's home page at once
So every page gets playwright-stealth's patches before it loads anything (apply_stealth), and
a headless run sends the user agent Chrome sends with a window (_window_user_agent) - the
same one this script signs in with, which keeps Cloudflare's clearance cookie valid for it.
Nothing here clicks or solves a challenge: when one needs a person, you do it by hand in this
script's window. Canva's terms restrict automated use of the site: this is your call and your
account.

If Google refuses the sign-in ("this browser may not be secure"), use "Continue with email":
Google blocks sign-ins in a browser that a program controls.

SETTINGS (.env), read by agents/canva_browser.py too
    JARVIS_CANVA_PROFILE  Jarvis's Chrome profile folder [.data/canva_profile]; a relative path
                          is read from the Jarvis folder

    python -m utils.canva_auth --headless --url <address>   (tests only: no window to sign in with)
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
CANVA_URL = "https://www.canva.com/"
DEFAULT_PROFILE = ".data/canva_profile"
# Google Chrome as installed on this computer - the real browser, not Playwright's test build.
CHANNEL = "chrome"
# Written into the profile folder after a sign-in: when, and how many Canva cookies. No secrets.
SIGNED_IN_FILE = "jarvis-canva-signin.json"
# The session file the first version (245bf21) saved; nothing reads it any more.
OLD_STATE_FILE = "canva_state.json"
# A window that has to be visible (headless was stopped) opens off screen and is then minimized:
# Chrome ignores --start-minimized (measured), and this way it never shows up over your work.
OFF_SCREEN = "--window-position=-32000,-32000"
# What playwright-stealth adds itself when it launches a browser. It does not hook persistent
# contexts (a TODO in its source), so it is passed here. It keeps Chrome itself from setting
# navigator.webdriver; playwright-stealth's script patches the same property in pages.
STEALTH_ARGS = ["--disable-blink-features=AutomationControlled"]
# Chrome's exit code when another Chrome already has the profile open (RESULT_CODE_PROFILE_IN_USE).
_PROFILE_IN_USE = re.compile(r"exitCode=21\b")
_LOG_IN = re.compile(r"^\s*(log in|sign in)\s*$", re.I)

_ENV_LOADED = False
_USER_AGENTS: dict[str, str] = {}  # the channel -> the user agent its windows send (one look per process)


def setting(name: str, default: str = "") -> str:
    """A setting from the environment or the Jarvis .env file (the environment wins)."""
    global _ENV_LOADED
    if not _ENV_LOADED:
        _ENV_LOADED = True
        try:
            from dotenv import load_dotenv

            load_dotenv(ROOT / ".env", override=False)
        except ImportError:  # python-dotenv is in requirements.txt; without it only the environment counts
            pass
    return (os.environ.get(name) or "").strip() or default


def profile_path() -> Path:
    """Jarvis's Chrome profile folder (JARVIS_CANVA_PROFILE, relative to the Jarvis folder)."""
    path = Path(setting("JARVIS_CANVA_PROFILE", DEFAULT_PROFILE)).expanduser()
    return path if path.is_absolute() else ROOT / path


def signed_in_file(profile: Optional[Path] = None) -> Path:
    """The note a sign-in leaves in the profile: there = signed in (as far as anyone knows here)."""
    return (profile or profile_path()) / SIGNED_IN_FILE


def signed_in_record(profile: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """What the last sign-in noted ({"signed_in_at", "canva_cookies"}), or None."""
    try:
        record = json.loads(signed_in_file(profile).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def canva_cookie_count(cookies: list[dict[str, Any]]) -> int:
    """How many of these cookies are Canva's."""
    return sum(1 for cookie in cookies if "canva" in str(cookie.get("domain", "")).lower())


def stealth_problem() -> str:
    """"" when playwright-stealth (2.0 or later) can be used; else what to install."""
    if importlib.util.find_spec("playwright_stealth") is None:
        return "playwright-stealth is not installed (pip install -r requirements.txt)"
    try:
        import playwright_stealth
    except ImportError as exc:  # installed but broken (a Playwright it cannot import, ...)
        return f"playwright-stealth cannot be loaded ({exc})"
    if not hasattr(playwright_stealth, "Stealth"):  # 2.0 replaced stealth_async() with the Stealth class
        return "playwright-stealth is older than 2.0 (pip install -U \"playwright-stealth>=2.0.3\")"
    return ""


async def apply_stealth(context: Any) -> bool:
    """
    playwright-stealth's patches on every page of `context`, present and future, before their
    next page load. False when playwright-stealth cannot be used (stealth_problem says why).
    """
    if stealth_problem():
        return False
    from playwright_stealth import Stealth

    # Its WebGL patch reports a Mac's GPU ("Intel Iris OpenGL Engine") on this Windows PC, whose
    # Chrome reports the real one: off, so the browser stays consistent with itself (measured).
    await Stealth(webgl_vendor=False).apply_stealth_async(context)
    return True


async def _window_user_agent(playwright: Any) -> str:
    """
    The user agent Chrome sends with a window, for a headless run: headless Chrome sends
    "HeadlessChrome/<version>" with every request, playwright-stealth only fixes what pages
    read in JavaScript, and Cloudflare stopped that mix (2026-11-01). Read from a throwaway
    headless Chrome once per process, so it always names the Chrome installed now.
    """
    if CHANNEL not in _USER_AGENTS:
        browser = await playwright.chromium.launch(channel=CHANNEL, headless=True)
        try:
            page = await browser.new_page()
            agent = await page.evaluate("navigator.userAgent")
        finally:
            await browser.close()
        _USER_AGENTS[CHANNEL] = str(agent).replace("HeadlessChrome/", "Chrome/")
    return _USER_AGENTS[CHANNEL]


def launch_problem(exc: BaseException, profile: Optional[Path] = None) -> str:
    """What a failed Chrome start means in plain words, or "" when it is not one of the known causes."""
    text = str(exc)
    if _PROFILE_IN_USE.search(text):
        return (
            f"Jarvis's Chrome profile ({profile or profile_path()}) is already open - the sign-in window "
            "(python -m utils.canva_auth) or another Canva run. Close it and try again"
        )
    lowered = text.lower()
    if "executable doesn't exist" in lowered or "is not found at" in lowered or "not installed" in lowered:
        return "Google Chrome is not installed - install it from https://www.google.com/chrome/ (or run: python -m playwright install chrome)"
    return ""


async def _minimize(context: Any) -> None:
    """Minimizes the context's window (it already opened off screen); a no-op where Chrome refuses."""
    try:
        page = context.pages[0] if context.pages else await context.new_page()
        session = await context.new_cdp_session(page)
        window = await session.send("Browser.getWindowForTarget")
        await session.send("Browser.setWindowBounds", {"windowId": window["windowId"], "bounds": {"windowState": "minimized"}})
        await session.detach()
    except Exception:  # noqa: BLE001 - off screen is out of the way too
        pass


async def open_profile(
    playwright: Any,
    *,
    headless: bool,
    minimized: bool = False,
    slow_mo: int = 0,
    viewport: Optional[dict[str, int]] = None,
    accept_downloads: bool = False,
    profile: Optional[Path] = None,
) -> Any:
    """
    Jarvis's Chrome on its own profile (a Playwright BrowserContext; closing it ends the browser),
    with playwright-stealth applied before any page loads. `minimized` puts a visible window out of
    the way. Raises RuntimeError saying what to do when Chrome is missing or the profile is in use.
    """
    path = profile or profile_path()
    path.mkdir(parents=True, exist_ok=True)
    options: dict[str, Any] = {
        "user_data_dir": str(path),
        "channel": CHANNEL,
        "headless": headless,
        "slow_mo": slow_mo,
        "locale": "en-US",
        "accept_downloads": accept_downloads,
        "args": STEALTH_ARGS + ([OFF_SCREEN] if minimized and not headless else []),
    }
    if viewport:
        options["viewport"] = viewport
    elif not headless:
        options["no_viewport"] = True  # a window you sign in with keeps its own size
    try:
        if headless:
            options["user_agent"] = await _window_user_agent(playwright)
        context = await playwright.chromium.launch_persistent_context(**options)
    except Exception as exc:  # noqa: BLE001 - Playwright's own Error; its text says what went wrong
        problem = launch_problem(exc, path)
        if problem:
            raise RuntimeError(problem) from exc
        raise
    try:
        await apply_stealth(context)
        if minimized and not headless:
            await _minimize(context)
    except BaseException:
        await context.close()
        raise
    return context


async def _press_enter(prompt: str, wait: Callable[[str], Any]) -> None:
    """Waits for Enter without blocking the event loop (or exiting with a thread still reading the terminal)."""
    loop = asyncio.get_running_loop()
    answered: asyncio.Future[Any] = loop.create_future()

    def ask() -> None:
        try:
            value = wait(prompt)
        except BaseException as exc:  # noqa: BLE001 - EOFError, KeyboardInterrupt: handed to the loop
            error = exc  # `exc` itself is gone once this block ends, before the loop runs the callback
            loop.call_soon_threadsafe(lambda: answered.done() or answered.set_exception(error))
            return
        loop.call_soon_threadsafe(lambda: answered.done() or answered.set_result(value))

    threading.Thread(target=ask, name="canva-auth-enter", daemon=True).start()
    await answered


async def _looks_signed_out(context: Any) -> bool:
    """True when the newest page is Canva's log-in page or still offers "Log in"."""
    if not context.pages:
        return False
    page = context.pages[-1]
    if re.search(r"/(login|signup)\b", page.url):
        return True
    for role in ("button", "link"):
        try:
            if await page.get_by_role(role, name=_LOG_IN).first.is_visible():
                return True
        except Exception:  # noqa: BLE001 - a page that is navigating: not known to be signed out
            pass
    return False


async def sign_in(
    *, url: str = CANVA_URL, headless: bool = False, profile: Optional[Path] = None, wait: Callable[[str], Any] = input
) -> tuple[Path, dict[str, Any], bool]:
    """
    Opens Jarvis's Chrome at `url`, waits for Enter, then notes the sign-in in the profile.
    Returns (the profile, what was noted, signed_out). A sign-in that still shows "Log in" is
    not noted, and an older note is removed, so the agent does not try Canva signed out.
    """
    from playwright.async_api import async_playwright

    path = profile or profile_path()
    async with async_playwright() as playwright:
        context = await open_profile(playwright, headless=headless, profile=path)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(url, wait_until="domcontentloaded")
            print(
                "\nJarvis's Chrome is open at Canva, in a window of its own (your own Chrome is not touched).\n"
                "Sign in there (use \"Continue with email\" if Google refuses), do any check Canva shows,\n"
                "wait until you see your Canva home page, then come back here."
            )
            await _press_enter("Press Enter when you are signed in (Ctrl+C to cancel)... ", wait)
            cookies = await context.cookies()
            signed_out = await _looks_signed_out(context)
        finally:
            await context.close()  # also writes Chrome's cookies to the profile
    record = {"signed_in_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "canva_cookies": canva_cookie_count(cookies)}
    note = signed_in_file(path)
    if signed_out:
        note.unlink(missing_ok=True)
    else:
        note.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path, record, signed_out


def _ignored_by_git(path: Path) -> Optional[bool]:
    """True/False whether git ignores `path`; None when git cannot say (no git, outside the repository)."""
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "check-ignore", "-q", str(path)], capture_output=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return {0: True, 1: False}.get(result.returncode)


def main(argv: list[str]) -> int:
    url = CANVA_URL
    headless = "--headless" in argv
    if "--url" in argv:
        index = argv.index("--url")
        if index + 1 >= len(argv):
            print("--url needs an address.")
            return 2
        url = argv[index + 1]
    problem = stealth_problem()
    if problem:
        print(f"WARNING: {problem}. You can sign in without it, but the Publishing Agent will not use Canva until it is installed.")
    try:
        path, record, signed_out = asyncio.run(sign_in(url=url, headless=headless))
    except KeyboardInterrupt:
        print("\nCancelled; the sign-in was not noted.")
        return 1
    except EOFError:
        print("\nThere is no terminal to press Enter in, so the sign-in was not noted. Run this from a terminal.")
        return 1
    except RuntimeError as exc:
        print(f"Could not open Chrome: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 - the window was closed before Enter, Canva unreachable, ...
        print(f"The sign-in was not noted: {type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}")
        print("If you closed the browser window, run this again and press Enter while it is still open.")
        return 1

    if signed_out:
        print(f"Canva still shows \"Log in\", so Jarvis is NOT signed in (profile: {path}). Run this again and sign in first.")
        return 2
    print(f"Signed in. Jarvis's Chrome profile is {path} ({record['canva_cookies']} Canva cookies).")
    if path.is_relative_to(ROOT) and _ignored_by_git(path) is False:
        print(f"WARNING: git does NOT ignore {path} - add it to .gitignore before you commit anything.")
    old = ROOT / OLD_STATE_FILE
    if old.is_file():
        print(f"{old} is from the earlier sign-in method and is no longer used: delete it.")
    print("Keep that folder private: it signs whoever has it in to your Canva account.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
