"""
main.py - Jarvis assistant entry point.

Wires every module into a single voice-driven loop. The loop itself is
a two-state machine owned by session_manager.py (Module 8):

    STANDBY  listening offline, discarding everything that is not a
             wake phrase ("hey jarvis", "wake up", ...)
        -> wake phrase detected, acknowledged out loud
    ACTIVE   every utterance routed straight through _dispatch below,
             no wake word needed between commands
        -> 15 s with no command
    STANDBY  after a spoken sign-off

This file owns the routing (_dispatch and the _handle_* functions);
session_manager.py owns when to listen and the inactivity timer that
ends a session.

Listening is hybrid and bilingual: Vosk (small, English, always-on)
spots the wake phrase in STANDBY, then faster-whisper (multilingual)
transcribes the commands in ACTIVE, so "log ค่ากาแฟ 120 บาท" works as
well as its English equivalent. See whisper_manager.py.

Three things run concurrently, and only the first one owns the mic:

    main thread        audio capture and routing (this file)
    jarvis-dispatcher  an asyncio loop running the slow tools
                       off the mic thread          (task_dispatcher)
    jarvis-whisper-*   the Whisper model load      (whisper_manager)

They share no locks with each other. Background results are never
spoken by the thread that produced them: they are queued and drained by
session_manager at a turn boundary, where the microphone is closed.
Everything below the main thread is a daemon, so a wedged worker can
never keep the process alive or take the listening loop down with it.

Routing is deliberately local-first. Every transcript is offered to the
local intent parsers (Module 2) before anything else:

    matches a built-in command   -> handled locally (memory, shutdown)
    names a workflow             -> confirmation gate -> n8n (Module 6)
    asks about the database      -> read-only query   -> db (Module 7)
    resolves to a known project  -> confirmation gate -> launch locally
    anything else                -> supervisor (Module 5) -> spoken aloud

So "open jarvis" never leaves the machine, and only genuine questions
are sent to the API.

The one deliberate exception is Subtrack: every transcript naming it
bypasses the local parsers entirely (see os_executor.SUPERVISOR_ONLY_TERMS)
and goes to the supervisor, because those commands carry data to log and
only the model can pull the fields out of them.

The last branch is no longer a plain chatbot. Module 5 is a supervisor:
it may decide an open-ended request needs n8n or the Subtrack database,
call that tool itself, and summarise the result. It runs *after* the
local parsers precisely so the cheap deterministic path always wins -
the model is for requests where choosing the system is the hard part.
Side-effecting tool calls reuse this file's spoken confirmation gate,
so there is one authorisation path regardless of who chose the action.

Automation is checked *before* the project parser, because the two
vocabularies overlap - "run workflow data sync" opens with a verb the
project parser also recognises, and it must not open VS Code.

Both agentic branches report through the same channel as everything
else: whatever n8n or Postgres comes back with is turned into one
sentence by its module and spoken aloud, so the user never has to look
at the console to learn whether something worked.

Every stage degrades gracefully. No microphone falls back to typed
input; no speech synthesiser falls back to printed output; a damaged
memory.json falls back to "no memory yet"; a missing API key disables
conversation while leaving OS commands working; an unreachable database
or n8n server is reported out loud rather than raised. The idle loop is
the last thing to ever go down.
"""

from __future__ import annotations

import os
import sys
from typing import NoReturn, Optional


def _dependency_error_and_exit(exc: ImportError) -> NoReturn:
    """
    Fires when a required third-party package (numpy, pyaudio,
    openwakeword, ...) isn't installed - typically because the user
    ran `python main.py` directly instead of setting up the project's
    virtual environment first. Prints a friendly, hard-to-miss message
    instead of letting a raw ModuleNotFoundError traceback surface.
    """
    missing = exc.name or str(exc)
    banner = "!" * 64
    print(f"\n{banner}")
    print("  JARVIS CANNOT START - MISSING DEPENDENCY")
    print(banner)
    print(f"\n  Missing package: {missing}")
    print("  Your Python environment isn't set up yet (or is incomplete).")
    print("\n  Fix: run setup.bat in the project folder, then start Jarvis with:")
    print("\n      .venv\\Scripts\\python main.py\n")
    print(banner + "\n")
    sys.exit(1)


