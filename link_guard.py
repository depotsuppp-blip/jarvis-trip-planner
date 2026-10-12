"""
Trip links are evidence, not memory.

A language model that has seen a trip link earlier in a conversation will, now
and then, write that link again when asked for a NEW trip: it is the right
shape, it is in the context, and it makes the reply feel finished. The user
opens it and lands on last week's trip. Telling the model not to is necessary
(llm_brain.SYSTEM_PROMPT does) but a prompt is a request, not a guarantee, so
this module is the guarantee: it finds every trip link and trip id in a reply
so the brain can check them against what a tool actually returned in THIS
request, before the reply goes anywhere.

What counts as a trip reference:

    * a URL whose path is one of the Trip Planner app's trip pages
      (/trip/poll/<id>, /trip/draft/<id>, /trip/dashboard/<id>,
      /trip/swipe/<id>) on any host - the LIFF links Jarvis sends over LINE
      have the same path; and
    * a bare UUID, which is what a trip id is.

A URL must appear in the evidence VERBATIM, so a real id behind the wrong
host or an invented ?admin= token is caught as well as an invented id. A bare
UUID only has to appear somewhere in it. Other links (a restaurant's website,
documentation) are none of this module's business.

Dependency-free on purpose, like tool_spec.py and request_context.py, so the
brain can import it without a cycle and a test can load it on its own.
"""

from __future__ import annotations

import re

# ASCII URL characters only: a reply written in Thai can run a word straight onto
# the end of an address, and that word is not part of the link.
_URL = re.compile(r"https?://[A-Za-z0-9\-._~:/?#@!$&*+,;=%()\[\]]+", re.IGNORECASE)
_TRIP_PAGE = re.compile(r"/trip/(?:poll|draft|dashboard|swipe)/", re.IGNORECASE)
_UUID = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE
)

# Sentence punctuation (or a closing bracket of a markdown link) that ends up
# stuck to the end of an address when it is written inside a sentence.
_TRAILING = ".,;:!?)]}>\"'"


def unverified_trip_references(reply: str, evidence: str) -> list[str]:
    """
    The trip links and trip ids in `reply` that `evidence` does not contain,
    in the order they appear, without repeats. Empty means every one of them
    is backed up (or there are none).

    `evidence` is everything the reply may legitimately quote: the results of
    the tools called during this request and what the user just typed - NOT
    the earlier conversation, which is exactly where a stale link comes from.
    Comparison ignores case.
    """
    haystack = (evidence or "").lower()
    found: list[str] = []

    def add(reference: str) -> None:
        if reference not in found:
            found.append(reference)

    for match in _URL.finditer(reply or ""):
        url = match.group(0).rstrip(_TRAILING)
        if not _TRIP_PAGE.search(url):
            continue
        lowered = url.lower()
        if lowered not in haystack and lowered.rstrip("/") not in haystack:
            add(url)

    # Trip URLs were judged whole above; whatever UUIDs remain are written bare.
    # UUIDs inside any URL are left to that URL's own check (or ignored when it
    # is not a trip page).
    rest = _URL.sub(" ", reply or "")
    for uuid in _UUID.findall(rest):
        if uuid.lower() not in haystack:
            add(uuid)

    return found
