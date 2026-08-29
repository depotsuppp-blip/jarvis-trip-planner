"""
Module 2: Supervised OS Execution (VS Code Opener) + Intent Router

Maps a natural-language intent to a local project path, then requires
explicit user confirmation before running any OS-level command. The
command is never executed silently.

This module also owns the *routing* half of intent parsing - deciding
whether an utterance is an OS command, an n8n workflow trigger, or a
database question (see resolve_automation). Routing lives here, and only
routing: the automation modules resolve their own names and do their own
work, so nothing in this file imports them and there is no import cycle.

The intent parser is deliberately forgiving, because its input is now
speech-to-text rather than typed text. All of these resolve to the same
project:

    "open jarvis"
    "launch jarvis"
    "start up the jarvis project"
    "open jarvis in vs code"
    "could you open jervis in vscode please"

Matching runs in four passes, cheapest first: exact name, alias,
whitespace-insensitive ("jarvis project" -> "jarvis"), then a fuzzy
close-match pass to absorb STT mishearings.

One vocabulary is deliberately excluded: see SUPERVISOR_ONLY_TERMS.
"""

from __future__ import annotations

import difflib
import os
import re
import shutil
import subprocess
from typing import Callable, Optional

# Hardcoded project-name -> local path registry.
# Extend this dictionary (or later load it from a config file) as new
# projects are added.
#
# NOTE: "subtrack" is deliberately absent - see SUPERVISOR_ONLY_TERMS.
PROJECT_PATHS = {
    "jarvis": "C:/Jarvis_Project"
}

# Words that must never be claimed by any local parser, no matter how
# they are phrased. Anything containing one of these falls straight
# through main.py's routing to the llm_brain supervisor, which decides
# what the user actually wanted and calls trigger_n8n_webhook with a
# payload it extracted from the sentence.
#
# WHY: Subtrack is a data destination now, not a folder to open. Real
# commands are "log my Netflix subscription, fifteen dollars,
# entertainment" - the useful part is the fields buried in that
# sentence, and no local table can extract them. A local "open project"
# intent sitting on the word "subtrack" swallowed those commands and
# answered "Loading context for Subtrack" instead of logging anything.
#
# Listed as spoken variants, including the mishearings Vosk produces
# ("subtract", "sub track"), because the guard runs on the raw
# transcript.
SUPERVISOR_ONLY_TERMS: tuple[str, ...] = (
    "subtrack",
    "sub track",
    "subtract",
    "sub tract",
    "sub tracker",
    "subtracker",
)

# Extra spoken forms that should map onto a registry key. STT often
# splits or mangles compound project names, so give it help here.
PROJECT_ALIASES: dict[str, str] = {
    "jarvis project": "jarvis",
    "jervis": "jarvis",
}

# Verbs that mean "open this thing". Longest forms first so that
# "fire up" is stripped before a bare "up" could ever be considered.
#
# Tense variants matter: offline STT frequently returns "launched" for
# "launch" and "opens" for "open", because it is picking the likeliest
# word sequence rather than obeying grammar.
_OPEN_VERBS = (
    "fire up",
    "pull up",
    "bring up",
    "boot up",
    "start up",
    "spin up",
    "open up",
    "opens",
    "opened",
    "open",
    "launches",
    "launched",
    "launch",
    "started",
    "starts",
    "start",
    "boots",
    "boot",
    "loads",
    "load",
    "runs",
    "run",
)

# Trailing editor phrases that carry no project information.
_EDITOR_SUFFIXES = (
    "in visual studio code",
    "with visual studio code",
    "in vs code",
    "with vs code",
    "in vscode",
    "with vscode",
    "in code",
    "using vs code",
    "using vscode",
)

# Polite filler that STT will happily transcribe.
_FILLER_WORDS = (
    "please",
    "for me",
    "could you",
    "can you",
    "would you",
    "hey",
    "jarvis",
    "the",
    "my",
    "project",
)

# Minimum similarity for the fuzzy fallback. High enough that random
# words don't match, low enough to survive a mangled syllable.
_FUZZY_CUTOFF = 0.72

# Minimum similarity for the sliding-window pass (see _window_match).
# Deliberately stricter than _FUZZY_CUTOFF, because that pass tries far
# more candidates and so has more chances to match something by accident.
_WINDOW_CUTOFF = 0.75

