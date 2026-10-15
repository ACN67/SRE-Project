"""Opt-in DeepSeek Chat Completions adapter.

Importing or constructing this class never reads environment variables or sends
requests. Inject a client for offline tests. The optional SDK is loaded only
when an explicitly enabled call has no injected client.
"""

from __future__ import annotations

import math
from typing import Any

from pydantic import ValidationError

from .protocol import LLMRequest, LLMResponse, TokenUsage, validate_response_for_request

DEEPSEEK_BASE_URL = "https://api.deepseek.com"


class DeepSeekAdapterError(RuntimeError):
    """Sanitized failure; never contains a key, prompt or provider error body."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _new_sdk_client(api_key: str, timeout_seconds: float) -> Any:
    try:
        from openai import OpenAI
    except ImportError:
        raise DeepSeekAdapterError("SDK_MISSING", 'Install the optional API dependency with pip install -e ".[api]"') from None
    return OpenAI(
        api_key=api_key,
        base_url=DEEPSEEK_BASE_URL,
        timeout=timeout_seconds,
        max_retries=0,
    )


def _provider_error(exc: Exception) -> DeepSeekAdapterError:
    """Classify without copying potentially sensitive vendor exception text."""
    name = type(exc).__name__
    status = getattr(exc, "status_code", None)
    if name in {"APITimeoutError", "TimeoutError"} or isinstance(exc, TimeoutError):
        return DeepSeekAdapterError("TIMEOUT", "DeepSeek request timed out")
    if name == "AuthenticationError" or status in {401, 403}:
        return DeepSeekAdapterError("AUTH", "DeepSeek authentication or authorization failed")
    if name == "RateLimitError" or status == 429:
        return DeepSeekAdapterError("RATE_LIMIT", "DeepSeek rate limit reached")
    if name in {"APIConnectionError", "ConnectionError"} or isinstance(exc, ConnectionError):
        return DeepSeekAdapterError("NETWORK", "DeepSeek connection failed")
    if isinstance(status, int):
        return DeepSeekAdapterError("API_STATUS", f"DeepSeek returned HTTP {status}")
    return DeepSeekAdapterError("PROVIDER_ERROR", "DeepSeek request failed")


class DeepSeekLLMAdapter:
    """One non-streaming Chat Completions attempt; retries belong to A-07."""

    def __init__(
        self,
        *,
        api_enabled: bool = False,
        client: Any | None = None,
        api_key: str | None = None,
        model_id: str | None = None,
        request_timeout_seconds: float = 120,
    ) -> None:
        if not isinstance(api_enabled, bool):
            raise TypeError("api_enabled must be a bool")
        if model_id is not None and (not isinstance(model_id, str) or not model_id.strip()):
            raise ValueError("model_id must be nonblank or null")
        if not isinstance(request_timeout_seconds, (int, float)) or isinstance(request_timeout_seconds, bool) or not math.isfinite(request_timeout_seconds) or request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        self._api_enabled = api_enabled
        self._client = client
        self._api_key = api_key
        self._model_id = model_id
        self._timeout_seconds = request_timeout_seconds

    def generate(self, request: LLMRequest) -> LLMResponse:
        if not self._api_enabled:
            raise DeepSeekAdapterError("API_DISABLED", "Real API calls require explicit api_enabled=True")
        if not isinstance(request, LLMRequest):
            raise DeepSeekAdapterError("INVALID_REQUEST", "Expected an LLMRequest")
        try:
            request = LLMRequest.model_validate(request.model_dump(mode="python"))
        except ValidationError:
            raise DeepSeekAdapterError("INVALID_REQUEST", "LLMRequest validation failed") from None
        if request.settings.thinking != "disabled":
            raise DeepSeekAdapterError("UNSUPPORTED_THINKING", "A-04 supports non-thinking mode only")
        if self._model_id is not None and request.model_id is not None and self._model_id != request.model_id:
            raise DeepSeekAdapterError("MODEL_MISMATCH", "Adapter and request model_id differ")
        model_id = request.model_id or self._model_id
        if model_id is None:
            raise DeepSeekAdapterError("MODEL_REQUIRED", "Explicit model_id is required for real calls")
        if self._client is None and (not isinstance(self._api_key, str) or not self._api_key.strip()):
            raise DeepSeekAdapterError("CREDENTIAL_REQUIRED", "Explicit API credential is required for real calls")

        if self._client is not None:
            client = self._client
        else:
            try:
                client = _new_sdk_client(self._api_key, self._timeout_seconds)
            except DeepSeekAdapterError:
                raise
            except Exception:
                raise DeepSeekAdapterError("CLIENT_INIT", "DeepSeek client initialization failed") from None
        kwargs: dict[str, Any] = {
            "model": model_id,
            "messages": [{"role": message.role, "content": message.content} for message in request.messages],
            "max_tokens": request.settings.max_output_tokens,
            "temperature": request.settings.temperature,
            "stream": False,
            "extra_body": {"thinking": {"type": "disabled"}},
        }
        if request.output_mode == "json":
            kwargs["response_format"] = {"type": "json_object"}
        try:
            raw = client.chat.completions.create(**kwargs)
        except Exception as exc:
            raise _provider_error(exc) from None
        return self._map_response(request, raw, model_id)

    @staticmethod
    def _map_response(request: LLMRequest, raw: Any, expected_model_id: str) -> LLMResponse:
        choices = getattr(raw, "choices", None)
        if not isinstance(choices, (list, tuple)) or not choices:
            raise DeepSeekAdapterError("INVALID_RESPONSE", "DeepSeek returned no choices")
        choice = choices[0]
        finish_reason = getattr(choice, "finish_reason", None)
        if finish_reason is not None and finish_reason != "stop":
            code = "TRUNCATED" if finish_reason == "length" else "INCOMPLETE"
            raise DeepSeekAdapterError(code, "DeepSeek completion did not finish normally")
        message = getattr(choice, "message", None)
        content = getattr(message, "content", None)
        if not isinstance(content, str) or not content.strip():
            raise DeepSeekAdapterError("INVALID_RESPONSE", "DeepSeek returned empty content")
        returned_model = getattr(raw, "model", None)
        if returned_model is not None and (not isinstance(returned_model, str) or not returned_model.strip()):
            raise DeepSeekAdapterError("INVALID_RESPONSE", "DeepSeek returned invalid model metadata")
        if returned_model is not None and returned_model != expected_model_id:
            raise DeepSeekAdapterError("MODEL_MISMATCH", "Response model_id differs from request")
        usage_raw = getattr(raw, "usage", None)
        usage = None
        if usage_raw is not None:
            try:
                usage = TokenUsage(
                    prompt_tokens=getattr(usage_raw, "prompt_tokens", None),
                    completion_tokens=getattr(usage_raw, "completion_tokens", None),
                )
            except ValidationError:
                raise DeepSeekAdapterError("INVALID_USAGE", "DeepSeek returned invalid token usage") from None
        try:
            response = LLMResponse(
                output_mode=request.output_mode,
                raw_output=content,
                finish_reason=finish_reason,
                usage=usage,
                model_id=returned_model,
            )
            validate_response_for_request(request, response)
        except (ValidationError, ValueError):
            raise DeepSeekAdapterError("INVALID_RESPONSE", "DeepSeek completion failed LLM contract validation") from None
        return response
