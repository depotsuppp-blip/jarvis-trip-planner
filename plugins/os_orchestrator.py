"""
Jarvis plugin: OS Orchestrator

Brings the whole Subtrack development environment up from one spoken
command - "start work", "open Subtrack" - instead of three terminals and
a Docker Desktop click:

    docker start n8n_stable          the automation/database container
    uvicorn main:app --reload        FastAPI backend, in its own console
    npm run dev                      Next.js frontend, in its own console

WHY THE SERVERS GET THEIR OWN CONSOLE WINDOWS
---------------------------------------------
Both servers run until somebody stops them, and both stream logs
forever. Started as ordinary children they would inherit Jarvis's
console: their output would scroll over the assistant's own, and killing
Jarvis would kill the servers with it. CREATE_NEW_CONSOLE gives each one
its own window, detached from this one, which is what a developer
actually wants - the logs are there to read, and closing Jarvis leaves
the environment running.

The Docker call is the exception: `docker start` returns as soon as the
container is up, so it is waited on (check=False - a container that is
already running exits non-zero, and that is a success from the user's
point of view, not a failure).

THIS FILE IS AN IMPLEMENTATION MODULE, NOT AN AUTO-LOADED PLUGIN
----------------------------------------------------------------
It deliberately declares no TOOL_SPEC. start_subtrack_env and
stop_subtrack_env are registered as built-in tools in llm_brain.py,
which imports this module - so each tool exists exactly once. A
TOOL_SPEC here would register a second tool with the same name and the
provider would reject the request.

THE BACKEND RUNS ON THE PROJECT'S OWN INTERPRETER
-------------------------------------------------
FastAPI projects keep their dependencies in a virtual environment, so
launching `python -m uvicorn` from whatever interpreter Jarvis happens
to be running would import the wrong site-packages - usually as a
ModuleNotFoundError for uvicorn itself, in a console window that closes
before anyone can read it. The backend folder is searched for a venv
interpreter first (venv/, .venv/, env/), and only falls back to the
system python when there is none, saying so in the summary.

STOPPING IS NOT THE MIRROR IMAGE OF STARTING
--------------------------------------------
stop_subtrack_env kills what THIS process launched, tracked by the
Popen objects the start helpers keep, and nothing else. The obvious
shortcut - taskkill /F /IM python.exe - would kill Jarvis itself, since
Jarvis is a python.exe; /IM node.exe would take out every unrelated
Node process the user has open, editor language servers included.
Neither is worth the convenience.

Killing the tracked PID alone is not enough either. `npm run dev`
spawns npm, which spawns node, which spawns the Next.js dev server, and
`uvicorn --reload` runs a reloader parent with a worker child. Killing
only the parent leaves the real server holding the port, so on Windows
the whole tree goes at once via `taskkill /F /T /PID`.

The registry lives in this process, so a Jarvis restarted since the
servers went up has nothing to kill. That is reported plainly rather
than papered over with a cheerful "all stopped" - those windows are
still open, and the user is the only one who can close them.

CONFIGURATION (.env)
--------------------
    SUBTRACK_FRONTEND_PATH   folder containing the frontend package.json
    SUBTRACK_BACKEND_PATH    folder containing the FastAPI app
    SUBTRACK_BACKEND_APP     uvicorn target, default main:app
    SUBTRACK_DOCKER_CONTAINER  container to start (default: n8n_stable)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading

try:  # Loading .env here as well keeps the module usable standalone.
    from dotenv import load_dotenv
except Exception:  # noqa: BLE001 - a convenience, never a requirement
    load_dotenv = None  # type: ignore[assignment]


# The container holding n8n (and, through it, the database work). Named
# in .env so a renamed container does not need a code change.
DEFAULT_CONTAINER = "n8n_stable"

# Windows-only flag: each server gets its own console window instead of
# sharing Jarvis's. getattr keeps this module importable on Linux and
# macOS, where 0 means "no special creation flags".
CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)

# Seconds to wait on `docker start`. Long enough for a cold container,
# short enough that a wedged Docker daemon does not hold the assistant.
DOCKER_TIMEOUT = 45.0

# Seconds to wait on `docker stop`. Docker sends SIGTERM, waits out a
# ten-second grace period, and only then kills the container - so this
# has to be comfortably longer than that or the wait would time out on a
# perfectly healthy shutdown.
DOCKER_STOP_TIMEOUT = 45.0

# Seconds to wait on one taskkill, and then on the process actually
# dying. Killing a process tree is near-instant; anything slower than
# this is a wedged handle, not work still in progress.
KILL_TIMEOUT = 15.0

# What this process launched, by spoken label -> Popen. Written by the
# start helpers, read by stop_subtrack_env, and the whole reason the
# shutdown path never has to guess at a PID or match on an image name.
#
# The lock is not paranoia: tools run on the dispatcher's thread pool,
# so a "stop work" arriving while "start work" is still launching would
# otherwise mutate this dict from two threads at once.
_LAUNCHED: dict[str, subprocess.Popen] = {}
_LAUNCHED_LOCK = threading.Lock()

# What uvicorn is pointed at. "main:app" is the FastAPI convention;
# projects that keep the app in a package override it in .env with
# something like "app.main:app".
DEFAULT_BACKEND_APP = "main:app"

# Where a project keeps its virtual environment, in the order they are
# tried. The Windows layout (Scripts/) comes first because that is the
# machine this runs on; the POSIX layout keeps the module portable.
VENV_PYTHON_PATHS: tuple[str, ...] = (
    os.path.join("venv", "Scripts", "python.exe"),
    os.path.join(".venv", "Scripts", "python.exe"),
    os.path.join("env", "Scripts", "python.exe"),
    os.path.join("venv", "bin", "python"),
    os.path.join(".venv", "bin", "python"),
    os.path.join("env", "bin", "python"),
)


def _project_env_path() -> str:
    """The project's .env, one directory up from plugins/."""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), ".env")


