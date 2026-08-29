"""
Utility: Vosk speech-to-text model management (STANDBY recogniser)

Vosk is now half of a hybrid STT stack, and this module owns that half:

    STANDBY  Vosk    <- here. Small, cheap, always-on, English. Its only
                        job is spotting the wake phrase, which it does
                        against an open microphone for hours at a time.
    ACTIVE   Whisper <- whisper_manager.py. Big, multilingual, loaded
                        once and used only inside a session, so Thai and
                        Thai-English commands are understood.

Keep this model small on purpose. Making it bigger would buy nothing:
nothing said in STANDBY is ever dispatched, and every command is
re-transcribed by Whisper anyway.

Vosk runs speech recognition entirely on-device, but ships no model
weights with the pip package - the model is a separate ~40 MB download.
This module owns that lifecycle, mirroring how train_jarvis.py handles
the openWakeWord model files.

Models live in a local .models/ directory next to this file, NOT inside
site-packages, so they survive `pip install --force-reinstall` and are
easy to inspect or delete.

Run standalone:

    python vosk_manager.py            # download the model if missing
    python vosk_manager.py --status   # report what's installed
    python vosk_manager.py --force    # re-download, replacing any existing copy

Or call ensure_model() from code - voice_engine.py does this on startup.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import zipfile
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

# Where models are stored (project-local, hidden-ish, easy to wipe).
MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".models")

# Lightweight English model: ~40 MB on disk, fast enough for real-time
# command recognition on CPU. Swap to a larger model (e.g.
# vosk-model-en-us-0.22, ~1.8 GB) for better accuracy on long-form speech.
MODEL_NAME = "vosk-model-small-en-us-0.15"
MODEL_URL = f"https://alphacephei.com/vosk/models/{MODEL_NAME}.zip"
MODEL_APPROX_MB = 40

# Subdirectories every valid Vosk model contains. Used to tell a real
# model apart from a half-extracted or empty folder.
_REQUIRED_SUBDIRS = ("am", "conf")


def model_path() -> str:
    """Absolute path where the Vosk model is expected to live."""
    return os.path.join(MODELS_DIR, MODEL_NAME)


def is_installed(path: Optional[str] = None) -> bool:
    """
    True only if the model directory exists AND looks structurally
    valid. A partially extracted folder reports False so the caller
    re-downloads rather than failing cryptically inside Vosk.
    """
    target = path or model_path()
    if not os.path.isdir(target):
        return False
    return all(os.path.isdir(os.path.join(target, sub)) for sub in _REQUIRED_SUBDIRS)


def manual_instructions() -> str:
    """Copy-pasteable instructions for installing the model by hand."""
    return (
        f"  1. Download: {MODEL_URL}\n"
        f"  2. Unzip it so the folder lands at:\n"
        f"       {model_path()}\n"
        f"     (that folder must directly contain 'am' and 'conf' subfolders)\n"
        f"\n"
        f"  Or just run:  python vosk_manager.py"
    )


def _report_progress(downloaded: int, total: int, state: dict) -> None:
    """
    Single-line download progress. Only redraws when the whole-number
    percentage changes, so piping this to a log file doesn't produce
    thousands of near-identical lines.
    """
    if total > 0:
        pct = int(downloaded * 100 / total)
        if pct == state.get("pct"):
            return
        state["pct"] = pct
        bar_len = 28
        filled = bar_len * pct // 100
        bar = "#" * filled + "-" * (bar_len - filled)
        sys.stdout.write(
            f"\r  [{bar}] {pct:3d}%  {downloaded / 1e6:6.1f} / {total / 1e6:.1f} MB"
        )
    else:
        mb = int(downloaded / 1e6)
        if mb == state.get("mb"):
            return
        state["mb"] = mb
        sys.stdout.write(f"\r  {mb} MB downloaded")
    sys.stdout.flush()


def _download_to(dest_zip: str) -> bool:
    """Streams MODEL_URL to dest_zip, printing progress. True on success."""
    try:
        with urlopen(MODEL_URL, timeout=30) as response:
            total = int(response.headers.get("Content-Length", 0))
            downloaded = 0
            chunk_size = 1 << 16  # 64 KB
            progress_state: dict = {}
            with open(dest_zip, "wb") as fh:
                while True:
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    fh.write(chunk)
                    downloaded += len(chunk)
                    _report_progress(downloaded, total, progress_state)
        print()  # close the progress line
        return True
    except (URLError, HTTPError) as exc:
        print(f"\n[Vosk] Download failed: {exc}")
        print("[Vosk] Check your internet connection, or install the model manually:")
        print(manual_instructions())
        return False
    except OSError as exc:
        print(f"\n[Vosk] Could not write the download: {exc}")
        return False


def download_model(force: bool = False) -> Optional[str]:
    """
    Downloads and extracts the Vosk model into .models/.

    Downloads to a temporary directory and only moves the finished model
    into place at the end, so an interrupted download can never leave a
    half-extracted folder that looks installed.

    Returns the model path on success, None on failure.
    """
    target = model_path()

    if is_installed(target) and not force:
        print(f"[Vosk] Model already installed: {target}")
        return target

    if force and os.path.isdir(target):
        print(f"[Vosk] Removing existing model at {target} ...")
        try:
            shutil.rmtree(target)
        except OSError as exc:
            print(f"[Vosk] Could not remove the old model: {exc}")
            return None

    try:
        os.makedirs(MODELS_DIR, exist_ok=True)
    except OSError as exc:
        print(f"[Vosk] Could not create {MODELS_DIR}: {exc}")
        return None

    print(f"[Vosk] Downloading {MODEL_NAME} (~{MODEL_APPROX_MB} MB, one time)...")
    print(f"[Vosk] Source: {MODEL_URL}")

    with tempfile.TemporaryDirectory(prefix="vosk-dl-") as tmpdir:
        zip_path = os.path.join(tmpdir, f"{MODEL_NAME}.zip")
        if not _download_to(zip_path):
            return None

        print("[Vosk] Extracting ...")
        extract_dir = os.path.join(tmpdir, "extracted")
        try:
            with zipfile.ZipFile(zip_path) as archive:
                archive.extractall(extract_dir)
        except (zipfile.BadZipFile, OSError) as exc:
            print(f"[Vosk] Extraction failed: {exc}")
            print("[Vosk] The download may be corrupt. Retry with: python vosk_manager.py --force")
            return None

        # The archive normally contains a single top-level model folder,
        # but don't assume it is named exactly MODEL_NAME.
        extracted = os.path.join(extract_dir, MODEL_NAME)
        if not os.path.isdir(extracted):
            candidates = [
                os.path.join(extract_dir, entry)
                for entry in os.listdir(extract_dir)
                if os.path.isdir(os.path.join(extract_dir, entry))
            ]
            if len(candidates) != 1:
                print(f"[Vosk] Unexpected archive layout in {extract_dir}: {candidates}")
                return None
            extracted = candidates[0]

        if not is_installed(extracted):
            print(f"[Vosk] Extracted folder is missing expected subfolders: {extracted}")
            return None

        try:
            shutil.move(extracted, target)
        except OSError as exc:
            print(f"[Vosk] Could not move the model into place: {exc}")
            return None

    if not is_installed(target):
        print(f"[Vosk] Model landed at {target} but failed validation.")
        return None

    print(f"[Vosk] Model installed: {target}")
    return target


def ensure_model(auto_download: bool = True, quiet: bool = False) -> Optional[str]:
    """
    Returns a usable model path, downloading it if necessary.

    Returns None (never raises) when the model is absent and cannot be
    fetched, so callers can fall back to typed input instead of dying.
    """
    target = model_path()
    if is_installed(target):
        return target

    if not auto_download:
        if not quiet:
            print(f"[Vosk] No model at {target} and auto-download is disabled.")
            print(manual_instructions())
        return None

    if not quiet:
        print(f"[Vosk] Speech-to-text model not found at {target}.")
    return download_model()


def status() -> int:
    """Prints what is installed and where. Returns a shell exit code."""
    target = model_path()
    installed = is_installed(target)

    print("=" * 68)
    print("  VOSK SPEECH-TO-TEXT MODEL STATUS")
    print("=" * 68)
    print(f"  Models dir : {MODELS_DIR}")
    print(f"  Model name : {MODEL_NAME}")
    print(f"  Expected at: {target}")
    print(f"  Installed  : {'yes' if installed else 'NO'}")

    if installed:
        total = 0
        for root, _dirs, files in os.walk(target):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
        print(f"  Size on disk: {total / 1e6:.1f} MB")

    try:
        import vosk  # noqa: F401

        print("  vosk package: installed")
    except ImportError:
        print("  vosk package: NOT INSTALLED  ->  pip install vosk")

    print("=" * 68)
    if not installed:
        print("To install the model:")
        print(manual_instructions())
        return 1
    print("Speech-to-text is ready and runs 100% offline.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the offline Vosk STT model.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--status", action="store_true", help="Report what is installed.")
    group.add_argument("--force", action="store_true", help="Re-download, replacing any existing model.")
    args = parser.parse_args()

    if args.status:
        return status()

    result = download_model(force=args.force)
    if result is None:
        return 1

    # Prove the model actually loads before declaring success.
    try:
        from vosk import Model, SetLogLevel

        SetLogLevel(-1)
        Model(result)
        print("[Vosk] Model loaded successfully - offline STT is ready.")
        return 0
    except ImportError:
        print("[Vosk] Model downloaded, but the 'vosk' package isn't installed.")
        print("[Vosk] Run: pip install vosk")
        return 1
    except Exception as exc:  # noqa: BLE001 - report load failures clearly
        print(f"[Vosk] Model downloaded but failed to load: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
