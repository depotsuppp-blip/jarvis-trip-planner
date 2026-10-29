"""
The Head of Creative: original images, made on request and kept as files.

Phase 1 and 2 of the capability orchestrator: an Artifact Manager (artifacts/ at the
repo root, where what Jarvis makes is saved) and the first native capability, image
generation through OpenAI's Images API.

    manager (llm_brain) --delegate_to_creative(image_description)--> CreativeAgent
        -> OpenAI images.generate -> the picture's bytes, checked to be an image
        -> saved as artifacts/image_YYYYMMDD_HHMMSS.png
        <- "SUCCESS: ... Path: <absolute path>"   or   "ERROR: <what happened>"

In the web chat the brain swaps the Path line for the picture itself, a Markdown image
at /artifacts/<file name> (markdown_image): api_server.py serves this folder at that
path, and the web app passes it on to the page (TripPlanner_Web/app/artifacts/[name]).

NOT A LANGUAGE-MODEL LOOP. The other agents (base.SpecialistAgent) are models with
tools. This one has nothing to reason about - the manager writes the description and
one API call makes the picture - so it is a plain class and spends no tokens.

THE MODEL. The spec named DALL-E 3, but OpenAI retired dall-e-2 and dall-e-3 from the
API on 2026-05-12 (checked 2026-10-29: the owner's key answers 404 model_not_found,
and the openai SDK marks both as retired). Their replacements, the GPT image models,
are tried in this order:

    primary   gpt-image-2   JARVIS_IMAGE_MODEL
    fallback  gpt-image-1   JARVIS_IMAGE_FALLBACK_MODEL ("none" = no fallback)

The fallback is tried only when the primary is not available to this key (404), not
allowed (403), rate limited or failing (429, 5xx). Not when the account is out of
credit, the key is rejected, OpenAI's safety system refused the description, or the
call timed out: the second model would fail the same way, or take the web chat past
its two-minute limit.

HOW THE PICTURE ARRIVES. GPT image models send it inline, base64-encoded. DALL-E
models sent a link instead (valid for an hour); that path is kept for any model that
still answers with one, and downloads it with requests: https only, no redirects, a
timeout, at most MAX_IMAGE_BYTES. Either way the bytes must be a PNG, JPEG or WebP
image before anything is written, and a file is never overwritten.

COST. Every image is billed to the OPENAI_API_KEY account - including images the web
hub's other signed-in users ask for. So the same description again within
SAME_REQUEST_SECONDS returns the file already made instead of paying twice, and at
most JARVIS_IMAGE_HOURLY_LIMIT images (default 10, 0 = no limit) are made per hour.
Quality is "medium" unless JARVIS_IMAGE_QUALITY says otherwise (low, medium, high,
auto; xhigh and max on the gpt-image-2.5 models); size 1024x1024 unless
JARVIS_IMAGE_SIZE does (WIDTHxHEIGHT, or auto). JARVIS_CREATIVE=0 turns it off.

NEVER RAISES. Every failure comes back as one sentence starting "ERROR:" that says
what happened and whether trying again could help, for the manager to tell the user.

    python -m agents.creative_agent --check            key, models, folder; makes no image
    python -m agents.creative_agent "a red fox in snow" makes one image (billed)
"""

from __future__ import annotations

import base64
import binascii
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from llm_brain import _env, load_api_key

try:
    import requests
except ImportError:  # pragma: no cover - requests is in requirements.txt
    requests = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parent.parent
# Everything Jarvis makes. Tracked as a folder, its contents ignored (artifacts/.gitignore).
ARTIFACTS_DIR = ROOT / "artifacts"
# Where a browser finds them: api_server.py serves the folder at this path, and the web
# app answers the same path for the page (TripPlanner_Web/app/artifacts/[name]/route.ts).
ARTIFACTS_URL_PATH = "/artifacts"

DEFAULT_MODEL = "gpt-image-2"
DEFAULT_FALLBACK_MODEL = "gpt-image-1"
DEFAULT_SIZE = "1024x1024"
DEFAULT_QUALITY = "medium"
DEFAULT_HOURLY_LIMIT = 10
DEFAULT_BASE_URL = "https://api.openai.com/v1"
QUALITIES = ("low", "medium", "high", "xhigh", "max", "auto")