try:
    import db_connector
    import memory_manager
    import n8n_trigger
    from llm_brain import LLMBrain
    from os_executor import (
        PROJECT_PATHS,
        is_supervisor_only,
        open_project,
        resolve_automation,
        resolve_project,
    )
    from session_manager import SessionManager
    from voice_engine import VoiceEngine
    from whisper_manager import WhisperTranscriber

    import task_dispatcher
except ImportError as exc:
    _dependency_error_and_exit(exc)

# openWakeWord is the optional STANDBY backend. The default backend is
# Vosk phrase-spotting, which needs nothing beyond the speech model
# already required for commands, so a missing openwakeword install must
# not stop Jarvis at the door - it just removes one wake-word option.
try:
    from wake_word_engine import WakeWordEngine

    _WAKEWORD_AVAILABLE = True
    _WAKEWORD_IMPORT_ERROR = ""
except Exception as exc:  # noqa: BLE001 - optional backend
    WakeWordEngine = None  # type: ignore[assignment]
    _WAKEWORD_AVAILABLE = False
    _WAKEWORD_IMPORT_ERROR = str(exc)

# Note: db_connector and n8n_trigger import psycopg2 and requests
# defensively, so neither can trigger the block above. A user with no
# database and no automation server should not be stopped at the door
# for missing packages they will never use - the modules say so out loud
# if those features are ever actually asked for.


# Phrases that end the session outright.
_QUIT_WORDS = ("stop", "exit", "quit", "shut down", "shutdown", "goodbye", "good bye")

# ...except that "stop" and "shut down" also open the phrase that takes
# down the DEVELOPMENT environment, and the two are one word apart.
# "stop" ends the session; "stop work" is a tool call. The quit check
# runs first in _dispatch, so without this exemption stop_subtrack_env
# is unreachable by every natural way of asking for it - Jarvis would
# simply say "Goodbye" and exit while the servers kept running.
#
# Only utterances that NAME something to stop are exempted. A bare
# "stop", or "stop listening", still quits: the failure in that
# direction (Jarvis stays awake) is recoverable, and quitting when the
# user meant their servers is not.
_ENVIRONMENT_TARGETS = (
    "work", "working",
    "environment", "servers", "server",
    "backend", "frontend", "dev",
    "docker", "container", "containers",
    "subtrack",
)

# Phrases that ask Jarvis what it remembers.
_MEMORY_QUERIES = (
    "what was i working on",
    "what was i doing",
    "last project",
    "previous project",
    "where did i leave off",
    "load context",
)

# Phrases that clear the conversation (not the project memory).
_RESET_PHRASES = (
    "new conversation",
    "start over",
    "forget that",
    "clear the conversation",
    "reset conversation",
)

# Populated in main(); shared by the handlers below.
_voice: Optional[VoiceEngine] = None
_session: Optional["SessionManager"] = None
_brain: Optional[LLMBrain] = None


def _confirm_open(project_name: str) -> bool:
    """
    Spoken confirmation gate. Anything other than a clear affirmative
    is treated as a refusal - an ambiguous answer must never authorise
    an OS-level command.
    """
    assert _voice is not None
    return _voice.confirm(f"Do you authorize opening {project_name.title()}?")