def _ensure_env_loaded() -> None:
    """Best-effort .env load. Already-set variables always win."""
    if load_dotenv is None:
        return
    try:
        load_dotenv(_project_env_path(), override=False)
    except Exception:  # noqa: BLE001 - the tool still works with real env vars
        pass


def _mangled_by_quotes(value: str) -> bool:
    """
    True if `value` looks like a Windows path wrecked by .env quoting.

    python-dotenv processes escape sequences inside DOUBLE-quoted
    values, so SUBTRACK_BACKEND_PATH="C:\\path\\to\\api" arrives with a
    literal TAB where \\t was written - and the same trap is waiting in
    C:\\temp, C:\\new, C:\\build, C:\\reports. No Windows path can contain a
    control character, so finding one is proof of the quoting bug rather
    than a guess, and it is worth naming: the alternative is a
    "folder doesn't exist" message pointing at a path the user can see
    is correct in their editor.
    """
    return any(ord(ch) < 32 for ch in value)


def _remember(label: str, process: subprocess.Popen) -> None:
    """Records a launched server so stop_subtrack_env can find it later."""
    with _LAUNCHED_LOCK:
        _LAUNCHED[label] = process


def _forget(label: str) -> None:
    """Drops a server that is confirmed dead, so a retry is not offered one."""
    with _LAUNCHED_LOCK:
        _LAUNCHED.pop(label, None)


