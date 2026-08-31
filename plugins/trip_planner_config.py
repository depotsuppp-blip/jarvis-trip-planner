"""
Trip Planner link config - the ONE place LIFF_ID is read and the ONE
place every user-facing trip link (poll, draft board) is built.

Split out from plugins/trip_planner.py so this stays a small,
dependency-free module (no google-genai/anthropic/requests imports) -
anything that just needs to build a link a user will open doesn't need
to pull in the Planner Agent machinery too.
"""

import os

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - matches trip_planner.py's own fallback
    load_dotenv = None

# Where the standalone Trip Planner app (TripPlanner_Web) is deployed.
# Overridable via TRIP_PLANNER_APP_URL in .env. Used two ways: as
# build_liff_link's fallback base when TRIP_PLANNER_LIFF_ID isn't
# configured, and as the base plugins/trip_planner.py's
# _fetch_poll_data GETs /api/poll/<trip_id> from directly (a
# server-to-server call, never a link a user opens, so it never goes
# through build_liff_link).
DEFAULT_APP_URL = "https://jarvis-trip-planner.vercel.app"


def _ensure_env_loaded() -> None:
    """Best-effort .env load, mirroring plugins/trip_planner.py."""
    if load_dotenv is None:
        return
    here = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(os.path.dirname(here), ".env")
    try:
        load_dotenv(env_path, override=False)
    except Exception:  # noqa: BLE001 - callers still work with real env vars
        pass


def get_liff_id() -> str:
    """The LINE LIFF app id - must match TripPlanner_Web's NEXT_PUBLIC_LIFF_ID (see .env.example)."""
    _ensure_env_loaded()
    return (os.getenv("TRIP_PLANNER_LIFF_ID") or "").strip()


def get_app_base_url() -> str:
    """Where TripPlanner_Web is deployed - defaults to DEFAULT_APP_URL."""
    _ensure_env_loaded()
    return (os.getenv("TRIP_PLANNER_APP_URL") or DEFAULT_APP_URL).strip()


def _is_production() -> bool:
    _ensure_env_loaded()
    return (os.getenv("ENV") or "").strip().lower() == "production"


def assert_link_not_localhost_in_production(link: str) -> None:
    """
    Fails loudly instead of silently handing a real user a dead link.

    A "localhost" link only ever resolves on the machine that generated
    it - harmless during local development (TRIP_PLANNER_APP_URL
    legitimately points at http://localhost:3000 for `next dev`), but
    useless if it ever reaches a real user's phone over LINE in
    production. Only enforced when ENV=production, so local dev is
    unaffected; every other environment (unset, "development", etc.)
    is left alone.
    """
    if not _is_production():
        return
    if "localhost" in link:
        raise RuntimeError(
            f"Refusing to send a localhost link in production: {link!r}. "
            "Set TRIP_PLANNER_LIFF_ID (preferred) or a real TRIP_PLANNER_APP_URL."
        )


def build_liff_link(path: str) -> str:
    """
    Builds a user-facing trip link as https://liff.line.me/{LIFF_ID}{path}.

    LINE only opens its embedded in-app browser - with the voter's LINE
    session already attached, which app/trip/poll/[id]/page.tsx's
    liff.isLoggedIn()/liff.login() rely on - for a liff.line.me link. A
    bare https://*.vercel.app (or http://localhost:3000) link opens in
    the device's default browser instead and never carries LINE auth
    context. TRIP_PLANNER_LIFF_ID must hold the same LIFF app id as
    TripPlanner_Web's NEXT_PUBLIC_LIFF_ID (see .env.example) - both name
    the one LIFF app registered in the LINE Developers console.

    path must start with "/" (e.g. "/trip/poll/<id>"). Falls back to a
    plain get_app_base_url() link, logged as a warning, only when
    TRIP_PLANNER_LIFF_ID isn't configured - so a link is still sent
    rather than the caller's background thread crashing outright.
    assert_link_not_localhost_in_production is what stops that fallback
    from silently handing out a dead localhost link once ENV=production.
    """
    if not path.startswith("/"):
        path = f"/{path}"

    liff_id = get_liff_id()
    if liff_id:
        link = f"https://liff.line.me/{liff_id}{path}"
    else:
        print(
            f"[TripPlanner] TRIP_PLANNER_LIFF_ID is not configured; {path} "
            "link will open in the system browser instead of LINE's in-app browser."
        )
        link = f"{get_app_base_url()}{path}"

    assert_link_not_localhost_in_production(link)
    return link
