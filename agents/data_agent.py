"""
The Head of Data & Documents: reads files and analyses data for the manager.

    primary      Gemini Pro            JARVIS_DATA_MODEL           (default gemini-pro-latest)
    fallback     Groq                  JARVIS_DATA_FALLBACK_MODEL  (default openai/gpt-oss-120b)
    last resort  Jarvis's Gemini model (agents/base.py)

Tools, taken off the manager: the filesystem server (read_file, list_directory -
text, PDF and Word files in the project folder, uploads/ included) and the data
server (load_file, list_tables, describe_table, query, run_sql). The spec asked for
read_file and load_file; the rest of the two servers come with them because a table
that is loaded but cannot be queried answers nothing.

The fallback was asked for as Llama 3 on Groq. Groq retired its Llama 3.x models for
free and developer accounts on 16 August 2026 and points to openai/gpt-oss-120b
(an open-weight model) instead; set JARVIS_DATA_FALLBACK_MODEL to use another.
Gemini Pro needs a paid Gemini key: on a free key it answers 429 "quota exceeded"
(measured 2026-10-26), and then every task goes to the fallback.
"""

from __future__ import annotations

from llm_brain import _env

from .base import SpecialistAgent, gemini_route, last_resort_route, Route
from .openai_compat import GroqProvider

PRIMARY_MODEL = "gemini-pro-latest"
FALLBACK_MODEL = "openai/gpt-oss-120b"
SERVERS = ("local_fs", "data")

SYSTEM_PROMPT = """\
You are the Head of Data & Documents on Jarvis's team. Jarvis (the manager) gives you one task about files or data; you do it with your tools and report back to Jarvis - not to the user, who never sees your words.

Your tools:
- read_file and list_directory read text, PDF and Word files inside the project folder, a page at a time (follow the offset it gives you to read on). Paths are relative to the project folder; files the user attached are under uploads/.
- load_file loads a CSV, TSV or JSON file into a table; list_tables, describe_table and query let you see and ask it with SQL. A file may be loaded already from an earlier task: check list_tables first.

How to work:
- Numbers (totals, averages, counts, top-N) come from SQL on the loaded table, never from adding rows up in your head.
- Read only as much as the task needs; for a long document, the parts that answer it.
- Never invent content you have not read. If a file cannot be opened, say exactly why.

Your report: the answer first, then the facts and figures it rests on, naming the file (and the page or table). Plain text, short, no greeting."""

DELEGATE_DESCRIPTION = (
    "Hands a task about files or data to your Head of Data & Documents, who has the only tools "
    "that read files - text, PDF and Word documents in the project folder, including files the "
    "user attached under uploads/ - and that load data files (CSV, TSV, JSON) into tables to "
    "total, average or count with SQL. It does not see this conversation: put the whole task in "
    "`task`, with every file path (copy it exactly from the [System Context] line) and what you "
    "need back. It reports to you; answer the user yourself from its report."
)


def build(last_resort: Route | None = None) -> SpecialistAgent:
    primary = _env("JARVIS_DATA_MODEL", PRIMARY_MODEL)
    fallback = _env("JARVIS_DATA_FALLBACK_MODEL", FALLBACK_MODEL)
    return SpecialistAgent(
        name="data",
        title="Head of Data & Documents",
        system_prompt=SYSTEM_PROMPT,
        routes=[
            gemini_route(primary, "primary"),
            Route(GroqProvider(model=fallback), fallback, "fallback"),
            last_resort or last_resort_route(),
        ],
        servers=SERVERS,
        max_steps=6,
        delegate_description=DELEGATE_DESCRIPTION,
        task_description="The whole task, self-contained: what to find or compute, every file path, and what to report back.",
        skills="reads files, PDFs and Word documents, and works on data files with SQL",
    )
