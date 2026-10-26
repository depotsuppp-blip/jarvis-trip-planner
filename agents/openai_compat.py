"""
OpenAI and Groq as providers for the specialist agents.

Both speak the OpenAI chat-completions format - Groq's SDK is a copy of OpenAI's -
so one adapter serves both, in the same shape as llm_brain's AnthropicProvider and
GeminiProvider: the neutral transcript (llm_brain.Turn) in, one Turn out, and every
failure raised as a llm_brain.ProviderError whose `kind` the agent's routing reads.

    OpenAIProvider   OPENAI_API_KEY   (package: openai)
    GroqProvider     GROQ_API_KEY     (package: groq)

A provider without its key or package is simply unavailable, and the agent's route
list skips it (agents/base.py). Neither SDK is imported until a provider is built,
so the brain starts without them.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

import llm_budget
from llm_brain import MAX_TOKENS, ProviderError, ToolCall, Turn, _Provider, load_api_key
from tool_spec import ToolSpec

# A call that takes longer than this has failed, and the next route is tried. The
# SDKs' own retries (several, with back-off) are cut to one: a fallback exists for that.
REQUEST_TIMEOUT_SECONDS = 30.0


def openai_tool_schemas(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    """ToolSpecs -> the `tools` list of a chat-completions request."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema or {"type": "object", "properties": {}},
            },
        }
        for tool in tools
    ]


def to_messages(system: str, transcript: list[Turn], context: str = "") -> list[dict[str, Any]]:
    """
    Neutral transcript -> chat-completions `messages`.

    Call ids are renumbered by position. The transcript may hold calls another
    provider made (a fallback in the middle of a task), and Gemini's ids repeat
    from one step to the next; these only have to pair each call with its result.
    """
    prompt = f"{system}\n\n{context}" if context else system
    messages: list[dict[str, Any]] = [{"role": "system", "content": prompt}]
    for index, turn in enumerate(transcript):
        if turn.kind == "user":
            messages.append({"role": "user", "content": turn.text})
        elif turn.kind == "assistant":
            messages.append({"role": "assistant", "content": turn.text})
        elif turn.kind == "tool_calls":
            messages.append(
                {
                    "role": "assistant",
                    "content": turn.text or None,
                    "tool_calls": [
                        {
                            "id": _call_id(index, number),
                            "type": "function",
                            "function": {"name": call.name, "arguments": json.dumps(call.args, ensure_ascii=False)},
                        }
                        for number, call in enumerate(turn.calls)
                    ],
                }
            )
        elif turn.kind == "tool_results":
            # The results answer the calls of the turn just before them, in order.
            calls_turn = index - 1
            for number, result in enumerate(turn.results):
                content = result.content if not result.is_error else f"ERROR: {result.content}"
                messages.append({"role": "tool", "tool_call_id": _call_id(calls_turn, number), "content": content})
    return messages


def _call_id(turn_index: int, number: int) -> str:
    return f"call_{turn_index}_{number}"


def _arguments(raw: Any) -> dict[str, Any]:
    """A tool call's arguments: JSON text from the API. Anything unreadable becomes {} (the tool then says what is missing)."""
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


