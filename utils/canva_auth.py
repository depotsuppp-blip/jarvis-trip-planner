"""
Signs Jarvis in to Canva - once, by hand - for the Publishing Agent.

    python -m utils.canva_auth

opens a real browser window at https://www.canva.com/. Sign in there yourself (email,
Google, Apple, a CAPTCHA, a code: whatever Canva asks), come back to this terminal and press
Enter. The browser's state - Canva's cookies and site storage - is saved as canva_state.json
in the Jarvis folder, and the Publishing Agent opens Canva with it, already signed in
(agents/canva_browser.py).

THE FILE IS A KEY TO YOUR CANVA ACCOUNT: whoever has it is signed in as you. It is in
.gitignore (the repository is public) and the Docker image never copies it; do not share it.
Signing out of Canva on all devices, or changing the password, makes it useless. Canva ends
sessions after a while: when Jarvis says the Canva session has ended, run this again.

If Google refuses the sign-in ("this browser may not be secure"), use "Continue with email":
Google blocks sign-ins in a browser that a program controls.

SETTINGS (.env), read by agents/canva_browser.py too
    JARVIS_CANVA_STATE    where the session file goes [canva_state.json]; a relative path is
                          read from the Jarvis folder
    JARVIS_CANVA_BROWSER  chromium - Playwright's own (python -m playwright install chromium)
                          chrome   - the Google Chrome installed on this computer
                          msedge   - Microsoft Edge                              [chromium]
                          Sign in with the same browser the agent will use.

    python -m utils.canva_auth --headless --url <address>   (tests only: no window to sign in with)
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
CANVA_URL = "https://www.canva.com/"
DEFAULT_STATE_FILE = "canva_state.json"
# JARVIS_CANVA_BROWSER -> Playwright's channel (None = its own Chromium build).
BROWSERS: dict[str, Optional[str]] = {"chromium": None, "chrome": "chrome", "msedge": "msedge"}

_ENV_LOADED = False


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


def state_path() -> Path:
    """Where the saved Canva session lives (JARVIS_CANVA_STATE, relative to the Jarvis folder)."""
    path = Path(setting("JARVIS_CANVA_STATE", DEFAULT_STATE_FILE)).expanduser()
    return path if path.is_absolute() else ROOT / path


def browser_name() -> str:
    name = setting("JARVIS_CANVA_BROWSER", "chromium").lower()
    return name if name in BROWSERS else "chromium"


async def launch(playwright: Any, headless: bool, slow_mo: int = 0) -> Any:
    """The browser JARVIS_CANVA_BROWSER names. A RuntimeError that says what to install when it is missing."""
    name = browser_name()
    try:
        return await playwright.chromium.launch(headless=headless, channel=BROWSERS[name], slow_mo=slow_mo)
    except Exception as exc:  # noqa: BLE001 - Playwright's own Error; its text says what is missing
        text = str(exc)
        if "Executable doesn't exist" in text or "is not found" in text or "not installed" in text.lower():
            hint = (
                "run: python -m playwright install chromium (or set JARVIS_CANVA_BROWSER=chrome to use Google Chrome)"
                if name == "chromium"
                else f"install {name}, or set JARVIS_CANVA_BROWSER=chromium and run: python -m playwright install chromium"
            )
            raise RuntimeError(f"the {name} browser is not installed - {hint}") from exc
        raise


def canva_cookie_count(state: dict[str, Any]) -> int:
    """How many of the saved cookies are Canva's."""
    return sum(1 for cookie in state.get("cookies", []) if "canva" in str(cookie.get("domain", "")).lower())


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


async def sign_in(
    *, url: str = CANVA_URL, headless: bool = False, path: Optional[Path] = None, wait: Callable[[str], Any] = input
) -> tuple[Path, dict[str, Any]]:
    """Opens the browser at `url`, waits for Enter, saves the session to `path`. Returns (path, the state)."""
    from playwright.async_api import async_playwright

    target = path or state_path()
    async with async_playwright() as playwright:
        browser = await launch(playwright, headless)
        try:
            # A headed window keeps its own size, like any browser you sign in with.
            context = await browser.new_context(locale="en-US", no_viewport=not headless)
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded")
            print(
                "\nA browser window is open at Canva. Sign in there (use \"Continue with email\" if Google refuses),\n"
                "wait until you see your Canva home page, then come back here."
            )
            await _press_enter("Press Enter to save the session (Ctrl+C to cancel)... ", wait)
            target.parent.mkdir(parents=True, exist_ok=True)
            state = await context.storage_state(path=str(target))
        finally:
            await browser.close()
    try:
        os.chmod(target, 0o600)  # only you may read it (a no-op beyond read-only on Windows)
    except OSError:
        pass
    return target, state


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
    try:
        path, state = asyncio.run(sign_in(url=url, headless=headless))
    except KeyboardInterrupt:
        print("\nCancelled; nothing was saved.")
        return 1
    except EOFError:
        print("\nThere is no terminal to press Enter in, so nothing was saved. Run this from a terminal.")
        return 1
    except RuntimeError as exc:
        print(f"Could not open the browser: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 - the window was closed before Enter, Canva unreachable, ...
        print(f"Nothing was saved: {type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}")
        print("If you closed the browser window, run this again and press Enter while it is still open.")
        return 1

    cookies = canva_cookie_count(state)
    print(f"Saved the Canva session to {path} ({cookies} Canva cookies).")
    if cookies == 0:
        print("WARNING: it holds no Canva cookies - were you signed in? Run this again if Jarvis says it is signed out.")
    if path.is_relative_to(ROOT) and _ignored_by_git(path) is False:
        print(f"WARNING: git does NOT ignore {path} - add it to .gitignore before you commit anything.")
    print("Keep that file private: it signs whoever has it in to your Canva account.")
    return 0 if cookies else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
