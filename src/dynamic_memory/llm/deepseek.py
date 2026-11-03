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
_SAFE_REASONS = frozenset({
    "UNKNOWN", "HTTP_OTHER", "INVALID_REQUEST", "AUTHENTICATION_FAILED",
    "INSUFFICIENT_BALANCE", "FORBIDDEN", "INVALID_PARAMETERS", "RATE_LIMIT",
    "SERVER_ERROR", "OVERLOADED", "TIMEOUT", "NETWORK",
})


class DeepSeekAdapterError(RuntimeError):
    """Sanitized failure; never contains a key, prompt or provider error body."""

    def __init__(self, code: str, message: str, *,
                 safe_status_code: int | None = None, safe_reason: str = "UNKNOWN",
                 usage: TokenUsage | None = None, finish_reason: str | None = None,
                 response_model_id: str | None = None,
                 http_response_received: bool = False) -> None:
        self.code = code
        self.safe_status_code = (safe_status_code if type(safe_status_code) is int
                                 and 100 <= safe_status_code <= 599 else None)
        self.safe_reason = (safe_reason if isinstance(safe_reason, str)
                            and safe_reason in _SAFE_REASONS else "UNKNOWN")
        self.usage = usage if isinstance(usage, TokenUsage) else None
        self.finish_reason = finish_reason if finish_reason in {
            "stop", "length", "content_filter", "insufficient_system_resource", "aborted"
        } else None
        self.response_model_id = response_model_id if response_model_id == "deepseek-flash" else None
        self.http_response_received = http_response_received is True
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
    try:
        status = getattr(exc, "status_code", None)
    except Exception:
        status = None
    if status is None:
        try:
            status = getattr(getattr(exc, "response", None), "status_code", None)
        except Exception:
            status = None
    if type(status) is not int or not 100 <= status <= 599:
        status = None
    reason = {400: "INVALID_REQUEST", 401: "AUTHENTICATION_FAILED",
              402: "INSUFFICIENT_BALANCE", 403: "FORBIDDEN",
              422: "INVALID_PARAMETERS", 429: "RATE_LIMIT",
              500: "SERVER_ERROR", 503: "OVERLOADED"}.get(status,
              "HTTP_OTHER" if status is not None else "UNKNOWN")
    if name in {"APITimeoutError", "TimeoutError"} or isinstance(exc, TimeoutError):
        return DeepSeekAdapterError("TIMEOUT", "DeepSeek request timed out",
                                    safe_status_code=status,
                                    safe_reason=reason if status is not None else "TIMEOUT")
    if name == "AuthenticationError" or status in {401, 403}:
        return DeepSeekAdapterError("AUTH", "DeepSeek authentication or authorization failed",
                                    safe_status_code=status,
                                    safe_reason=reason if status is not None else "AUTHENTICATION_FAILED")
    if name == "RateLimitError" or status == 429:
        return DeepSeekAdapterError("RATE_LIMIT", "DeepSeek rate limit reached",
                                    safe_status_code=status,
                                    safe_reason=reason if status is not None else "RATE_LIMIT")
    if name in {"APIConnectionError", "ConnectionError"} or isinstance(exc, ConnectionError):
        return DeepSeekAdapterError("NETWORK", "DeepSeek connection failed",
                                    safe_status_code=status,
                                    safe_reason=reason if status is not None else "NETWORK")
    if status is not None:
        return DeepSeekAdapterError("API_STATUS", "DeepSeek returned an HTTP error",
                                    safe_status_code=status, safe_reason=reason)
    return DeepSeekAdapterError("PROVIDER_ERROR", "DeepSeek request failed",
                                safe_reason=reason)


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
        if self._model_id is not None and request.model_id is not None and self._model_id != request.model_id:
            raise DeepSeekAdapterError("MODEL_MISMATCH", "Adapter and request model_id differ")
        model_id = request.model_id or self._model_id
        if model_id is None:
            raise DeepSeekAdapterError("MODEL_REQUIRED", "Explicit model_id is required for real calls")
        if request.settings.thinking == "enabled" and request.settings.reasoning_effort is None:
            raise DeepSeekAdapterError("EFFORT_REQUIRED", "Explicit thinking effort is required for real calls")
        if request.settings.thinking == "enabled" and model_id != "deepseek-flash":
            raise DeepSeekAdapterError("UNSUPPORTED_MODEL", "Thinking pilot supports deepseek-flash only")
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
            "stream": False,
            "extra_body": {"thinking": {"type": request.settings.thinking}},
        }
        if not request.settings.omit_max_tokens:
            kwargs["max_tokens"] = request.settings.max_output_tokens
        if request.settings.thinking == "enabled":
            kwargs["reasoning_effort"] = request.settings.reasoning_effort
        else:
            kwargs["temperature"] = request.settings.temperature
        if request.output_mode == "json":
            kwargs["response_format"] = {"type": "json_object"}
        try:
            raw = client.chat.completions.create(**kwargs)
        except Exception as exc:
            raise _provider_error(exc) from None
        return self._map_response(request, raw, model_id)

    @staticmethod
    def _map_response(request: LLMRequest, raw: Any, expected_model_id: str) -> LLMResponse:
        returned_model = getattr(raw, "model", None)
        usage_raw = getattr(raw, "usage", None)
        usage = None
        if usage_raw is not None:
            try:
                details = getattr(usage_raw, "completion_tokens_details", None)
                usage = TokenUsage(
                    prompt_tokens=getattr(usage_raw, "prompt_tokens", None),
                    completion_tokens=getattr(usage_raw, "completion_tokens", None),
                    reasoning_tokens=getattr(details, "reasoning_tokens", None),
                    prompt_cache_hit_tokens=getattr(usage_raw, "prompt_cache_hit_tokens", None),
                    prompt_cache_miss_tokens=getattr(usage_raw, "prompt_cache_miss_tokens", None),
                )
            except ValidationError:
                raise DeepSeekAdapterError("INVALID_USAGE", "DeepSeek returned invalid token usage",
                                           http_response_received=True) from None
        choices = getattr(raw, "choices", None)
        choice = choices[0] if isinstance(choices, (list, tuple)) and choices else None
        finish_reason = getattr(choice, "finish_reason", None)
        metadata = {"usage": usage, "finish_reason": finish_reason,
                    "response_model_id": returned_model,
                    "http_response_received": True}
        if choice is None:
            raise DeepSeekAdapterError("INVALID_RESPONSE", "DeepSeek returned no choices", **metadata)
        if returned_model is not None and (not isinstance(returned_model, str) or not returned_model.strip()):
            raise DeepSeekAdapterError("INVALID_RESPONSE", "DeepSeek returned invalid model metadata", **metadata)
        if returned_model is not None and returned_model != expected_model_id:
            raise DeepSeekAdapterError("MODEL_MISMATCH", "Response model_id differs from request", **metadata)
        if request.settings.thinking == "enabled" and finish_reason is None:
            raise DeepSeekAdapterError("INCOMPLETE", "DeepSeek thinking completion has no finish status", **metadata)
        if finish_reason is not None and finish_reason != "stop":
            code = "TRUNCATED" if finish_reason == "length" else "INCOMPLETE"
            raise DeepSeekAdapterError(code, "DeepSeek completion did not finish normally", **metadata)
        message = getattr(choice, "message", None)
        content = getattr(message, "content", None)
        if not isinstance(content, str) or not content.strip():
            if request.output_mode == "json" and (content is None or isinstance(content, str)):
                raise DeepSeekAdapterError("EMPTY_JSON", "DeepSeek returned empty JSON content", **metadata)
            if request.settings.thinking == "enabled" and (content is None or isinstance(content, str)):
                raise DeepSeekAdapterError("EMPTY_FINAL", "DeepSeek returned no final answer", **metadata)
            raise DeepSeekAdapterError("INVALID_RESPONSE", "DeepSeek returned empty or invalid content", **metadata)
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
            raise DeepSeekAdapterError("INVALID_RESPONSE", "DeepSeek completion failed LLM contract validation",
                                       **metadata) from None
        return response