def _handle_open(project_name: str) -> None:
    """
    Confirmation gate -> memory load/save -> launch. The context
    announcement happens only after the user has authorised the action.
    """
    assert _voice is not None

    if not _confirm_open(project_name):
        _voice.speak("Understood. Nothing was executed.")
        return

    # Tier-1 memory: announce, then persist this as the active project.
    _voice.speak(f"Loading context for {project_name.title()}.")

    previous = memory_manager.save_context(project_name)
    open_count = int(previous.get("open_count", 0) or 0) if previous else 0
    if open_count:
        # Console only, not spoken - see "Silent Startup": a session-count
        # summary is not something the user asked to hear on every open.
        print(f"[Jarvis] Welcome back. This is session number {open_count + 1} for this project.")

    if open_project(project_name):
        _voice.speak(f"{project_name.title()} is open.")
    else:
        _voice.speak(f"I could not open {project_name.title()}. See the console for details.")


def _handle_workflow(workflow_name: str) -> None:
    """
    Triggers an n8n workflow, behind the same confirmation gate that
    guards OS commands.

    A webhook is an outward-facing, non-reversible side effect - it may
    email a customer or move data - and the name reaching this function
    came out of a speech recogniser. Both facts point the same way: ask
    first, and speak whatever n8n reports back.
    """
    assert _voice is not None

    # An empty name is normal - "run automation" names nothing - and
    # resolves to the N8N_WEBHOOK_URL workflow. Resolving before the
    # prompt means the user is asked about the real workflow ("daily
    # report"), not their mangled transcript of it.
    resolved = n8n_trigger.resolve_workflow(workflow_name)
    if resolved is None:
        # trigger_workflow phrases every "I can't run that" answer,
        # including the list of what is available. Nothing to duplicate
        # here - and it short-circuits before any HTTP call.
        _voice.speak(n8n_trigger.trigger_workflow(workflow_name))
        return

    if not _voice.confirm(f"Do you authorize running the {resolved} workflow?"):
        _voice.speak("Understood. Nothing was executed.")
        return

    _voice.speak(f"Running {resolved}.")
    _voice.speak(n8n_trigger.trigger_workflow(resolved))


def _handle_database(query_name: str) -> None:
    """
    Runs a pre-defined read-only query and speaks the answer.

    No confirmation gate here, and deliberately so: the connector cannot
    run anything but registered SELECTs against a read-only session, so
    the worst a misheard word can do is answer a different question.
    """
    assert _voice is not None

    if not db_connector.is_configured():
        _voice.speak(
            "My database isn't configured yet. "
            "Set the database credentials in the environment file."
        )
        return

    # "check database" / "database status" name no query and go to the
    # health check; anything else is looked up in the query registry.
    if not query_name:
        _voice.speak(db_connector.check_system_status())
        return

    _voice.speak(db_connector.run_query(query_name))


def _handle_conversation(intent: str) -> None:
    """
    Fallback branch: anything the local parser didn't claim is a
    question for Claude. The reply is spoken aloud.
    """
    assert _voice is not None

    # Guarded rather than asserted: main() always builds the brain, but a
    # bare assert here would surface as an empty error message and total
    # silence from the speakers - the one failure mode a voice assistant
    # must never have.
    if _brain is None:
        _voice.speak("My language model isn't loaded, so I can't answer that.")
        return

    if not _brain.available:
        known = ", ".join(sorted(PROJECT_PATHS))
        _voice.speak(
            "I can't answer questions until my language model is configured. "
            f"I can still open these projects: {known}."
        )
        return

    print("[Jarvis] Thinking...")
    _voice.speak(_brain.ask(intent))


