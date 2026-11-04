"""Shared synchronous LLM execution policy; no provider or persistence side effects."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import BoundedSemaphore

from pydantic import ValidationError

from dynamic_memory.pipeline.settings import ModelsSettings

from .deepseek import DeepSeekAdapterError
from .protocol import LLMAdapter, LLMMessage, LLMRequest, LLMResponse, validate_response_for_request

TokenEstimator = Callable[[tuple[LLMMessage, ...]], int | None]
_RETRYABLE = frozenset({"TIMEOUT", "NETWORK", "RATE_LIMIT", "EMPTY_JSON"})
_PROVIDER_CODES = _RETRYABLE | frozenset({
    "API_STATUS", "API_DISABLED", "AUTH", "MODEL_REQUIRED", "MODEL_MISMATCH",
    "CREDENTIAL_REQUIRED", "SDK_MISSING", "INVALID_REQUEST", "UNSUPPORTED_THINKING",
    "INVALID_RESPONSE", "INVALID_USAGE", "CLIENT_INIT", "PROVIDER_ERROR",
    "EMPTY_FINAL", "UNSUPPORTED_MODEL", "EFFORT_REQUIRED",
    "TRUNCATED", "INCOMPLETE",
})


@dataclass(frozen=True, slots=True)
class LLMCallSummary:
    attempt_count: int
    retry_count: int
    failure_codes: tuple[str, ...]
    input_tokens_estimated: int | None
    input_budget_verified: bool
    prompt_tokens_known: int | None
    completion_tokens_known: int | None
    unknown_usage_attempts: int
    safe_status_code: int | None = None
    safe_reason: str = "UNKNOWN"
    reasoning_tokens_known: int | None = None
    final_tokens_known: int | None = None
    finish_reason: str | None = None
    response_model_id: str | None = None
    http_response_received: bool = False
    prompt_cache_hit_tokens_known: int | None = None
    prompt_cache_miss_tokens_known: int | None = None


@dataclass(frozen=True, slots=True)
class LLMCallResult:
    response: LLMResponse = field(repr=False)
    summary: LLMCallSummary


class LLMExecutionError(RuntimeError):
    """Sanitized execution failure with all attempted work summarized."""

    def __init__(self, code: str, summary: LLMCallSummary) -> None:
        self.code = code
        self.summary = summary
        super().__init__(f"{code}: LLM execution failed")


class GuardedLLMAdapter:
    """One shared instance bounds in-flight provider calls across its callers.

    The injected adapter owns its per-call timeout. This class bounds waiting
    for a semaphore permit; it cannot cancel a blocking synchronous adapter.
    """

    def __init__(
        self,
        adapter: LLMAdapter,
        models_settings: ModelsSettings,
        *,
        token_estimator: TokenEstimator | None = None,
        strict_budget: bool = False,
        sleep: Callable[[float], None] = time.sleep,
        backoff_initial_seconds: float = 0.1,
        backoff_max_seconds: float = 1.0,
        acquire_timeout_seconds: float | None = None,
    ) -> None:
        if not callable(getattr(adapter, "generate", None)):
            raise ValueError("adapter must provide generate(request)")
        if isinstance(adapter, GuardedLLMAdapter):
            raise ValueError("nested guarded adapters would multiply retries")
        if not isinstance(models_settings, ModelsSettings):
            raise ValueError("models_settings must be ModelsSettings")
        try:
            settings = ModelsSettings.model_validate(models_settings.model_dump(mode="python", warnings=False))
        except (ValidationError, ValueError, TypeError):
            raise ValueError("models_settings validation failed") from None
        if token_estimator is not None and not callable(token_estimator):
            raise ValueError("token_estimator must be callable or null")
        if not isinstance(strict_budget, bool) or not callable(sleep):
            raise ValueError("strict_budget must be bool and sleep must be callable")
        for value in (backoff_initial_seconds, backoff_max_seconds):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError("backoff delays must be finite and nonnegative")
        if backoff_max_seconds < backoff_initial_seconds:
            raise ValueError("backoff cap must be at least the initial delay")
        wait = settings.request_timeout_seconds if acquire_timeout_seconds is None else acquire_timeout_seconds
        if isinstance(wait, bool) or not isinstance(wait, (int, float)) or not math.isfinite(wait) or wait < 0:
            raise ValueError("acquire timeout must be finite and nonnegative")
        self._adapter = adapter
        self._settings = settings
        self._estimator = token_estimator
        self._strict_budget = strict_budget
        self._sleep = sleep
        self._backoff_initial = backoff_initial_seconds
        self._backoff_max = backoff_max_seconds
        self._acquire_timeout = wait
        self._limiter = BoundedSemaphore(settings.max_concurrent_api_requests)

    def generate(self, request: LLMRequest) -> LLMResponse:
        """LLMAdapter-compatible call; execution errors retain a summary."""
        return self.execute(request).response

    def execute(self, request: LLMRequest) -> LLMCallResult:
        attempts = 0
        failures: list[str] = []
        estimated: int | None = None
        verified = False
        prompt_known: int | None = None
        completion_known: int | None = None
        reasoning_known: int | None = None
        unknown_usage = 0
        safe_status_code: int | None = None
        safe_reason = "UNKNOWN"
        finish_reason: str | None = None
        response_model_id: str | None = None
        http_response_received = False
        cache_hit_known: int | None = None
        cache_miss_known: int | None = None

        def summary() -> LLMCallSummary:
            return LLMCallSummary(
                attempt_count=attempts, retry_count=max(0, attempts - 1),
                failure_codes=tuple(failures), input_tokens_estimated=estimated,
                input_budget_verified=verified, prompt_tokens_known=prompt_known,
                completion_tokens_known=completion_known, unknown_usage_attempts=unknown_usage,
                safe_status_code=safe_status_code, safe_reason=safe_reason,
                reasoning_tokens_known=reasoning_known,
                final_tokens_known=(completion_known - reasoning_known
                                    if completion_known is not None and reasoning_known is not None else None),
                finish_reason=finish_reason, response_model_id=response_model_id,
                http_response_received=http_response_received,
                prompt_cache_hit_tokens_known=cache_hit_known,
                prompt_cache_miss_tokens_known=cache_miss_known,
            )

        def fail(code: str) -> None:
            raise LLMExecutionError(code, summary()) from None

        if not isinstance(request, LLMRequest):
            fail("INVALID_REQUEST")
        try:
            LLMRequest.model_validate(request.model_dump(mode="python", warnings=False))
        except (ValidationError, ValueError, TypeError):
            fail("INVALID_REQUEST")
        prefix = {"memory_extraction": "extraction", "state_reasoning": "reasoning", "qa": "qa"}[request.task]
        allowed_input = getattr(self._settings, f"{prefix}_max_input_tokens")
        allowed_output = (65536 if request.settings.omit_max_tokens else
                          getattr(self._settings, f"{prefix}_max_output_tokens"))
        if request.settings.max_input_tokens > allowed_input or request.settings.max_output_tokens > allowed_output:
            fail("REQUEST_BUDGET_EXCEEDED")
        if self._estimator is not None:
            try:
                estimated = self._estimator(request.messages)
            except Exception:
                fail("INVALID_ESTIMATE")
            if estimated is not None and (isinstance(estimated, bool) or not isinstance(estimated, int) or estimated < 0):
                fail("INVALID_ESTIMATE")
        if estimated is None and self._strict_budget:
            fail("INPUT_BUDGET_UNKNOWN")
        if estimated is not None:
            if estimated > request.settings.max_input_tokens:
                fail("INPUT_BUDGET_EXCEEDED")
            verified = True  # Verified against the injected estimate, not exact provider tokens.

        for index in range(self._settings.max_retries + 1):
            if not self._limiter.acquire(timeout=self._acquire_timeout):
                fail("CONCURRENCY_LIMIT")
            response = None
            provider_code = None
            try:
                attempts += 1
                response = self._adapter.generate(request)
            except DeepSeekAdapterError as exc:
                provider_code = exc.code if isinstance(exc.code, str) and exc.code in _PROVIDER_CODES else "PROVIDER_ERROR"
                safe_status_code = exc.safe_status_code
                safe_reason = exc.safe_reason
                finish_reason = exc.finish_reason
                response_model_id = exc.response_model_id
                http_response_received = exc.http_response_received
                if exc.usage is not None:
                    prompt_known = exc.usage.prompt_tokens
                    completion_known = exc.usage.completion_tokens
                    reasoning_known = exc.usage.reasoning_tokens
                    cache_hit_known = exc.usage.prompt_cache_hit_tokens
                    cache_miss_known = exc.usage.prompt_cache_miss_tokens
            except Exception:
                provider_code = "ADAPTER_ERROR"
            finally:
                self._limiter.release()

            if provider_code is not None:
                if prompt_known is None or completion_known is None:
                    unknown_usage += 1
                failures.append(provider_code)
                if (provider_code not in _RETRYABLE or
                    (provider_code == "EMPTY_JSON" and request.output_mode != "json") or
                    index == self._settings.max_retries):
                    fail(provider_code)
            else:
                if not isinstance(response, LLMResponse):
                    unknown_usage += 1
                    failures.append("INVALID_RESPONSE")
                    fail("INVALID_RESPONSE")
                try:
                    checked = LLMResponse.model_validate(response.model_dump(mode="python", warnings=False))
                except (ValidationError, ValueError, TypeError):
                    unknown_usage += 1
                    failures.append("INVALID_RESPONSE")
                    fail("INVALID_RESPONSE")
                if checked.usage is None:
                    unknown_usage += 1
                else:
                    prompt_known = checked.usage.prompt_tokens
                    completion_known = checked.usage.completion_tokens
                    reasoning_known = checked.usage.reasoning_tokens
                    cache_hit_known = checked.usage.prompt_cache_hit_tokens
                    cache_miss_known = checked.usage.prompt_cache_miss_tokens
                finish_reason = checked.finish_reason
                response_model_id = checked.model_id if checked.model_id == "deepseek-flash" else None
                http_response_received = request.model_id == "deepseek-flash"
                try:
                    validate_response_for_request(request, checked)
                except ValueError:
                    failures.append("INVALID_RESPONSE")
                    fail("INVALID_RESPONSE")
                if checked.finish_reason is not None and checked.finish_reason != "stop":
                    failures.append("RESPONSE_INCOMPLETE")
                    fail("RESPONSE_INCOMPLETE")
                if checked.usage is not None:
                    if prompt_known > request.settings.max_input_tokens:
                        failures.append("INPUT_BUDGET_EXCEEDED")
                        fail("INPUT_BUDGET_EXCEEDED")
                    if completion_known > request.settings.max_output_tokens:
                        failures.append("OUTPUT_BUDGET_EXCEEDED")
                        fail("OUTPUT_BUDGET_EXCEEDED")
                return LLMCallResult(response=response, summary=summary())

            delay = min(self._backoff_initial * (2 ** index), self._backoff_max)
            try:
                self._sleep(delay)
            except Exception:
                fail("BACKOFF_FAILED")

        raise AssertionError("retry loop must terminate")
