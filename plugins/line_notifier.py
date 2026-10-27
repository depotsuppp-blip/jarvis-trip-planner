"""
Moved to utils/line_notifier.py. This module re-exports it so the existing
imports (`from plugins import line_notifier` in plugins/trip_planner.py) keep
working, and so a test that replaces plugins.line_notifier.send_line_notification
still catches the Trip Planner's messages.

It is not an auto-loaded plugin: it declares no TOOL_SPEC, so plugin_loader
skips it.
"""

from utils.line_notifier import (  # noqa: F401 - re-exported
    LINE_PUSH_URL,
    MAX_MESSAGE_LENGTH,
    configured,
    push_line_notification,
    send_line_notification,
)