def _start_docker(container: str) -> str:
    """Starts the container. Returns one spoken clause about what happened."""
    if shutil.which("docker") is None:
        return "Docker isn't installed or isn't on the PATH, so the container was skipped"

    try:
        completed = subprocess.run(
            ["docker", "start", container],
            check=False,
            capture_output=True,
            text=True,
            timeout=DOCKER_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"Docker didn't respond within {DOCKER_TIMEOUT:.0f} seconds, so {container} may not be up"
    except Exception as exc:  # noqa: BLE001 - a tool must not crash the loop
        return f"Docker failed to start {container} ({type(exc).__name__})"

    if completed.returncode == 0:
        return f"the {container} container is running"

    # Docker puts the reason on stderr; the most common one by far is
    # "No such container", which is worth saying out loud because it is
    # a configuration mistake the user can fix.
    detail = (completed.stderr or completed.stdout or "").strip().splitlines()
    reason = detail[-1] if detail else f"exit code {completed.returncode}"
    print(f"[Orchestrator] docker start {container}: {reason}")
    if "no such container" in reason.lower():
        return f"there is no Docker container called {container}"
    return f"the {container} container didn't start"


def _find_venv_python(path: str) -> str:
    """The project's own interpreter, or "" if it has no virtual env."""
    for relative in VENV_PYTHON_PATHS:
        candidate = os.path.join(path, relative)
        if os.path.isfile(candidate):
            return candidate
    return ""


def _app_module_exists(path: str, app_spec: str) -> bool:
    """
    True if the module half of "module:attribute" is actually there.

    Checked because uvicorn's failure for a wrong target is an import
    error printed into a console window that vanishes - the user sees a
    flash and no server, with nothing to read afterwards.
    """
    module = app_spec.split(":", 1)[0].strip()
    if not module:
        return False
    relative = module.replace(".", os.sep)
    return os.path.isfile(os.path.join(path, relative + ".py")) or os.path.isfile(
        os.path.join(path, relative, "__init__.py")
    )


def _start_python_backend(path: str, app_spec: str) -> str:
    """
    Launches `python -m uvicorn <app> --reload` in its own console.

    Prefers the project's venv interpreter over Jarvis's own, because
    uvicorn and the app's dependencies live in that venv and nowhere
    else. Returns one clause describing the outcome; never raises.
    """
    if not os.path.isdir(path):
        return f"the backend folder doesn't exist at {path}"

    interpreter = _find_venv_python(path)
    using_venv = bool(interpreter)
    if not interpreter:
        interpreter = shutil.which("python") or shutil.which("python3") or ""
    if not interpreter:
        return "no Python interpreter was found, so the backend wasn't started"

    if not _app_module_exists(path, app_spec):
        module = app_spec.split(":", 1)[0]
        return (
            f"the backend has no {module}.py, so uvicorn had nothing to serve - "
            "set SUBTRACK_BACKEND_APP if the app lives somewhere else"
        )

    command = [interpreter, "-m", "uvicorn", app_spec, "--reload"]
    try:
        process = subprocess.Popen(
            command,
            cwd=path,
            creationflags=CREATE_NEW_CONSOLE,
            close_fds=True,
        )
    except Exception as exc:  # noqa: BLE001 - report it, do not raise
        print(f"[Orchestrator] backend failed to launch: {type(exc).__name__}: {exc}")
        return f"the backend server failed to launch ({type(exc).__name__})"

    _remember("backend", process)
    print(
        f"[Orchestrator] backend: {' '.join(command[1:])} in {path} "
        f"(PID {process.pid}, {'venv' if using_venv else 'system'} interpreter)."
    )
    if using_venv:
        return "the backend server is starting in its own window"
    # Worth saying out loud: this is the usual reason a backend window
    # opens and immediately dies with "No module named uvicorn".
    return (
        "the backend server is starting in its own window, but on the system "
        "Python - I couldn't find a virtual environment in the backend folder"
    )


def _start_node_server(label: str, path: str, script: str) -> str:
    """
    Launches `npm run <script>` in `path`, in its own console window.

    Returns one clause describing the outcome. Never raises: a missing
    folder or a missing npm is a thing to tell the user about, not a
    reason to take down the assistant.
    """
    if not os.path.isdir(path):
        return f"the {label} folder doesn't exist at {path}"

    if not os.path.isfile(os.path.join(path, "package.json")):
        return f"there's no package.json in the {label} folder"


    # npm on Windows is npm.cmd, which CreateProcess cannot launch from a
    # bare "npm" argument - resolving it here is what makes the list form
    # work without shell=True.
    npm = shutil.which("npm")
    if npm is None:
        return f"npm isn't on the PATH, so the {label} server wasn't started"

    try:
        process = subprocess.Popen(
            [npm, "run", script],
            cwd=path,
            creationflags=CREATE_NEW_CONSOLE,
            close_fds=True,
        )
    except Exception as exc:  # noqa: BLE001 - report it, do not raise
        print(f"[Orchestrator] {label} failed to launch: {type(exc).__name__}: {exc}")
        return f"the {label} server failed to launch ({type(exc).__name__})"

    _remember(label, process)
    print(f"[Orchestrator] {label}: npm run {script} in {path} (PID {process.pid}).")
    return f"the {label} server is starting in its own window"


def start_subtrack_env() -> str:
    """
    Brings up Docker, the NestJS backend, and the frontend dev server.

    Returns a spoken summary of what came up and what did not. Each
    service is independent: a missing frontend folder must not stop the
    backend from starting, so every step reports its own outcome and the
    summary names all three.
    """
    _ensure_env_loaded()

    frontend = (os.getenv("SUBTRACK_FRONTEND_PATH") or "").strip().strip('"')
    backend = (os.getenv("SUBTRACK_BACKEND_PATH") or "").strip().strip('"')

    missing = [
        name
        for name, value in (
            ("SUBTRACK_FRONTEND_PATH", frontend),
            ("SUBTRACK_BACKEND_PATH", backend),
        )
        if not value
    ]
    if missing:
        verb = "is" if len(missing) == 1 else "are"
        return (
            f"I can't start the Subtrack environment: {' and '.join(missing)} "
            f"{verb} not set. Tell the user to add it to the .env file in the "
            "Jarvis folder, pointing at the project folder that holds "
            "package.json. Nothing was started."
        )

    mangled = [
        name
        for name, value in (
            ("SUBTRACK_FRONTEND_PATH", frontend),
            ("SUBTRACK_BACKEND_PATH", backend),
        )
        if _mangled_by_quotes(value)
    ]
    if mangled:
        verb = "is" if len(mangled) == 1 else "are"
        return (
            f"I can't start the Subtrack environment: {' and '.join(mangled)} "
            f"{verb} wrapped in double quotes in the .env file, which turns the "
            "backslashes in the path into escape characters. Tell the user to "
            "remove the quotes around the path. Nothing was started."
        )

    container = (os.getenv("SUBTRACK_DOCKER_CONTAINER") or "").strip() or DEFAULT_CONTAINER
    app_spec = (os.getenv("SUBTRACK_BACKEND_APP") or "").strip() or DEFAULT_BACKEND_APP

    print("[Orchestrator] Starting the Subtrack development environment...")
    results = [
        _start_docker(container),
        _start_python_backend(backend, app_spec),
        _start_node_server("frontend", frontend, "dev"),
    ]

    summary = "; ".join(results)
    print(f"[Orchestrator] Done: {summary}.")
    return (
        f"Subtrack environment: {summary}. Summarise this for the user in one "
        "or two sentences - say what came up, and name anything that did not."
    )


def _stop_process(label: str, process: subprocess.Popen) -> str:
    """
    Kills one launched server and everything it spawned.

    Returns one spoken clause. Never raises: a server that refuses to
    die is something to tell the user about, not a reason to abandon the
    rest of the shutdown.
    """
    if process.poll() is not None:
        _forget(label)
        return f"the {label} server had already stopped"

    # Windows first, and /T is the whole point: npm and the uvicorn
    # reloader are both just parents of the process actually holding the
    # port, so killing the tracked PID on its own would leave the server
    # running and the port taken.
    if os.name == "nt" and shutil.which("taskkill") is not None:
        try:
            completed = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                check=False,
                capture_output=True,
                text=True,
                timeout=KILL_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 - fall through to the portable path
            print(f"[Orchestrator] taskkill on {label} failed: {type(exc).__name__}: {exc}")
        else:
            if completed.returncode == 0:
                _forget(label)
                return f"the {label} server is stopped"
            detail = (completed.stderr or completed.stdout or "").strip().splitlines()
            reason = detail[-1] if detail else f"exit code {completed.returncode}"
            print(f"[Orchestrator] taskkill {process.pid} ({label}): {reason}")

    # No taskkill, or it refused. This path only reaches the process we
    # started ourselves, so a child may outlive it - which is why the
    # console keeps the PID: it is the one thing the user needs to go
    # and finish the job by hand.
    try:
        process.terminate()
    except Exception as exc:  # noqa: BLE001 - already gone, or not ours to kill
        print(f"[Orchestrator] terminate on {label} failed: {type(exc).__name__}: {exc}")

    try:
        process.wait(timeout=KILL_TIMEOUT)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            process.wait(timeout=KILL_TIMEOUT)
        except Exception:  # noqa: BLE001 - it is as dead as it is going to get
            pass
    except Exception:  # noqa: BLE001 - a dead process is the outcome we wanted
        pass

    if process.poll() is None:
        print(f"[Orchestrator] {label} (PID {process.pid}) survived being stopped.")
        return f"the {label} server didn't respond to being stopped"

    _forget(label)
    return f"the {label} server is stopped"


def _stop_docker(container: str) -> str:
    """Stops the container. Returns one spoken clause about what happened."""
    if shutil.which("docker") is None:
        return "Docker isn't installed or isn't on the PATH, so the container was skipped"

    try:
        completed = subprocess.run(
            ["docker", "stop", container],
            check=False,
            capture_output=True,
            text=True,
            timeout=DOCKER_STOP_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return (
            f"Docker didn't respond within {DOCKER_STOP_TIMEOUT:.0f} seconds, "
            f"so {container} may still be running"
        )
    except Exception as exc:  # noqa: BLE001 - a tool must not crash the loop
        return f"Docker failed to stop {container} ({type(exc).__name__})"

    if completed.returncode == 0:
        return f"the {container} container is stopped"

    detail = (completed.stderr or completed.stdout or "").strip().splitlines()
    reason = detail[-1] if detail else f"exit code {completed.returncode}"
    print(f"[Orchestrator] docker stop {container}: {reason}")
    if "no such container" in reason.lower():
        return f"there is no Docker container called {container}"
    return f"the {container} container didn't stop"


def stop_subtrack_env() -> str:
    """
    Takes down what start_subtrack_env brought up.

    Stops the servers this process launched, then the Docker container.
    Each step is independent - a container that will not stop must not
    leave the servers running - so every one reports its own outcome and
    the summary names all of them.
    """
    _ensure_env_loaded()
    container = (os.getenv("SUBTRACK_DOCKER_CONTAINER") or "").strip() or DEFAULT_CONTAINER

    print("[Orchestrator] Stopping the Subtrack development environment...")

    # Snapshot under the lock, then do the killing outside it: taskkill
    # and wait() take seconds, and holding the lock across them would
    # block a concurrent start for the length of the shutdown.
    with _LAUNCHED_LOCK:
        launched = list(_LAUNCHED.items())

    results = [_stop_process(label, process) for label, process in launched]
    if not results:
        # Nothing tracked: either nothing was started from here, or
        # Jarvis has been restarted since it was. Both leave real windows
        # on the user's desktop, and saying so is worth more than a
        # confident summary of work that did not happen.
        results.append(
            "I have no record of starting the backend or frontend in this "
            "session, so if their windows are open they were left alone"
        )

    results.append(_stop_docker(container))

    summary = "; ".join(results)
    print(f"[Orchestrator] Done: {summary}.")
    return (
        f"Subtrack shutdown: {summary}. Summarise this for the user in one "
        "or two sentences - say what stopped, and name anything that did not."
    )


if __name__ == "__main__":
    import sys

    # `python -m plugins.os_orchestrator stop` exercises the shutdown
    # path on its own. Starting and stopping in one process is also the
    # only way to test it end to end, since the registry it reads is
    # populated by start_subtrack_env.
    if len(sys.argv) > 1 and sys.argv[1].lower() in ("stop", "down"):
        print(stop_subtrack_env())
    else:
        print(start_subtrack_env())
