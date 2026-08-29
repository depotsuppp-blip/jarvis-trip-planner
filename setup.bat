@echo off
REM setup.bat - One-click environment setup for Jarvis.
REM Creates a local virtual environment, activates it, and installs
REM everything listed in requirements.txt. Safe to double-click, and
REM safe to re-run if it fails partway through.

setlocal

echo ============================================
echo   Jarvis - Environment Setup
echo ============================================
echo.

REM Always run from the folder this script lives in, regardless of
REM where it was launched from (e.g. double-clicked in Explorer).
cd /d "%~dp0"

REM --- 0. Make sure Python is available on PATH ---------------------------
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python was not found on PATH.
    echo         Install Python 3.10+ from https://python.org and re-run this script.
    pause
    exit /b 1
)

REM --- 1. Create the virtual environment (skip if it already exists) ------
if exist ".venv\Scripts\python.exe" (
    echo [1/4] Virtual environment already exists - skipping creation.
) else (
    echo [1/4] Creating virtual environment in .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create the virtual environment.
        pause
        exit /b 1
    )
)

REM --- 2. Activate it -------------------------------------------------------
echo [2/4] Activating virtual environment ...
call ".venv\Scripts\activate.bat"
if errorlevel 1 (
    echo [ERROR] Failed to activate the virtual environment.
    pause
    exit /b 1
)

REM --- 3. Upgrade pip ---------------------------------------------------------
echo [3/4] Upgrading pip ...
python -m pip install --upgrade pip
if errorlevel 1 (
    echo [ERROR] Failed to upgrade pip.
    pause
    exit /b 1
)

REM --- 4. Install project dependencies -----------------------------------
if not exist "requirements.txt" (
    echo [ERROR] requirements.txt not found in %cd%
    pause
    exit /b 1
)

echo [4/6] Installing dependencies from requirements.txt ...
pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Dependency installation failed. See the output above for details.
    pause
    exit /b 1
)

REM --- 5. Download the wake word model files -----------------------------
REM pip installs openwakeword's model DEFINITIONS but not the model FILES.
REM Without this step the first run dies with:
REM     ONNXRuntimeError ... NO_SUCHFILE ... hey_jarvis_v0.1.onnx
echo [5/6] Downloading wake word model files ...
python train_jarvis.py
if errorlevel 1 (
    echo [WARN] Wake word model download failed - check your internet connection.
    echo        You can retry later with:  .venv\Scripts\python train_jarvis.py
)

REM --- 6. Download the offline speech-to-text model ----------------------
REM The vosk pip package ships no model weights. Without this step the
REM assistant still runs, but falls back to typed input instead of voice.
echo [6/6] Downloading offline speech-to-text model (~40 MB) ...
python vosk_manager.py
if errorlevel 1 (
    echo [WARN] Speech model download failed - check your internet connection.
    echo        Jarvis will fall back to typed input until you run:
    echo            .venv\Scripts\python vosk_manager.py
)

echo.
echo ============================================
echo   Setup complete! Jarvis environment is ready.
echo.
echo   Run Jarvis with:
echo       .venv\Scripts\python main.py
echo ============================================
pause
