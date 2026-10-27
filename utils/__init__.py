"""
Small integrations with outside services, shared by the brain, the plugins and the agents.

    line_notifier    push a text message to the owner's LINE (Messaging API)
    calendar_reader  today's events from Google Calendar (read-only)

Nothing here registers a tool: each caller wraps what it needs in its own ToolSpec
(agents/productivity_agent.py, llm_brain.py), so a tool exists exactly once.
"""
