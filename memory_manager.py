"""
Module 4: Tiered Memory (local JSON store)

Persists lightweight "what was I working on" context to a local
memory.json file. 100% offline, no database, human-readable and
hand-editable.

Tier 1 (current):  which project is active + when it was opened
Tier 2 (later):    per-project notes, open files, recent commands
Tier 3 (later):    vector/semantic recall over past sessions

The file layout is intentionally simple and forward-compatible:

    {
      "current_project": "subtrack",
      "updated_at": "2026-08-17T20:11:04.123456",
      "projects": {
        "subtrack": {
          "open_count": 3,
          "last_opened": "2026-08-17T20:11:04.123456",
          "notes": []
        }
      }
    }

Every read is defensive: a missing, empty, corrupt, or hand-mangled
memory.json degrades to "no memory yet" rather than crashing the
assistant.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from typing import Any, Optional

# Local JSON store, kept next to this module so it travels with the project.
MEMORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory.json")

# Shape used for a brand-new / unreadable store.
_EMPTY_MEMORY: dict[str, Any] = {
    "current_project": None,
    "updated_at": None,
    "projects": {},
}


def _blank_memory() -> dict[str, Any]:
    """Fresh copy of the empty structure (never hand out the module-level dict)."""
    return {"current_project": None, "updated_at": None, "projects": {}}


def _read_raw() -> dict[str, Any]:
    """
    Loads and validates memory.json.

    Returns a blank memory structure if the file is missing, empty,
    invalid JSON, or not a JSON object - memory is a convenience, so a
    damaged store must never take the assistant down with it.
    """
    if not os.path.exists(MEMORY_FILE):
        return _blank_memory()

    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        print(f"[Memory] Could not read {os.path.basename(MEMORY_FILE)} ({exc}). Starting fresh.")
        return _blank_memory()

    if not isinstance(data, dict):
        print("[Memory] memory.json is not a JSON object. Starting fresh.")
        return _blank_memory()

    # Repair partial/hand-edited files rather than trusting them blindly.
    memory = _blank_memory()
    memory["current_project"] = data.get("current_project")
    memory["updated_at"] = data.get("updated_at")
    projects = data.get("projects")
    memory["projects"] = projects if isinstance(projects, dict) else {}
    return memory


def _write_raw(memory: dict[str, Any]) -> bool:
    """
    Writes memory.json atomically (temp file + os.replace) so an
    interrupted write can never leave a truncated, unparseable store
    behind. Returns True on success.
    """
    directory = os.path.dirname(MEMORY_FILE) or "."
    tmp_path = None
    try:
        # Same directory as the target, so os.replace stays atomic.
        fd, tmp_path = tempfile.mkstemp(prefix=".memory-", suffix=".tmp", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(memory, fh, indent=2, ensure_ascii=False)
        os.replace(tmp_path, MEMORY_FILE)
        return True
    except OSError as exc:
        print(f"[Memory] Failed to save memory: {exc}")
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return False


def save_context(project_name: str) -> dict[str, Any]:
    """
    Records `project_name` as the active project and bumps its stats.

    Returns the project's entry *as it looked before this call* - that
    is what makes "welcome back, you've opened this 3 times" possible.
    For a first-ever open, returns an empty dict.
    """
    memory = _read_raw()
    key = project_name.strip().lower()
    now = datetime.now().isoformat()

    previous = memory["projects"].get(key, {})
    if not isinstance(previous, dict):
        previous = {}

    entry = dict(previous)
    entry["open_count"] = int(previous.get("open_count", 0) or 0) + 1
    entry["last_opened"] = now
    entry.setdefault("notes", [])
    if "first_opened" not in entry:
        entry["first_opened"] = now

    memory["projects"][key] = entry
    memory["current_project"] = key
    memory["updated_at"] = now

    _write_raw(memory)
    return previous


def load_context() -> Optional[dict[str, Any]]:
    """
    Returns the most recent session context, or None if nothing has
    been saved yet.

        {"project": "subtrack", "open_count": 3,
         "last_opened": "...", "notes": [], "updated_at": "..."}
    """
    memory = _read_raw()
    current = memory.get("current_project")
    if not current:
        return None

    entry = memory["projects"].get(current, {})
    if not isinstance(entry, dict):
        entry = {}

    return {
        "project": current,
        "open_count": entry.get("open_count", 0),
        "last_opened": entry.get("last_opened"),
        "first_opened": entry.get("first_opened"),
        "notes": entry.get("notes", []),
        "updated_at": memory.get("updated_at"),
    }


def add_note(project_name: str, note: str) -> bool:
    """Appends a free-form note to a project (Tier 2 groundwork)."""
    memory = _read_raw()
    key = project_name.strip().lower()
    entry = memory["projects"].setdefault(key, {"open_count": 0, "notes": []})
    if not isinstance(entry, dict):
        entry = {"open_count": 0, "notes": []}
        memory["projects"][key] = entry
    notes = entry.setdefault("notes", [])
    if not isinstance(notes, list):
        notes = []
        entry["notes"] = notes
    notes.append({"text": note, "at": datetime.now().isoformat()})
    memory["updated_at"] = datetime.now().isoformat()
    return _write_raw(memory)


def clear_memory() -> bool:
    """Wipes the store back to empty (useful for testing / a fresh start)."""
    return _write_raw(_blank_memory())


def describe_context() -> str:
    """
    Human/TTS-friendly one-liner about the last session. Safe to hand
    straight to VoiceEngine.speak().
    """
    context = load_context()
    if context is None:
        return "No previous project context is stored yet."

    project = str(context["project"]).title()
    count = context.get("open_count", 0)
    if count and count > 1:
        return f"Last session you were working on {project}. You've opened it {count} times."
    return f"Last session you were working on {project}."


if __name__ == "__main__":
    print(f"[Memory] Store: {MEMORY_FILE}")
    print(f"[Memory] Existing context: {load_context()}")
    previous = save_context("subtrack")
    print(f"[Memory] Previous entry for 'subtrack': {previous}")
    print(f"[Memory] Now: {load_context()}")
    print(f"[Memory] Spoken form: {describe_context()}")
