# syntax=docker/dockerfile:1

# Jarvis brain (api_server.py, FastAPI) - production image.
#
# Build (from the repo root):
#     docker compose build backend
#
# Python 3.11 is the version this project is developed and tested on.
# Two stages: the builder carries a compiler and the PortAudio headers (PyAudio
# has no Linux wheel, so requirements.txt compiles it); only the finished
# virtualenv is copied into the runtime image.

# --- builder: install requirements.txt into a virtualenv --------------------
FROM python:3.11-slim AS builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential portaudio19-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copied on its own first, so a code-only change does not re-install packages.
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

# --- runtime ---------------------------------------------------------------
FROM python:3.11-slim

# libportaudio2: PyAudio's runtime library. libgomp1: OpenMP, which
# onnxruntime and CTranslate2 (faster-whisper) link against.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libportaudio2 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN useradd --create-home --uid 10001 jarvis
WORKDIR /app

# State the app writes next to its sources: the faster-whisper weights
# (whisper_manager.py -> .models/whisper) and the daily token record
# (llm_budget.py -> .data). Created and owned by `jarvis` here so that named
# volumes mounted over them (docker-compose.yml) start out writable.
RUN mkdir -p /app/.models /app/.data && chown -R jarvis:jarvis /app

COPY --from=builder /opt/venv /opt/venv
# Which files land here is decided by .dockerignore (an allowlist).
COPY --chown=jarvis:jarvis . .

USER jarvis
EXPOSE 8000

# 0.0.0.0, not api_server.py's default 127.0.0.1: inside a container loopback is
# unreachable from the frontend. One worker on purpose - the brain is a single
# stateful object behind a lock, and each worker would load its own copy of the
# Whisper model.
CMD ["uvicorn", "api_server:app", "--host", "0.0.0.0", "--port", "8000"]