def _dispatch(intent: str) -> bool:
    """
    Routes a captured intent. Returns False if the assistant should shut
    down, True to keep listening.

    Order matters: local handlers get first refusal, and only what none
    of them claim is sent to the API.
    """
    assert _voice is not None
    lowered = intent.lower().strip()

    # 1. Session control - always local, unless the user named something
    #    to stop, in which case they meant their servers and the
    #    supervisor's stop_subtrack_env tool handles it further down.
    if any(word == lowered or lowered.startswith(word) for word in _QUIT_WORDS):
        if not any(target in lowered for target in _ENVIRONMENT_TARGETS):
            _voice.speak("Shutting down. Goodbye.")
            return False

    # 2. Conversation reset - always local.
    if any(phrase in lowered for phrase in _RESET_PHRASES):
        if _brain is not None:
            _brain.reset()
        _voice.speak("Starting a fresh conversation.")
        return True

    # 3. Reserved vocabulary - straight to the supervisor.
    #
    #    Anything naming Subtrack skips every remaining local parser,
    #    including the memory queries below ("load context for
    #    subtrack"). Subtrack is a data destination now, not a folder to
    #    open, and the real commands ("log my Netflix subscription,
    #    fifteen dollars, entertainment") carry their value in fields no
    #    local table can extract. The old project intent claimed those on
    #    the word alone and answered "Loading context for Subtrack"
    #    instead of logging anything, so the word is now off-limits
    #    locally: the brain reads the sentence and calls
    #    trigger_n8n_webhook with the payload it pulled out of it.
    if is_supervisor_only(intent):
        _handle_conversation(intent)
        return True

    # 4. Stored project context - always local.
    if any(query in lowered for query in _MEMORY_QUERIES):
        _voice.speak(memory_manager.describe_context())
        return True

    # 5. Automation - n8n workflows and read-only database queries.
    #    Ahead of the project parser on purpose: "run workflow data sync"
    #    starts with a verb that parser also claims.
    automation = resolve_automation(intent)
    if automation is not None:
        kind, target = automation
        if kind == "workflow":
            _handle_workflow(target)
        else:
            _handle_database(target)
        return True

    # 6. OS command - resolved and executed entirely on this machine.
    project_name = resolve_project(intent)
    if project_name is not None:
        _handle_open(project_name)
        return True

    # 7. Everything else is a question for the language model.
    _handle_conversation(intent)
    return True


def _build_standby_engine():
    """
    Chooses the STANDBY wake-word backend.

    Vosk phrase-spotting is the default: it needs nothing beyond the
    speech model commands already use, and it recognises every phrase in
    session_manager.WAKE_PHRASES, including "wake up", which no
    pretrained openWakeWord model knows. Set JARVIS_WAKE_BACKEND to
    "openwakeword" for the dedicated always-on ONNX model instead -
    lower CPU and fewer false triggers, at the cost of one fixed phrase
    and a model download.

    Returns the engine instance, or None to use the Vosk backend.
    """
    backend = os.getenv("JARVIS_WAKE_BACKEND", "vosk").strip().lower()

    if backend not in ("vosk", "openwakeword"):
        print(f"[Jarvis] Unknown JARVIS_WAKE_BACKEND={backend!r}; using Vosk.")
        backend = "vosk"

    if backend == "vosk":
        print("[Jarvis] Standby backend: Vosk phrase-spotting.")
        return None

    if not _WAKEWORD_AVAILABLE:
        print(f"[Jarvis] openWakeWord unavailable ({_WAKEWORD_IMPORT_ERROR}); using Vosk.")
        return None

    try:
        engine = WakeWordEngine()
    except RuntimeError as exc:
        # Model files missing - the engine's message explains the fix.
        # Not fatal any more: Vosk can spot the wake phrase instead.
        print(f"\n[Jarvis] {exc}\n")
        print("[Jarvis] Falling back to Vosk phrase-spotting for standby.")
        return None

    print("[Jarvis] Standby backend: openWakeWord.")
    return engine