# Longest span of words considered as a single project name.
_MAX_WINDOW = 3


def _normalise(text: str) -> str:
    """Lowercase, strip punctuation, and collapse runs of whitespace."""
    lowered = text.lower().strip()
    lowered = re.sub(r"[^\w\s]", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def is_supervisor_only(intent: str) -> bool:
    """
    True if the transcript mentions something reserved for the language
    model supervisor (SUPERVISOR_ONLY_TERMS).

    Callers use this to bail out of local routing entirely. The check is
    a plain substring test on the normalised text rather than a word
    match, so "subtracked", "sub-track", and "SubTrack," all trip it -
    a false positive here costs one API call, while a false negative
    silently swallows the command.
    """
    if not intent:
        return False
    text = _normalise(intent)
    # Also test with whitespace removed, so a transcript that split the
    # name three ways ("sub t rack") still trips the "subtrack" entry.
    squashed = text.replace(" ", "")
    return any(
        term in text or term.replace(" ", "") in squashed
        for term in SUPERVISOR_ONLY_TERMS
    )


def _strip_editor_suffix(text: str) -> str:
    """Removes a trailing 'in vs code'-style phrase, wherever it appears."""
    for suffix in _EDITOR_SUFFIXES:
        if suffix in text:
            text = text.replace(suffix, " ")
    return re.sub(r"\s+", " ", text).strip()


def _strip_leading_verb(text: str) -> str:
    """Removes a leading open/launch/start-style verb, if present."""
    for verb in _OPEN_VERBS:
        if text == verb:
            return ""
        if text.startswith(verb + " "):
            return text[len(verb) + 1 :].strip()
    return text


def _strip_filler(text: str) -> str:
    """Drops polite filler tokens that carry no project information."""
    for filler in _FILLER_WORDS:
        text = re.sub(rf"\b{re.escape(filler)}\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _match_candidate(candidate: str) -> Optional[str]:
    """
    Resolves a cleaned candidate string to a registry key, trying
    exact -> alias -> whitespace-insensitive -> fuzzy.
    """
    if not candidate:
        return None

    if candidate in PROJECT_PATHS:
        return candidate

    if candidate in PROJECT_ALIASES:
        return PROJECT_ALIASES[candidate]

    # "sub track" -> "subtrack"
    squashed = candidate.replace(" ", "")
    for name in PROJECT_PATHS:
        if squashed == name.replace(" ", ""):
            return name
    for alias, target in PROJECT_ALIASES.items():
        if squashed == alias.replace(" ", ""):
            return target

    # Fuzzy pass, for STT mishearings ("subtrak", "jarvus").
    pool = list(PROJECT_PATHS) + list(PROJECT_ALIASES)
    close = difflib.get_close_matches(candidate, pool, n=1, cutoff=_FUZZY_CUTOFF)
    if not close:
        close = difflib.get_close_matches(squashed, pool, n=1, cutoff=_FUZZY_CUTOFF)
    if close:
        hit = close[0]
        return PROJECT_ALIASES.get(hit, hit)

    return None


def _window_match(text: str) -> Optional[str]:
    """
    Last-resort pass for project names the speech model has never heard.

    Offline STT has a fixed vocabulary, so an invented product name is
    not merely misspelled - it is decomposed into real words. Vosk turns
    "open subtrack" into "opens of track", because "subtrack" is simply
    not in its lexicon.

    Whole-string matching can't recover that, so this slides a window of
    1..3 words across the phrase and fuzzy-matches each span (spaced and
    squashed) against the registry, keeping the single best score. That
    finds "track" -> "subtrack" inside "opens of track".

    Longer spans are preferred at equal confidence, since matching more
    of the utterance is stronger evidence than matching one short word.
    """
    words = text.split()
    if not words:
        return None

    pool: dict[str, str] = {name: name for name in PROJECT_PATHS}
    pool.update(PROJECT_ALIASES)

    best_score = 0.0
    best_target: Optional[str] = None
    best_size = 0

    for size in range(1, min(_MAX_WINDOW, len(words)) + 1):
        for start in range(len(words) - size + 1):
            window = " ".join(words[start : start + size])
            squashed = window.replace(" ", "")
            for key, target in pool.items():
                key_squashed = key.replace(" ", "")
                for candidate in (window, squashed):
                    score = difflib.SequenceMatcher(None, candidate, key_squashed).ratio()
                    if score > best_score or (score == best_score and size > best_size):
                        best_score, best_target, best_size = score, target, size

    return best_target if best_score >= _WINDOW_CUTOFF else None


def resolve_project(intent: str) -> Optional[str]:
    """
    Pulls a known project name out of a free-form (often spoken) intent.
    Returns the registry key, or None if nothing matched.
    """
    if not intent:
        return None

    # Reserved vocabulary is rejected before anything else: a phrase
    # naming Subtrack belongs to the supervisor, which can extract a
    # payload out of it, not to a parser that can only open a folder.
    if is_supervisor_only(intent):
        return None

    text = _normalise(intent)

    # Automation commands are checked first and rejected outright.
    # "run workflow data sync" opens with a verb this parser recognises
    # ("run"), and the sliding-window pass in pass 5 is greedy enough to
    # find a project name inside almost anything - so without this guard
    # a workflow request could open VS Code instead.
    if _automation_kind(text) is not None:
        return None

    text = _strip_editor_suffix(text)

    # Pass 1: treat the whole phrase, minus verb and filler, as the name.
    stripped = _strip_filler(_strip_leading_verb(text))
    match = _match_candidate(stripped)
    if match:
        return match

    # Pass 2: same, but strip the verb after the filler (handles
    # "could you open subtrack" where filler precedes the verb).
    stripped2 = _strip_leading_verb(_strip_filler(text))
    match = _match_candidate(stripped2)
    if match:
        return match

    # Pass 3: explicit "<verb> <name> [in vs code]" regex.
    verb_pattern = "|".join(re.escape(v) for v in _OPEN_VERBS)
    regex = re.search(rf"\b(?:{verb_pattern})\s+(.+)$", text)
    if regex:
        match = _match_candidate(_strip_filler(regex.group(1).strip()))
        if match:
            return match

    # Pass 4: any known name appearing anywhere in the phrase.
    for name in PROJECT_PATHS:
        if re.search(rf"\b{re.escape(name)}\b", text):
            return name
    for alias, target in PROJECT_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", text):
            return target

    # Pass 5: sliding-window fuzzy match, for names the speech model
    # broke into unrelated real words ("subtrack" -> "opens of track").
    return _window_match(text)


# Backwards-compatible alias for the previous private helper.
_extract_project_name = resolve_project


def is_open_intent(intent: str) -> bool:
    """
    True if the phrase looks like a request to open a project - i.e. it
    names a known project, with or without an explicit verb.
    """
    return resolve_project(intent) is not None


# ---------------------------------------------------------------------
# Intent routing: automation commands
# ---------------------------------------------------------------------
#
# Two families of command are recognised here, and only recognised - the
# actual work belongs to n8n_trigger.py and db_connector.py:
#
#     "run workflow data sync"      -> ("workflow", "data sync")
#     "trigger the backup workflow" -> ("workflow", "backup")
#     "check database"              -> ("database", "")
#     "check the database user count" -> ("database", "user count")
#
# Only the *shape* of the command is decided here; the extracted name is
# passed on raw, because each module owns its own registry and its own
# fuzzy matching. That keeps this file free of automation imports.

# THE COMMAND TABLE: exact spoken phrases, mapped to (destination, name).
#
# This pass exists because real commands don't announce themselves.
# "check demp database status" contains no marker noun the structural
# parser below can key on - and "check demp database status" would be shredded into the
# wrong query name by it. Naming the phrase outright is both more
# accurate and cheaper than teaching the parser about every domain noun.
#
# The right-hand name is passed to the owning module as a plain string;
# this table deliberately imports nothing, so os_executor stays free of
# any dependency on n8n_trigger or db_connector.
#
# Matching is fuzzy (see _match_phrase), so near-misses from offline STT
# still land - but list the mishearings you actually hear here anyway,
# because an exact entry always beats a fuzzy score.
AUTOMATION_PHRASES: dict[str, tuple[str, str]] = {
    # -- n8n: subtrack log --------------------------------------------
    # Deliberately NOT listed here. "Log my Netflix subscription, fifteen
    # dollars, entertainment" has to reach the supervisor in llm_brain.py,
    # because the value of that command is the payload it carries -
    # item_name, amount, category - and this table can only pass a bare
    # workflow name. Claiming the phrase locally would fire the webhook
    # with an empty body and write a blank row into Subtrack.
    # -- Postgres: DEMP status ----------------------------------------
    "check demp database status": ("database", "demp status"),
    "check the demp database status": ("database", "demp status"),
    "demp database status": ("database", "demp status"),
    "check demp status": ("database", "demp status"),
    "demp status": ("database", "demp status"),
    "check demp database": ("database", "demp status"),
    # Vosk mishearings of the coined name "DEMP".
    "check damp database status": ("database", "demp status"),
    "check temp database status": ("database", "demp status"),
    "check dem database status": ("database", "demp status"),
    # Vosk hears the spelled-out "D E M P" as a name it does know, and
    # keeps the letters apart. Listed as exact entries because the
    # letter-spaced forms are far enough from "check demp database
    # status" that the fuzzy pass cannot be relied on to catch them.
    "shake d e m p database status": ("database", "demp status"),
    "shaq d e m p database status": ("database", "demp status"),
    "check d e m p database status": ("database", "demp status"),
    "shake demp database status": ("database", "demp status"),
    # Listed so they resolve by exact match rather than fuzzily landing
    # on "check demp database" (they score ~0.85 against it). The empty
    # name means the connectivity check: asking about "the database"
    # is a question about Postgres, not about the energy log table -
    # and SELECT 1 answers it whether or not the schema exists yet.
    "check database": ("database", ""),
    "check the database": ("database", ""),
    "database status": ("database", ""),
    "check database status": ("database", ""),
}

# Minimum similarity for the phrase table. Stricter than the name
# matchers: a phrase match routes the whole utterance somewhere on its
# own authority, so it should need real confidence, not a lucky vowel.
_PHRASE_CUTOFF = 0.78


# Verbs that mean "fire this workflow". Longest forms first so "kick
# off" is consumed before a bare "off" could be.
_WORKFLOW_VERBS = (
    "kick off",
    "kickoff",
    "trigger",
    "execute",
    "launch",
    "start",
    "run",
    "fire",
)

# Nouns that mark an utterance as being about a workflow. "n8n" is
# included in the spellings offline STT actually produces for it.
_WORKFLOW_NOUNS = (
    "workflows",
    "workflow",
    "work flow",
    "automations",
    "automation",
    "n8n",
    "n 8 n",
    "flow",
)

# Verbs that mean "read something out of the database".
_DATABASE_VERBS = (
    "how many",
    "look up",
    "lookup",
    "check on",
    "check",
    "query",
    "search",
    "read",
    "ask",
    "get",
    "show",
    "tell me",
)

# Nouns that mark an utterance as being about the database.
_DATABASE_NOUNS = (
    "databases",
    "database",
    "data base",
    "postgresql",
    "postgres",
    "sql server",
    "db",
)

# Dangling prepositions left behind once the marker noun is removed:
# "how many users in the database" -> "users in"    -> "users"
# "query the database for active users" -> "for active users" -> "active users"
_PREPOSITIONS = ("about", "into", "from", "for", "in", "on", "of", "at", "to")

# Linking words that survive filler-stripping and would otherwise be
# fuzzy-matched as part of a name ("users are" -> "users").
_TRAILING_STOPWORDS = _PREPOSITIONS + ("are", "is", "there", "have", "has", "do", "does")


def normalise(text: str) -> str:
    """
    Public wrapper around the normaliser used throughout this module:
    lowercase, punctuation stripped, whitespace collapsed. Shared with
    n8n_trigger.py and db_connector.py so all three agree on what a
    spoken name looks like.
    """
    return _normalise(text)


def match_name(
    candidate: str,
    pool: dict[str, str],
    cutoff: float = _FUZZY_CUTOFF,
) -> Optional[str]:
    """
    Generic spoken-name resolver, shared by the automation modules.

    `pool` maps every accepted spoken form (canonical names and aliases
    alike) to the canonical key it stands for. Matching runs exact ->
    whitespace-insensitive -> substring -> fuzzy, mirroring the project
    matcher above, and returns None when nothing clears `cutoff`.
    """
    if not candidate:
        return None

    text = _normalise(candidate)
    if not text:
        return None

    if text in pool:
        return pool[text]

    squashed = text.replace(" ", "")
    for key, target in pool.items():
        if squashed == _normalise(key).replace(" ", ""):
            return target

    # A named form appearing inside a longer phrase ("the backup one").
    # Longest key first, so "active users" beats "users".
    for key in sorted(pool, key=len, reverse=True):
        normalised_key = _normalise(key)
        if normalised_key and re.search(rf"\b{re.escape(normalised_key)}\b", text):
            return pool[key]

    close = difflib.get_close_matches(text, list(pool), n=1, cutoff=cutoff)
    if not close:
        close = difflib.get_close_matches(squashed, list(pool), n=1, cutoff=cutoff)
    return pool[close[0]] if close else None


def _strip_leading_words(text: str, words: tuple[str, ...]) -> str:
    """Repeatedly removes any of `words` from the front of `text`."""
    changed = True
    while changed:
        changed = False
        for word in words:
            if text == word:
                return ""
            if text.startswith(word + " "):
                text = text[len(word) + 1 :].strip()
                changed = True
    return text


def _split_on_noun(text: str, nouns: tuple[str, ...]) -> Optional[tuple[str, str]]:
    """
    Finds the first marker noun and returns (before, after). None if the
    phrase contains no marker noun at all.
    """
    for noun in nouns:
        match = re.search(rf"\b{re.escape(noun)}\b", text)
        if match:
            return text[: match.start()].strip(), text[match.end() :].strip()
    return None


def _clean_target(text: str, verbs: tuple[str, ...]) -> str:
    """Reduces a fragment to the bare name the caller should resolve."""
    cleaned = _strip_filler(_strip_leading_words(text, verbs))
    cleaned = _strip_leading_words(cleaned, verbs)  # verb may follow filler
    cleaned = _strip_leading_words(cleaned, _PREPOSITIONS)

    # Repeated because one pass can expose another: "users are in the"
    # loses "the" to the filler pass, then "in", then "are".
    previous = None
    while cleaned != previous:
        previous = cleaned
        for stopword in _TRAILING_STOPWORDS:
            cleaned = re.sub(rf"\s*\b{re.escape(stopword)}$", "", cleaned).strip()

    return re.sub(r"\s+", " ", cleaned).strip()


def _match_phrase(text: str) -> Optional[tuple[str, str]]:
    """
    Matches already-normalised text against AUTOMATION_PHRASES, exactly
    first and then fuzzily. Returns the (destination, name) pair, or
    None if nothing was close enough.
    """
    if not text:
        return None

    if text in AUTOMATION_PHRASES:
        return AUTOMATION_PHRASES[text]

    pool = {phrase: phrase for phrase in AUTOMATION_PHRASES}
    hit = match_name(text, pool, cutoff=_PHRASE_CUTOFF)
    return AUTOMATION_PHRASES[hit] if hit else None


def _automation_kind(text: str) -> Optional[str]:
    """
    'workflow', 'database', or None, for already-normalised text.

    Workflow wins when both marker nouns are present, so a workflow
    named "database sync" still routes to n8n.
    """
    phrase = _match_phrase(text)
    if phrase is not None:
        return phrase[0]

    if _split_on_noun(text, _WORKFLOW_NOUNS) is not None:
        return "workflow"
    if _split_on_noun(text, _DATABASE_NOUNS) is not None:
        return "database"
    return None


def resolve_automation(intent: str) -> Optional[tuple[str, str]]:
    """
    Classifies an automation command.

    Returns ("workflow", name) or ("database", query_name), where the
    name is a raw spoken fragment for the owning module to resolve, and
    may be empty ("check database" names no query). Returns None when
    the utterance isn't an automation command at all, leaving it to the
    OS parser and then the language model.

    Two passes: the exact command table first, then the structural
    "<verb> <marker noun> <name>" parse for phrasings nobody listed.
    """
    if not intent:
        return None

    text = _normalise(intent)

    # Pass 1: a named command, matched whole. Both of the commands this
    # system is actually built around land here.
    phrase = _match_phrase(text)
    if phrase is not None:
        return phrase

    # Pass 2: open-ended phrasing - "run workflow X", "check database Y".
    kind = _automation_kind(text)
    if kind is None:
        return None

    if kind == "workflow":
        nouns, verbs = _WORKFLOW_NOUNS, _WORKFLOW_VERBS
    else:
        nouns, verbs = _DATABASE_NOUNS, _DATABASE_VERBS

    split = _split_on_noun(text, nouns)
    assert split is not None  # guaranteed by _automation_kind
    before, after = split

    # "run workflow data sync" - the name follows the marker noun.
    target = _clean_target(after, verbs)

    # "run the data sync workflow" - the name precedes it.
    if not target:
        target = _clean_target(before, verbs)

    return kind, target


def is_automation_intent(intent: str) -> bool:
    """True if the phrase is a workflow trigger or a database question."""
    return resolve_automation(intent) is not None


# ---------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------


def _default_confirmation(project_name: str) -> bool:
    """
    Typed confirmation gate. Only an explicit 'yes' proceeds; empty or
    ambiguous input is treated as 'no'.
    """
    prompt = f"Do you authorize opening {project_name.title()}? [y/N]: "
    try:
        response = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return response in ("y", "yes")


def _find_vscode() -> Optional[str]:
    """
    Locates the VS Code launcher. On Windows `code` is a .cmd shim, which
    bare subprocess calls miss, so search the PATHEXT variants too.
    """
    for candidate in ("code", "code.cmd", "code.exe"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


def open_project(project_name: str) -> bool:
    """
    Launches VS Code for an already-resolved project key. Assumes the
    confirmation gate has already been cleared by the caller.
    """
    path = PROJECT_PATHS.get(project_name)
    if path is None:
        print(f"[Jarvis] '{project_name}' is not in the project registry.")
        return False

    if not os.path.isdir(path):
        print(f"[Jarvis] The path for '{project_name}' does not exist: {path}")
        print("[Jarvis] Update PROJECT_PATHS in os_executor.py to point at the real folder.")
        return False

    executable = _find_vscode()
    if executable is None:
        print("[Jarvis] Could not find the 'code' command on your PATH.")
        print("[Jarvis] In VS Code: Ctrl+Shift+P -> 'Shell Command: Install code command in PATH'.")
        return False

    try:
        subprocess.run([executable, path], check=True)
        print(f"[Jarvis] Opened '{project_name}' in VS Code.")
        return True
    except (subprocess.CalledProcessError, OSError) as exc:
        print(f"[Jarvis] Failed to launch VS Code for '{project_name}': {exc}")
        return False


def open_in_vscode(
    intent: str,
    confirm_fn: Optional[Callable[[str], bool]] = None,
) -> bool:
    """
    Resolves an intent to a local path, asks the user to confirm, and
    only then runs `code <path>`.

    `confirm_fn` lets the caller swap in a spoken confirmation gate;
    it defaults to the typed [y/N] prompt.

    Returns True if the command was executed, False otherwise (unknown
    project, user declined, or launch failure).
    """
    project_name = resolve_project(intent)
    if project_name is None:
        print(f"[Jarvis] Could not resolve a known project from intent: '{intent}'")
        known = ", ".join(sorted(PROJECT_PATHS))
        print(f"[Jarvis] Known projects: {known}")
        return False

    gate = confirm_fn or _default_confirmation
    if not gate(project_name):
        print("[Jarvis] Action declined. Nothing was executed.")
        return False

    return open_project(project_name)


if __name__ == "__main__":
    samples = [
        # Reserved for the supervisor - all of these must be None.
        "open subtrack",
        "launch subtrack",
        "open Subtrack in VS Code",
        "could you open sub track please",
        "log my Netflix subscription in subtrack, fifteen dollars",
        # Genuine project commands.
        "fire up jarvis",
        "start the jarvis project",
        "what's the weather",
    ]
    print("Project resolution check:")
    for phrase in samples:
        print(f"  {phrase!r:40} -> {resolve_project(phrase)!r}")

    automation_samples = [
        # The production command, plus the mishearings of it.
        "check demp database status",
        "check the demp database status",
        "check damp database status",
        "check dem database status",
        "shake d e m p database status",
        "shaq d e m p database status",
        "check d e m p database status",
        "shake demp database status",
        # Open-ended phrasings handled by the structural parser.
        "run automation",
        "trigger workflow",
        "run workflow subtrack log",
        "check database",
        "how many records are in the database",
        # Must NOT be claimed as automation.
        "open jarvis",
        "what's the weather",
    ]
    print("\nAutomation routing check:")
    for phrase in automation_samples:
        print(f"  {phrase!r:40} -> {resolve_automation(phrase)!r}")
