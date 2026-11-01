"""
Small integrations with outside services, shared by the brain, the plugins and the agents.

    line_notifier    push a text message to the owner's LINE (Messaging API)
    calendar_reader  today's events from Google Calendar (read-only)
    canva_auth       sign in to Canva once, by hand, for the Publishing Agent's browser
                     (python -m utils.canva_auth; agents/canva_browser.py)

Nothing here registers a tool: each caller wraps what it needs in its own ToolSpec
(agents/productivity_agent.py, llm_brain.py), so a tool exists exactly once.
"""
