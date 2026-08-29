"""Jarvis plugin: example_word_count

A worked example of the plugin contract, shipped so the folder is never
empty and so `create_new_tool` has a pattern to imitate. Safe to delete.
"""

TOOL_SPEC = {
    "name": "example_word_count",
    "description": (
        "Count the words and characters in a piece of text. A worked "
        "example of the plugin format; use it only if the user actually "
        "asks for a word count."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The text to measure."},
        },
        "required": ["text"],
    },
    # This one only reads its own argument, so it does not need the gate.
    "requires_confirmation": False,
    "label": "word count",
}


def run(text: str = "") -> str:
    """Every plugin's entry point: keyword arguments in, one sentence out."""
    words = len(text.split())
    return f"That is {words} words and {len(text)} characters."
