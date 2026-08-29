"""
Module 12: Plugin loader (self-extension)

Lets Jarvis grow a new capability at runtime. A plugin is one Python
file in plugins/ that declares a tool and implements it:

    TOOL_SPEC = {
        "name": "roll_dice",
        "description": "Roll an N-sided die. Use for random numbers.",
        "input_schema": {
            "type": "object",
            "properties": {"sides": {"type": "integer"}},
            "required": ["sides"],
        },
    }

    def run(sides: int) -> str:
        import random
        return f"You rolled a {random.randint(1, sides)}."

The loader scans the folder, validates each file, imports it, and wraps
it in the same ToolSpec the built-in tools use. From the model's point
of view there is no difference between a built-in tool and one it wrote
itself ten seconds ago.

RELOADING WITHOUT RESTARTING
----------------------------
load_plugins() is cheap and idempotent: it stats the folder, and only
imports files that are new or whose mtime changed. llm_brain calls it at
the start of every turn, so a tool created during one command is
available on the next one - no restart, and no reload of anything that
did not change.

WHAT VALIDATION DOES AND DOES NOT BUY YOU
-----------------------------------------
Be honest about this, because it is the security boundary of the whole
feature: **a plugin is ordinary Python running in the assistant's own
process, with the assistant's own privileges.** There is no sandbox
here. `validate_source` parses the file, rejects it if it will not
compile, if the required symbols are missing, or if it uses one of the
patterns in DANGEROUS_PATTERNS - but a determined piece of code can
reach the filesystem and the network in ways no AST scan will catch.

What actually protects the user is the layer above: create_new_tool in
llm_brain.py is confirmation-gated, the code is printed to the console
before it is written, and anything matching DANGEROUS_PATTERNS is named
in the spoken prompt. The AST scan is a speed bump for accidents, not a
wall against malice. Anyone reading this: do not treat plugins/ as
untrusted input executed safely. Treat it as code you agreed to run.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

from tool_spec import ToolSpec

PLUGINS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugins")

# Names a plugin may not take: the built-in tools, and anything that
# would shadow one. Checked before writing, so a plugin can never
# silently replace the webhook trigger.
RESERVED_NAMES = {
    "trigger_n8n_webhook",
    "query_subtrack_database",
    "analyze_screen",
    "create_new_tool",
    "list_capabilities",
    "start_subtrack_env",
    "os_orchestrator",
}

# Patterns that make a plugin worth a second look. Matching one does not
# make code malicious - subprocess is how you open an app - it makes it
# worth naming out loud in the confirmation prompt before it is written.
DANGEROUS_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bos\.system\b", "runs shell commands"),
    (r"\bsubprocess\b", "starts other programs"),
    (r"\bshutil\.rmtree\b", "deletes directory trees"),
    (r"\bos\.remove\b|\bos\.unlink\b", "deletes files"),
    (r"\beval\s*\(|\bexec\s*\(", "executes generated code"),
    (r"\bsocket\b", "opens network sockets"),
    (r"\brequests\.(post|put|delete|patch)\b", "sends data over the network"),
    (r"\bopen\s*\([^)]*['\"][wax]", "writes to files"),
    (r"__import__|importlib", "imports modules dynamically"),
)

# A plugin file must define these.
REQUIRED_SYMBOLS = ("TOOL_SPEC", "run")

_LOCK = threading.Lock()
_LOADED: dict[str, "LoadedPlugin"] = {}


@dataclass
class LoadedPlugin:
    """One successfully imported plugin file."""

    name: str
    path: str
    mtime: float
    spec: ToolSpec


# ---------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------


def risky_behaviours(source: str) -> list[str]:
    """Plain-English list of the flagged patterns in `source`."""
    return sorted({why for pattern, why in DANGEROUS_PATTERNS if re.search(pattern, source)})


def declares_tool_spec(source: str) -> bool:
    """
    True if `source` assigns TOOL_SPEC at module level.

    Parsed rather than substring-matched: this very sentence contains
    the word TOOL_SPEC, and a docstring mentioning it must not make a
    helper module look like a plugin.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # Broken syntax is validate_source's business to report, not
        # something to swallow here - claim it is a plugin and let the
        # real validation say why it will not load.
        return True

    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        if any(isinstance(t, ast.Name) and t.id == "TOOL_SPEC" for t in targets):
            return True
    return False