class OpenAICompatibleProvider(_Provider):
    """One chat-completions API (OpenAI or Groq). Subclasses say which SDK, key and name."""

    name = "openai-compatible"
    key_name = ""
    package = ""
    base_url_env = ""
    default_base_url = ""

    def __init__(self, model: str, max_tokens: int = MAX_TOKENS, api_key: Optional[str] = None, base_url: Optional[str] = None) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._client: Any = None
        self._sdk: Any = None
        self.unavailable_reason = ""

        key = api_key or load_api_key(self.key_name)
        if not key:
            self.unavailable_reason = f"no {self.key_name} configured"
            return
        try:
            self._sdk = self._import_sdk()
        except ImportError as exc:
            self.unavailable_reason = f"{self.package} package not installed ({exc})"
            return
        try:
            # Always passed: the SDKs read OPENAI_BASE_URL / GROQ_BASE_URL themselves and take
            # an empty one - .env.example has "OPENAI_BASE_URL=" - as the address, so every call fails.
            address = base_url or os.environ.get(self.base_url_env, "").strip() or self.default_base_url
            options: dict[str, Any] = {
                "api_key": key,
                "base_url": address,
                "timeout": REQUEST_TIMEOUT_SECONDS,
                "max_retries": 1,
            }
            self._client = self._make_client(self._sdk, options)
        except Exception as exc:  # noqa: BLE001 - a bad key format, a proxy setting, ...
            self.unavailable_reason = f"could not create the {self.name} client: {exc}"

    # -- per SDK -----------------------------------------------------------

    def _import_sdk(self) -> Any:
        raise NotImplementedError

    def _make_client(self, sdk: Any, options: dict[str, Any]) -> Any:
        raise NotImplementedError

    # -- _Provider -----------------------------------------------------------

    @property
    def available(self) -> bool:
        return self._client is not None

    @property
    def model(self) -> str:
        return self._model

    def complete(
        self,
        system: str,
        transcript: list[Turn],
        tools: list[ToolSpec],
        model: Optional[str] = None,
        effort: Optional[str] = None,
        context: str = "",
        json_output: bool = False,
    ) -> Turn:
        if self._client is None:
            raise ProviderError("no_key", f"My {self.name} model isn't configured.")

        request: dict[str, Any] = {
            "model": model or self._model,
            "messages": to_messages(system, transcript, context),
            "max_completion_tokens": self._max_tokens,
        }
        if tools:
            request["tools"] = openai_tool_schemas(tools)
        if json_output:
            # The system prompt says "JSON" (both APIs require the word for this mode).
            request["response_format"] = {"type": "json_object"}

        try:
            response = self._client.chat.completions.create(**request)
        except Exception as exc:  # noqa: BLE001 - classified immediately below
            raise self._classify(exc) from exc

        usage = getattr(response, "usage", None)
        if usage is not None:
            llm_budget.record_usage(getattr(usage, "prompt_tokens", 0) or 0, getattr(usage, "completion_tokens", 0) or 0)

        choices = getattr(response, "choices", None) or []
        if not choices:
            return Turn(kind="assistant", text="")
        message = choices[0].message
        text = (getattr(message, "content", None) or "").strip()
        calls = [
            ToolCall(id=getattr(call, "id", "") or f"call_{number}", name=call.function.name, args=_arguments(call.function.arguments))
            for number, call in enumerate(getattr(message, "tool_calls", None) or [])
            if getattr(call, "function", None) is not None and call.function.name
        ]
        if calls:
            return Turn(kind="tool_calls", text=text, calls=calls)
        if getattr(choices[0], "finish_reason", None) == "length" and text:
            text += " I had to cut that short."
        return Turn(kind="assistant", text=text)

    def describe_image(self, question: str, image_b64: str, media_type: str) -> str:  # pragma: no cover - agents never look at screens
        raise ProviderError("unknown", "This model is not used for images.")

    def _classify(self, exc: Exception) -> ProviderError:
        """SDK exceptions -> the kinds llm_brain uses (both SDKs raise the same classes, by name)."""
        print(f"[Agents] {self.name} call failed: {type(exc).__name__}: {str(exc)[:300]}")
        status = getattr(exc, "status_code", None)
        name = type(exc).__name__
        body = getattr(exc, "body", None)
        code = ""
        if isinstance(body, dict):
            error = body.get("error") if isinstance(body.get("error"), dict) else body
            code = str(error.get("code") or error.get("type") or "")
        text = f"{code} {exc}".lower()

        if status == 402 or "insufficient_quota" in text or "billing" in code.lower():
            return ProviderError("billing", f"My {self.name} account is out of credit.")
        if status == 429 or name == "RateLimitError":
            return ProviderError("rate_limit", f"{self.name} is rate limiting me.")
        if status == 401 or name == "AuthenticationError":
            return ProviderError("auth", f"My {self.name} key was rejected. Check {self.key_name} in the .env file.")
        if status == 403 or name == "PermissionDeniedError":
            return ProviderError("permission", f"My {self.name} key may not use the model {self._model}.")
        if status == 404 or name == "NotFoundError":
            return ProviderError("not_found", f"The model {self._model} wasn't found on {self.name}. It may have been retired.")
        if name in ("APITimeoutError", "APIConnectionError"):
            return ProviderError("connection", f"I couldn't reach {self.name}.")
        if isinstance(status, int) and status >= 500:
            return ProviderError("server", f"{self.name} is having trouble.")
        if status == 400 or name == "BadRequestError":
            return ProviderError("bad_request", f"{self.name} rejected the request. See the console.")
        return ProviderError("unknown", f"Something went wrong with {self.name}.")


class OpenAIProvider(OpenAICompatibleProvider):
    """GPT models via the OpenAI API."""

    name = "openai"
    key_name = "OPENAI_API_KEY"
    package = "openai"
    base_url_env = "OPENAI_BASE_URL"
    default_base_url = "https://api.openai.com/v1"

    def _import_sdk(self) -> Any:
        import openai

        return openai

    def _make_client(self, sdk: Any, options: dict[str, Any]) -> Any:
        return sdk.OpenAI(**options)


class GroqProvider(OpenAICompatibleProvider):
    """Open models (gpt-oss, Llama, Qwen) on Groq's hardware."""

    name = "groq"
    key_name = "GROQ_API_KEY"
    package = "groq"
    base_url_env = "GROQ_BASE_URL"
    default_base_url = "https://api.groq.com"

    def _import_sdk(self) -> Any:
        import groq

        return groq

    def _make_client(self, sdk: Any, options: dict[str, Any]) -> Any:
        return sdk.Groq(**options)