# The web chat waits two minutes for the whole answer (TripPlanner_Web/lib/jarvisBrain.ts,
# BRAIN_TIMEOUT_MS), and the manager makes a model call before and after this one.
IMAGE_TIMEOUT_SECONDS = 90.0
DOWNLOAD_TIMEOUT = (10.0, 60.0)  # connect, read
MAX_IMAGE_BYTES = 50 * 1024 * 1024
# DALL-E 3's limit; GPT image models take more, but no picture needs it.
MAX_DESCRIPTION_CHARS = 4000
SAME_REQUEST_SECONDS = 120
HOUR_SECONDS = 3600.0

# Failures after which the next model is tried (see THE MODEL above).
TRY_NEXT_MODEL = frozenset({"not_found", "permission", "rate_limit", "server"})

_SIZE = re.compile(r"^(auto|\d{2,5}x\d{2,5})$")


class ImageError(Exception):
    """One failed attempt. `kind` decides whether the next model is tried; `message` is for the manager."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


def image_kind(data: bytes) -> Optional[str]:
    """The file extension for these bytes when they are a PNG, JPEG or WebP image, else None."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _legacy(model: str) -> bool:
    """DALL-E models: no quality levels or output formats, and they answer with a link."""
    return model.startswith("dall-e")


def _shown(text: str, limit: int = 120) -> str:
    """`text` cut short and safe for a console that cannot print Thai."""
    text = text if len(text) <= limit else text[: limit - 1] + "…"
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return text.encode(encoding, errors="backslashreplace").decode(encoding)


def classify(exc: Exception, model: str) -> ImageError:
    """An openai SDK exception -> an ImageError whose message says what to tell the user."""
    names = {cls.__name__ for cls in type(exc).__mro__}
    status = getattr(exc, "status_code", None)
    body = getattr(exc, "body", None)
    code = detail = ""
    if isinstance(body, dict):
        error = body.get("error") if isinstance(body.get("error"), dict) else body
        code = str(error.get("code") or error.get("type") or "")
        detail = str(error.get("message") or "")
    detail = " ".join((detail or str(getattr(exc, "message", "") or exc)).split())[:240]
    text = f"{code} {detail}".lower()

    # APITimeoutError is an APIConnectionError, so it is checked first.
    if "APITimeoutError" in names:
        return ImageError(
            "timeout",
            f"making the image took longer than {IMAGE_TIMEOUT_SECONDS:.0f} seconds, so it was given up and "
            "nothing was saved. It can be asked for again.",
        )
    if "APIConnectionError" in names:
        return ImageError("connection", "OpenAI could not be reached (a network problem), so no image was made.")
    if status == 402 or "insufficient_quota" in text or "billing" in text:
        return ImageError(
            "billing",
            "the OpenAI account is out of credit or has hit its spending limit, so no image was made. "
            "The owner has to add credit at platform.openai.com; do not try again.",
        )
    if status == 429 or "RateLimitError" in names:
        return ImageError("rate_limit", "OpenAI is rate limiting image requests right now, so no image was made. "
                          "It can be asked for again in a minute.")
    if status == 401 or "AuthenticationError" in names:
        return ImageError("auth", "OpenAI rejected the API key (OPENAI_API_KEY in the .env file), so no image "
                          "was made. Do not try again.")
    if status == 403 or "PermissionDeniedError" in names:
        hint = ""
        if "verif" in text:
            hint = (" - OpenAI asks for the organization to be verified before it may use GPT image models "
                    "(platform.openai.com, Settings, Organization, General)")
        return ImageError("permission", f"this OpenAI key may not use the image model {model}{hint}. No image was made.")
    if status == 404 or "NotFoundError" in names:
        return ImageError("not_found", f"the image model {model} is not available on this OpenAI account (retired or "
                          "unknown), so no image was made. JARVIS_IMAGE_MODEL in the .env file picks another.")
    if isinstance(status, int) and status >= 500:
        return ImageError("server", f"OpenAI's image service is having trouble (HTTP {status}), so no image was "
                          "made. It may work again in a few minutes.")
    if any(word in text for word in ("moderation", "content_policy", "safety system", "safety_violation")):
        return ImageError("refused", "OpenAI's safety system refused this description, so no image was made. Tell "
                          "the user and offer a different description; do not send the same one again.")
    if status == 400 or "BadRequestError" in names:
        return ImageError("bad_request", f"OpenAI rejected the request ({detail or 'no reason given'}), so no image "
                          "was made.")
    return ImageError("unknown", f"the image could not be made ({type(exc).__name__}: {detail}).")