def main() -> None:
    global _voice, _session, _brain

    # Commands can now be Thai, and Windows consoles still default to a
    # legacy code page where printing Thai raises UnicodeEncodeError.
    # Switch stdout to UTF-8 before anything can transcribe a word.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:  # noqa: BLE001 - older console, not worth failing over
            pass

    _voice = VoiceEngine()

    # Synthesise the acknowledgement phrases now, on a background
    # thread, so the first command of the session can be acknowledged
    # off the disk in milliseconds instead of waiting on Edge-TTS. This
    # runs while the greeting below is playing and costs nothing here.
    _voice.prewarm_fillers()

    # Built before the greeting so any API key warning prints first.
    #
    # confirm=_voice.confirm is what keeps the supervisor honest. The
    # brain can now trigger n8n webhooks on its own initiative, and a
    # webhook is an outward-facing, non-reversible side effect. Handing
    # it the same spoken yes/no gate that guards _handle_workflow means
    # there is exactly one authorisation path in the app, whether the
    # workflow was named by the local parser or chosen by the model.
    _brain = LLMBrain(confirm=_voice.confirm)

    # Silent startup: none of this is spoken any more (see "Silent
    # Startup") - it is status a person reads on the console if they
    # want it, not something Jarvis should announce out loud on every
    # boot before anyone has said a word to it.
    context = memory_manager.load_context()
    if context is not None:
        print(f"[Jarvis] Online. {memory_manager.describe_context()}")
    else:
        print("[Jarvis] Online. No previous project context stored.")

    if not _brain.available:
        print("[Jarvis] Conversation is offline, but I can still open your projects.")

    # Report the agentic capabilities once, at startup, so a missing
    # webhook URL or database password is discovered now rather than in
    # the middle of a command.
    workflows = n8n_trigger.configured_workflows()
    capabilities = []
    if workflows:
        capabilities.append(f"{len(workflows)} n8n workflows")
    if db_connector.is_configured():
        capabilities.append("database queries")
    if capabilities:
        print("[Jarvis] Automation online: " + " and ".join(capabilities) + ".")
    else:
        print("[Jarvis] No n8n webhooks or database credentials configured (see .env.example).")

    if _brain.available:
        print(
            f"[Jarvis] Supervisor: {_brain.provider_name} ({_brain.model}), "
            f"tools: {', '.join(_brain.tool_names)}."
        )

    # The listening loop itself lives in session_manager.py: STANDBY
    # until a wake phrase, then ACTIVE for as long as the user keeps
    # talking, with a non-blocking inactivity timer ending the session.
    # main.py's job is only to hand it the router.
    # ACTIVE-mode ears. Loaded on a background thread so the ~460 MB
    # model is ready by the time somebody says the wake word, instead of
    # costing them a pause mid-command. STANDBY stays on Vosk either
    # way - see session_manager's "HYBRID SPEECH-TO-TEXT".
    _whisper = WhisperTranscriber(voice=_voice)
    _whisper.preload_async()

    # Background worker for the slow tools. A daemon, and optional: if it
    # fails to start it says why and the assistant runs without it rather
    # than refusing to boot.
    #
    # Vision is NOT started here, because there is nothing to start any
    # more: screenshots are taken on demand inside the analyze_screen
    # tool. Nothing looks at the screen until someone asks about it.
    task_dispatcher.dispatcher()          # starts the async loop thread

    _session = SessionManager(
        voice=_voice,
        dispatch=_dispatch,
        wake_engine=_build_standby_engine(),
        whisper=_whisper,
    )

    # Console only, not spoken - see "Silent Startup". Jarvis waits for
    # the wake phrase silently rather than announcing itself first.
    print(f"[Jarvis] Say {_session.wake_hint} to wake me.")

    try:
        _session.run()
    finally:
        # Order matters: stop producing work, then stop the consumers,
        # then the audio. Each shutdown is independently guarded so one
        # hanging thread cannot prevent the others from being released.
        _session.stop()
        for name, stop in (
            ("dispatcher", task_dispatcher.shutdown),
        ):
            try:
                stop()
            except Exception as exc:  # noqa: BLE001 - exit path stays quiet
                print(f"[Jarvis] {name} shutdown: {type(exc).__name__}: {exc}")
        _voice.speak("Session ended.")
        _voice.shutdown()


if __name__ == "__main__":
    main()