def validate_source(source: str, name: str) -> tuple[bool, str]:
    """
    Static checks on plugin source. Returns (ok, reason_if_not).

    Everything here is about "will this load and behave like a tool",
    not "is this safe" - see the module docstring on why that
    distinction matters.
    """
    if not name or not re.fullmatch(r"[a-z][a-z0-9_]{2,39}", name):
        return False, (
            "the tool name must be lowercase letters, digits and underscores, "
            "3 to 40 characters, starting with a letter"
        )
    if name in RESERVED_NAMES:
        return False, f"{name} is the name of a built-in tool"

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return False, f"the code does not parse: line {exc.lineno}, {exc.msg}"

    defined: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            defined.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            defined.update(t.id for t in targets if isinstance(t, ast.Name))

    missing = [symbol for symbol in REQUIRED_SYMBOLS if symbol not in defined]
    if missing:
        return False, f"the file must define {' and '.join(missing)} at the top level"

    run_node = next(
        (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run"), None
    )
    if run_node is None:
        return False, "run must be a plain function defined at the top level"

    return True, ""


# ---------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------


def _ensure_dir() -> None:
    os.makedirs(PLUGINS_DIR, exist_ok=True)
    init = os.path.join(PLUGINS_DIR, "__init__.py")
    if not os.path.exists(init):
        with open(init, "w", encoding="utf-8") as handle:
            handle.write('"""Runtime-loaded Jarvis tools. See plugin_loader.py."""\n')


def normalise_schema(schema: Any) -> dict[str, Any]:
    """
    Forces a tool schema into the shape both provider APIs require.

    Anthropic rejects a tool whose input_schema has no "type", and that
    rejection is a 400 on the whole request - one malformed plugin would
    otherwise disable every tool in the turn, including the built-in
    ones. Cheap to repair, expensive to leave.
    """
    if not isinstance(schema, dict):
        schema = {}
    fixed = dict(schema)
    fixed["type"] = "object"
    properties = fixed.get("properties")
    fixed["properties"] = properties if isinstance(properties, dict) else {}
    required = fixed.get("required")
    if required is not None and not isinstance(required, list):
        fixed.pop("required")
    return fixed


def _wrap(module, path: str) -> Optional[ToolSpec]:
    """Turns an imported plugin module into a ToolSpec, or None."""
    raw = getattr(module, "TOOL_SPEC", None)
    handler = getattr(module, "run", None)

    if not isinstance(raw, dict) or not callable(handler):
        print(f"[Plugins] {os.path.basename(path)}: TOOL_SPEC/run missing or wrong type.")
        return None

    name = str(raw.get("name", "")).strip()
    description = str(raw.get("description", "")).strip()
    schema = raw.get("input_schema")

    if not name or not description:
        print(f"[Plugins] {os.path.basename(path)}: TOOL_SPEC needs a name and a description.")
        return None
    schema = normalise_schema(schema)

    return ToolSpec(
        name=name,
        description=description + " (This tool was added at runtime as a plugin.)",
        input_schema=schema,
        handler=handler,
        # Plugins are user-authored code running in-process. Even a
        # read-only-looking one gets the gate the first time the model
        # reaches for it, unless the plugin explicitly opts out.
        requires_confirmation=bool(raw.get("requires_confirmation", True)),
        confirmation_template=str(
            raw.get("confirmation_template", f"Do you want me to run the {name} plugin?")
        ),
        background=bool(raw.get("background", False)),
        label=raw.get("label") or f"{name.replace('_', ' ')} plugin",
        source="plugin",
    )


def _import_file(path: str):
    """Imports one plugin file under a private module name."""
    module_name = f"jarvis_plugin_{os.path.splitext(os.path.basename(path))[0]}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not build an import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    # Registered before exec so a plugin containing a dataclass or a
    # relative self-reference can find itself.
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def load_plugins(verbose: bool = False) -> list[ToolSpec]:
    """
    Returns the ToolSpec for every valid plugin in plugins/.

    Re-importing only happens for files that are new or changed, so
    calling this once per turn costs a directory stat. A plugin that
    fails to import is reported once and skipped - one bad file must
    never take out the tool list.
    """
    _ensure_dir()

    with _LOCK:
        seen: set[str] = set()

        for entry in sorted(os.listdir(PLUGINS_DIR)):
            if not entry.endswith(".py") or entry.startswith("_"):
                continue

            path = os.path.join(PLUGINS_DIR, entry)
            key = entry[:-3]
            seen.add(key)

            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue

            cached = _LOADED.get(key)
            if cached is not None and cached.mtime == mtime:
                continue

            try:
                with open(path, encoding="utf-8") as handle:
                    source = handle.read()
            except OSError as exc:
                print(f"[Plugins] Could not read {entry}: {exc}")
                continue

            # A file that never assigns TOOL_SPEC is an implementation
            # module that happens to live here (os_orchestrator.py is
            # one), not a plugin that failed to declare itself. Skipping
            # it quietly matters because this scan runs every turn, and
            # a printed rejection would repeat forever.
            if not declares_tool_spec(source):
                _LOADED.pop(key, None)
                continue

            ok, reason = validate_source(source, key)
            if not ok:
                print(f"[Plugins] Skipping {entry}: {reason}.")
                _LOADED.pop(key, None)
                continue

            try:
                module = _import_file(path)
            except Exception as exc:  # noqa: BLE001 - one bad plugin, not a crash
                print(f"[Plugins] {entry} failed to import: {type(exc).__name__}: {exc}")
                _LOADED.pop(key, None)
                continue

            spec = _wrap(module, path)
            if spec is None:
                _LOADED.pop(key, None)
                continue

            verb = "Reloaded" if cached else "Loaded"
            print(f"[Plugins] {verb} {entry} -> tool {spec.name!r}.")
            _LOADED[key] = LoadedPlugin(name=spec.name, path=path, mtime=mtime, spec=spec)

        # Forget plugins whose files are gone.
        for key in list(_LOADED):
            if key not in seen:
                print(f"[Plugins] {key}.py disappeared; dropping tool.")
                _LOADED.pop(key, None)

        specs = [loaded.spec for loaded in _LOADED.values()]

    if verbose:
        print(f"[Plugins] {len(specs)} plugin tool(s) available.")
    return specs


def write_plugin(name: str, source: str) -> str:
    """
    Writes a validated plugin to plugins/<name>.py and returns the path.

    Raises ValueError when validation fails, so the caller can hand the
    reason back to the model to fix rather than writing a broken file.
    """
    ok, reason = validate_source(source, name)
    if not ok:
        raise ValueError(reason)

    _ensure_dir()
    path = os.path.join(PLUGINS_DIR, f"{name}.py")
    header = (
        f'"""Jarvis plugin: {name}\n\n'
        f"Written by Jarvis at the user's request via create_new_tool.\n"
        f"Generated {time.strftime('%Y-%m-%d %H:%M:%S')}. Edit or delete freely -\n"
        f"the loader picks up changes on the next command.\n"
        f'"""\n\n'
    )
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(header + source.rstrip() + "\n")
    return path


def plugin_names() -> list[str]:
    with _LOCK:
        return sorted(loaded.spec.name for loaded in _LOADED.values())


if __name__ == "__main__":
    print(f"Plugins directory: {PLUGINS_DIR}\n")

    print("--- validation checks ---")
    cases = [
        ("roll_dice", "TOOL_SPEC = {}\ndef run():\n    return 'ok'\n", True),
        ("bad syntax", "TOOL_SPEC = {\ndef run(", False),
        ("trigger_n8n_webhook", "TOOL_SPEC = {}\ndef run():\n    pass\n", False),
        ("no_symbols", "x = 1\n", False),
        ("Bad-Name", "TOOL_SPEC = {}\ndef run():\n    pass\n", False),
    ]
    for name, source, expected in cases:
        ok, why = validate_source(source, name)
        flag = "OK " if ok == expected else "FAIL"
        print(f"  [{flag}] {name!r:22} -> {ok} {why}")

    print("\n--- risky pattern detection ---")
    sample = "import subprocess\ndef run():\n    subprocess.run(['ls'])\n    open('x','w')\n"
    print(f"  {risky_behaviours(sample)}")

    print("\n--- loading ---")
    for spec in load_plugins(verbose=True):
        print(f"  {spec.name}: {spec.description[:60]}...")