class CreativeAgent:
    """Makes one image per call and saves it in artifacts/. Building one makes no network call."""

    name = "creative"
    title = "Head of Creative"
    tool_name = "delegate_to_creative"

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        models: Optional[list[str]] = None,
        size: Optional[str] = None,
        quality: Optional[str] = None,
        hourly_limit: Optional[int] = None,
        artifacts_dir: Optional[Path | str] = None,
        base_url: Optional[str] = None,
        client: Any = None,
        http: Any = None,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.models = list(dict.fromkeys(models if models is not None else _models_from_env()))
        self.size = _setting("JARVIS_IMAGE_SIZE", size, DEFAULT_SIZE, lambda v: bool(_SIZE.match(v)))
        self.quality = _setting("JARVIS_IMAGE_QUALITY", quality, DEFAULT_QUALITY, lambda v: v in QUALITIES)
        self.hourly_limit = hourly_limit if hourly_limit is not None else _hourly_limit_from_env()
        self.artifacts_dir = Path(artifacts_dir) if artifacts_dir else ARTIFACTS_DIR
        self._http = http
        self._now = now or datetime.now
        self._lock = threading.Lock()
        self._made: list[float] = []  # time.monotonic() of each image made, for the hourly limit
        self._recent: dict[str, tuple[float, Path]] = {}  # description -> (when, file)
        self.unavailable_reason = ""
        self._client = client
        if client is not None:
            return
        if not self.models:
            self.unavailable_reason = "no image model is set (JARVIS_IMAGE_MODEL)"
            return
        key = api_key or load_api_key("OPENAI_API_KEY")
        if not key:
            self.unavailable_reason = "no OPENAI_API_KEY in the .env file"
            return
        try:
            import openai
        except ImportError as exc:
            self.unavailable_reason = f"the openai package is not installed ({exc})"
            return
        try:
            # base_url is always passed: the SDK takes an EMPTY OPENAI_BASE_URL= (as in
            # .env.example) as the address. No SDK retries: a retried image is billed twice.
            self._client = openai.OpenAI(
                api_key=key,
                base_url=base_url or _env("OPENAI_BASE_URL") or DEFAULT_BASE_URL,
                timeout=IMAGE_TIMEOUT_SECONDS,
                max_retries=0,
            )
        except Exception as exc:  # noqa: BLE001 - a malformed key, a proxy setting, ...
            self.unavailable_reason = f"the OpenAI client could not be created ({exc})"

    @property
    def available(self) -> bool:
        return self._client is not None

    def describe(self) -> str:
        if not self.available:
            return f"off ({self.unavailable_reason})"
        limit = f"{self.hourly_limit}/hour" if self.hourly_limit > 0 else "no hourly limit"
        return f"{' -> '.join(self.models)}, {self.quality} quality, {self.size}, {limit}, into {self.artifacts_dir}"

    # -- the work ------------------------------------------------------------

    def generate_image(self, image_description: str) -> str:
        """Makes one image from `image_description` and saves it. Returns SUCCESS + its path, or ERROR + why."""
        description = " ".join(str(image_description or "").split())
        if not description:
            return "ERROR: no image description was given. Call again with the whole description in image_description."
        if not self.available:
            return f"ERROR: image generation is not set up ({self.unavailable_reason}). Tell the user; do not try again."
        if len(description) > MAX_DESCRIPTION_CHARS:
            description = description[:MAX_DESCRIPTION_CHARS].rstrip()

        # One image at a time: two identical requests at once would otherwise both be paid for.
        with self._lock:
            again = self._already_made(description)
            if again:
                return again
            wait = self._hourly_wait()
            if wait:
                return (
                    f"ERROR: the limit of {self.hourly_limit} images an hour has been reached "
                    f"(JARVIS_IMAGE_HOURLY_LIMIT), so no image was made. The next one can be made in about "
                    f"{max(1, round(wait / 60))} minute(s)."
                )

            first: Optional[ImageError] = None
            others: list[str] = []
            for index, model in enumerate(self.models):
                started = time.monotonic()
                print(f"[Agent:creative] {model}: {_shown(description)}")
                try:
                    path, size = self._make(model, description)
                except ImageError as err:
                    print(f"[Agent:creative] {model} failed ({err.kind}): {_shown(err.message, 200)}")
                    if first is None:
                        first = err
                    else:
                        others.append(f"{model}: {err.kind}")
                    following = self.models[index + 1] if index + 1 < len(self.models) else None
                    if err.kind in TRY_NEXT_MODEL and following:
                        print(f"[Agent:creative] Falling back to {following}.")
                        continue
                    break
                now = time.monotonic()
                self._made.append(now)
                self._recent[description.lower()] = (now, path)
                print(f"[Agent:creative] Saved {path} ({now - started:.1f} s).")
                return f"SUCCESS: the image was created ({model}, {size}) and saved on this computer.\nPath: {path}"

            assert first is not None
            tail = f" (The fallback model failed too - {'; '.join(others)}.)" if others else ""
            return f"ERROR: {first.message}{tail}"

    def _already_made(self, description: str) -> str:
        """The answer for a description made moments ago (the file still there), or ""."""
        now = time.monotonic()
        for key, (when, _path) in list(self._recent.items()):
            if now - when > SAME_REQUEST_SECONDS:
                del self._recent[key]
        hit = self._recent.get(description.lower())
        if hit is None or not hit[1].exists():
            return ""
        print(f"[Agent:creative] Same description {now - hit[0]:.0f} s ago; not made again.")
        return (
            f"SUCCESS: this exact image was already created {now - hit[0]:.0f} seconds ago, so it was not made "
            f"again.\nPath: {hit[1]}"
        )

    def _hourly_wait(self) -> float:
        """Seconds until another image may be made under the hourly limit; 0 when it may be made now."""
        if self.hourly_limit <= 0:
            return 0.0
        now = time.monotonic()
        self._made = [when for when in self._made if now - when < HOUR_SECONDS]
        if len(self._made) < self.hourly_limit:
            return 0.0
        return HOUR_SECONDS - (now - self._made[0])

    def _make(self, model: str, description: str) -> tuple[Path, str]:
        """One model call, the image checked and saved. Raises ImageError."""
        # No output_format: PNG is the GPT image models' default, and older openai SDKs
        # (requirements.txt allows >=1.60) do not know the argument.
        request: dict[str, Any] = {"model": model, "prompt": description, "n": 1, "size": self.size}
        if not _legacy(model):
            request["quality"] = self.quality
        try:
            response = self._client.images.generate(**request)
        except Exception as exc:  # noqa: BLE001 - classified here
            raise classify(exc, model) from exc

        items = list(getattr(response, "data", None) or [])
        if not items:
            raise ImageError("bad_image", "OpenAI's answer held no image, so nothing was saved.")
        item = items[0]
        if getattr(item, "b64_json", None):
            data = _decode(item.b64_json)
        elif getattr(item, "url", None):
            data = self._download(item.url)
        else:
            raise ImageError("bad_image", "OpenAI's answer held neither image data nor a link, so nothing was saved.")

        extension = image_kind(data)
        if extension is None:
            raise ImageError("bad_image", "what OpenAI sent back is not a PNG, JPEG or WebP image, so nothing was saved.")
        try:
            path = self._save(data, extension)
        except OSError as exc:
            raise ImageError("save", f"the image was made but could not be saved in {self.artifacts_dir} ({exc}).") from exc
        return path, str(getattr(response, "size", None) or self.size)

    def _download(self, url: str) -> bytes:
        """The image behind `url`: https only, no redirects, a timeout and a size cap. Raises ImageError."""
        parts = urlparse(url)
        if parts.scheme != "https" or not parts.hostname:
            raise ImageError("download", "the image's link is not an https address, so it was not downloaded.")
        if self._http is None:
            if requests is None:
                raise ImageError("download", "the requests package is not installed, so the image could not be downloaded.")
            self._http = requests.Session()
        failed = "the image was made but could not be downloaded"
        try:
            with self._http.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT, allow_redirects=False) as response:
                if response.status_code != 200:
                    raise ImageError("download", f"{failed} (HTTP {response.status_code}).")
                declared = str(response.headers.get("Content-Length") or "")
                if declared.isdigit() and int(declared) > MAX_IMAGE_BYTES:
                    raise ImageError("download", f"{failed}: it is larger than {MAX_IMAGE_BYTES // 2**20} MB.")
                kind = str(response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if kind and not kind.startswith("image/") and kind not in ("application/octet-stream", "binary/octet-stream"):
                    raise ImageError("download", f"{failed}: the link served {kind}, not an image.")
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    total += len(chunk)
                    if total > MAX_IMAGE_BYTES:
                        raise ImageError("download", f"{failed}: it is larger than {MAX_IMAGE_BYTES // 2**20} MB.")
                    chunks.append(chunk)
                return b"".join(chunks)
        except ImageError:
            raise
        except Exception as exc:  # noqa: BLE001 - requests' timeouts, TLS and connection errors
            raise ImageError("download", f"{failed} ({type(exc).__name__}).") from exc

    def _save(self, data: bytes, extension: str) -> Path:
        """Writes `data` as artifacts/image_YYYYMMDD_HHMMSS.<ext> (_2, _3... if taken) and returns its absolute path."""
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        stamp = self._now().strftime("%Y%m%d_%H%M%S")
        for number in range(1, 100):
            path = self.artifacts_dir / (f"image_{stamp}.{extension}" if number == 1 else f"image_{stamp}_{number}.{extension}")
            try:
                handle = open(path, "xb")  # never overwrites
            except FileExistsError:
                continue
            try:
                with handle:
                    handle.write(data)
            except BaseException:
                path.unlink(missing_ok=True)
                raise
            return path.resolve()
        raise OSError(f"no free file name for image_{stamp}")

    # -- setup check (python -m agents.creative_agent --check) ----------------

    def check(self) -> list[str]:
        """Lines on the setup: key, each model as this key sees it, the folder. Makes no image."""
        lines = [f"Image generation: {self.describe()}"]
        if not self.available:
            return lines
        for model in self.models:
            try:
                self._client.models.retrieve(model)
                lines.append(f"  {model}: listed for this key (listed is not proof of access: OpenAI may still ask "
                             "for organization verification on the first image)")
            except Exception as exc:  # noqa: BLE001
                lines.append(f"  {model}: {classify(exc, model).message}")
        try:
            self.artifacts_dir.mkdir(parents=True, exist_ok=True)
            probe = self.artifacts_dir / ".write-check"
            probe.write_bytes(b"")
            probe.unlink()
            lines.append(f"  folder: {self.artifacts_dir} is writable")
        except OSError as exc:
            lines.append(f"  folder: {self.artifacts_dir} is NOT writable ({exc})")
        return lines


def web_url(path: Path | str) -> str:
    """The saved file's address for the web chat: /artifacts/<file name>."""
    return f"{ARTIFACTS_URL_PATH}/{Path(path).name}"


def markdown_image(path: Path | str, alt: str = "Generated Image") -> str:
    """The Markdown line the web chat draws as the picture: ![Generated Image](/artifacts/<file name>)."""
    return f"![{alt}]({web_url(path)})"


def _decode(b64: str) -> bytes:
    if len(b64) > MAX_IMAGE_BYTES * 4 // 3 + 4:
        raise ImageError("bad_image", f"the image is larger than {MAX_IMAGE_BYTES // 2**20} MB, so it was not saved.")
    try:
        return base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImageError("bad_image", "the image data OpenAI sent back does not decode, so nothing was saved.") from exc


def _models_from_env() -> list[str]:
    primary = _env("JARVIS_IMAGE_MODEL") or DEFAULT_MODEL
    fallback = _env("JARVIS_IMAGE_FALLBACK_MODEL") or DEFAULT_FALLBACK_MODEL
    return [primary] + ([] if fallback.lower() in ("none", "off", "0") else [fallback])


def _setting(name: str, given: Optional[str], default: str, valid: Callable[[str], bool]) -> str:
    value = (given if given is not None else _env(name)).strip().lower()
    if not value:
        return default
    if valid(value):
        return value
    print(f"[Agent:creative] {name}={value!r} is not valid; using {default}.")
    return default


def _hourly_limit_from_env() -> int:
    raw = _env("JARVIS_IMAGE_HOURLY_LIMIT")
    if not raw:
        return DEFAULT_HOURLY_LIMIT
    try:
        return max(0, int(raw))
    except ValueError:
        print(f"[Agent:creative] JARVIS_IMAGE_HOURLY_LIMIT={raw!r} is not a number; using {DEFAULT_HOURLY_LIMIT}.")
        return DEFAULT_HOURLY_LIMIT


def build() -> CreativeAgent:
    return CreativeAgent()


if __name__ == "__main__":
    agent = build()
    arguments = sys.argv[1:]
    if not arguments or arguments == ["--check"]:
        print("\n".join(agent.check()))
        raise SystemExit(0 if agent.available else 1)
    result = agent.generate_image(" ".join(arguments))
    print(result)
    raise SystemExit(0 if result.startswith("SUCCESS") else 1)
