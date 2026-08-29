"""
The tool contract, in one place.

ToolSpec used to live in llm_brain.py. It moved here so plugins can be
loaded without importing the brain, which imports the plugin loader -
a cycle that would otherwise have to be broken with a late import.

One dataclass, no dependencies, imported by:

    llm_brain.py     builds the built-in tools and executes all of them
    plugin_loader.py wraps user-written plugins in the same shape
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass(frozen=True)
class ToolSpec:
    """One capability the supervisor may invoke."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., str]

    # Outward-facing side effects need a human in the loop. A webhook may
    # email a customer or move data, and the argument reaching it came out
    # of a speech recogniser by way of a language model - two lossy steps.
    requires_confirmation: bool = False

    # Rendered with the tool's arguments to build the spoken question.
    confirmation_template: str = "Do you authorize running {name}?"

    # Run this off the listening thread (task_dispatcher) and report the
    # result later, instead of making the user wait through it. Set on
    # tools that are slow AND whose answer is not part of the sentence
    # Jarvis is about to speak - a webhook, a long query. A tool whose
    # result IS the answer ("what is on my screen") must stay in the
    # foreground, or the reply would be "I'm on it" and nothing else.
    background: bool = False

    # Human phrase used when a backgrounded result is announced:
    # "About the subtrack log workflow: ...". Falls back to the name.
    label: Optional[str] = None

    # Set by the plugin loader for tools that came from plugins/, so the
    # console and the status tool can tell them apart from built-ins.
    source: str = "builtin"

    def confirmation_question(self, args: dict[str, Any]) -> str:
        # The tool's own arguments win over the defaults: create_new_tool
        # takes an argument called "name", and passing both as keywords
        # is a TypeError, not a fallback. Build one mapping instead.
        fields: dict[str, Any] = {"name": self.name}
        fields.update(args or {})
        try:
            return self.confirmation_template.format(**fields)
        except (KeyError, IndexError, TypeError):
            return f"Do you authorize running {self.name}?"

    def spoken_label(self, args: Optional[dict[str, Any]] = None) -> str:
        """The phrase used to refer to this tool out loud."""
        if self.label:
            try:
                return self.label.format(**(args or {}))
            except (KeyError, IndexError, TypeError):
                return self.label
        return self.name.replace("_", " ")
