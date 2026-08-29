# Jarvis plugins

Drop a `.py` file here and it becomes a tool Jarvis can call — on the
next command, without restarting anything. Jarvis writes files here
itself when you ask it for a capability it does not have
(`create_new_tool`).

## The contract

A plugin file defines exactly two things at the top level:

```python
TOOL_SPEC = {
    "name": "roll_dice",                  # must match the filename
    "description": "Roll an N-sided die.",# the model reads this to decide when to use it
    "input_schema": {                     # JSON Schema for the arguments
        "type": "object",
        "properties": {"sides": {"type": "integer"}},
        "required": ["sides"],
    },
    "requires_confirmation": True,        # optional, defaults to True
    "background": False,                  # optional; True = run off the mic thread
    "label": "dice roll",                 # optional; used when announcing results
}

def run(sides: int) -> str:               # arguments arrive as keywords
    import random
    return f"You rolled a {random.randint(1, sides)}."
```

Rules the loader enforces:

- filename and `name` must be lowercase letters/digits/underscores
- the file must define both `TOOL_SPEC` and a top-level `run` function
- the name may not collide with a built-in tool
- `run` returns a **string** — it is read aloud, so write a sentence

Anything that fails these checks is skipped with a printed reason; one
broken plugin never breaks the others or the assistant.

## Read this before trusting a plugin

**Plugins are not sandboxed.** A plugin is ordinary Python running
inside Jarvis with your user account's privileges — it can read your
files and reach the network. The loader's static checks catch mistakes,
not malice.

What actually protects you:

- `create_new_tool` prints the code and asks out loud before writing it
- risky patterns (`subprocess`, `eval`, file writes, sockets) are named
  in that spoken prompt
- every plugin is confirmation-gated when called, unless its own
  `TOOL_SPEC` opts out

Treat this folder as code you agreed to run. Read a generated plugin
before you say yes, and delete anything you did not want — deleting the
file removes the tool on the next command.
