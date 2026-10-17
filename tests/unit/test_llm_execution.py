"""Offline execution policy tests with controlled adapters and thread events."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

import pytest

from dynamic_memory.llm import (
    DeepSeekAdapterError, DeepSeekLLMAdapter, GuardedLLMAdapter, LLMExecutionError,
    LLMRequest, LLMResponse, MockLLMAdapter, QA_PROMPT, MEMORY_EXTRACTION_PROMPT,
    STATE_REASONING_PROMPT, build_prompt_request, render_prompt, validate_structured_output,
)
from dynamic_memory.llm.structured_output import StructuredOutputError
from dynamic_memory.pipeline.settings import load_settings
from pydantic import BaseModel


TEMPLATES = (MEMORY_EXTRACTION_PROMPT, STATE_REASONING_PROMPT, QA_PROMPT)


def call(template=QA_PROMPT, settings=None):
    variables = {name: "synthetic-secret input" for name in template.required_variables}
    return build_prompt_request(render_prompt(template, variables), settings or load_settings().models)


def success(request, usage=None):
    if request.output_mode == "text":
        return LLMResponse(output_mode="text", raw_output="Synthetic answer", usage=usage)
    return LLMResponse(output_mode="json", raw_output='{"synthetic":true}', usage=usage)


class ScriptedAdapter:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def generate(self, request):
        self.calls.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome(request) if callable(outcome) else outcome


@pytest.mark.parametrize("template", TEMPLATES)
def test_three_tasks_use_same_executor_policy_and_mock(template):
    config = load_settings().models
    assert config.max_retries == 2 and config.max_concurrent_api_requests == 2
    request = call(template)
    result = GuardedLLMAdapter(MockLLMAdapter(), config, sleep=lambda _delay: None).execute(request)
    assert result.response.output_mode == request.output_mode
    assert result.summary.attempt_count == 1 and result.summary.retry_count == 0
    assert result.summary.input_tokens_estimated is None
    assert result.summary.input_budget_verified is False
    assert result.summary.prompt_tokens_known is None
    assert result.summary.unknown_usage_attempts == 1


@pytest.mark.parametrize("code", ["TIMEOUT", "NETWORK", "RATE_LIMIT"])
def test_retry_whitelist_stops_on_success_and_has_exponential_backoff(code):
    request = call()
    fake = ScriptedAdapter([
        DeepSeekAdapterError(code, "synthetic-secret vendor body"),
        DeepSeekAdapterError(code, "synthetic-secret vendor body"),
        success,
    ])
    delays = []
    result = GuardedLLMAdapter(fake, load_settings().models, sleep=delays.append).execute(request)
    assert len(fake.calls) == 3 and all(item is request for item in fake.calls)
    assert delays == [0.1, 0.2]
    assert result.summary.attempt_count == 3 and result.summary.retry_count == 2
    assert result.summary.failure_codes == (code, code)
    assert result.summary.unknown_usage_attempts == 3


@pytest.mark.parametrize("retries,expected_calls", [(0, 1), (1, 2), (2, 3)])
def test_retry_limits_preserve_last_error_and_attempt_count(retries, expected_calls):
    request = call()
    fake = ScriptedAdapter([DeepSeekAdapterError("TIMEOUT", "synthetic-secret")] * 3)
    config = load_settings().models.model_copy(update={"max_retries": retries})
    delays = []
    with pytest.raises(LLMExecutionError) as info:
        GuardedLLMAdapter(fake, config, sleep=delays.append).execute(request)
    assert info.value.code == "TIMEOUT"
    assert info.value.summary.attempt_count == expected_calls
    assert info.value.summary.retry_count == retries
    assert info.value.summary.unknown_usage_attempts == expected_calls
    assert len(delays) == retries and len(fake.calls) == expected_calls
    assert "synthetic-secret" not in str(info.value)


@pytest.mark.parametrize("code", [
    "AUTH", "API_STATUS", "API_DISABLED", "MODEL_REQUIRED", "MODEL_MISMATCH",
    "CREDENTIAL_REQUIRED", "SDK_MISSING", "INVALID_REQUEST", "UNSUPPORTED_THINKING",
    "INVALID_RESPONSE", "TRUNCATED", "PROVIDER_ERROR",
])
def test_non_retryable_provider_errors_fail_once(code):
    fake = ScriptedAdapter([DeepSeekAdapterError(code, "synthetic-secret response body")])
    with pytest.raises(LLMExecutionError) as info:
        GuardedLLMAdapter(fake, load_settings().models, sleep=lambda _delay: None).execute(call())
    assert info.value.code == code and info.value.summary.failure_codes == (code,)
    assert info.value.summary.attempt_count == len(fake.calls) == 1
    assert "synthetic-secret" not in str(info.value)


def test_unknown_exceptions_and_disabled_provider_fail_once():
    for adapter, code in (
        (ScriptedAdapter([RuntimeError("synthetic-secret provider body")]), "ADAPTER_ERROR"),
        (DeepSeekLLMAdapter(), "API_DISABLED"),
    ):
        with pytest.raises(LLMExecutionError) as info:
            GuardedLLMAdapter(adapter, load_settings().models).execute(call())
        assert info.value.code == code and info.value.summary.attempt_count == 1
        assert "synthetic-secret" not in str(info.value)


@pytest.mark.parametrize("template", TEMPLATES)
def test_task_limits_and_estimator_preflight(template):
    request = call(template)
    fake = ScriptedAdapter([success])
    seen = []
    def estimate(messages):
        seen.append(messages)
        return 12
    result = GuardedLLMAdapter(fake, load_settings().models, token_estimator=estimate, strict_budget=True).execute(request)
    assert seen == [request.messages] and fake.calls == [request]
    assert result.summary.input_tokens_estimated == 12 and result.summary.input_budget_verified is True
    assert request.settings.max_input_tokens == getattr(load_settings().models, {"memory_extraction":"extraction", "state_reasoning":"reasoning", "qa":"qa"}[request.task] + "_max_input_tokens")


@pytest.mark.parametrize("change", [
    {"max_input_tokens": 99999}, {"max_output_tokens": 99999},
])
def test_configured_request_budget_checked_before_provider(change):
    request = call()
    request = request.model_copy(update={"settings": request.settings.model_copy(update=change)})
    fake = ScriptedAdapter([success])
    with pytest.raises(LLMExecutionError) as info:
        GuardedLLMAdapter(fake, load_settings().models).execute(request)
    assert info.value.code == "REQUEST_BUDGET_EXCEEDED"
    assert info.value.summary.attempt_count == 0 and fake.calls == []


@pytest.mark.parametrize("estimate,strict,code", [
    (32769, False, "INPUT_BUDGET_EXCEEDED"),
    (-1, False, "INVALID_ESTIMATE"),
    (True, False, "INVALID_ESTIMATE"),
    (1.5, False, "INVALID_ESTIMATE"),
    (None, True, "INPUT_BUDGET_UNKNOWN"),
])
def test_unknown_invalid_or_excessive_estimate_fails_before_call(estimate, strict, code):
    fake = ScriptedAdapter([success])
    with pytest.raises(LLMExecutionError) as info:
        GuardedLLMAdapter(fake, load_settings().models, token_estimator=lambda _messages: estimate, strict_budget=strict).execute(call())
    assert info.value.code == code and info.value.summary.attempt_count == 0 and fake.calls == []


def test_unknown_input_estimate_is_explicit_and_strict_mode_closes():
    request = call()
    loose = GuardedLLMAdapter(ScriptedAdapter([success]), load_settings().models).execute(request)
    assert loose.summary.input_tokens_estimated is None and loose.summary.input_budget_verified is False
    with pytest.raises(LLMExecutionError) as info:
        GuardedLLMAdapter(ScriptedAdapter([success]), load_settings().models, strict_budget=True).execute(request)
    assert info.value.code == "INPUT_BUDGET_UNKNOWN" and info.value.summary.attempt_count == 0


@pytest.mark.parametrize("usage,code", [
    ({"prompt_tokens": 12, "completion_tokens": 3}, None),
    ({"prompt_tokens": 32769, "completion_tokens": 3}, "INPUT_BUDGET_EXCEEDED"),
    ({"prompt_tokens": 12, "completion_tokens": 257}, "OUTPUT_BUDGET_EXCEEDED"),
])
def test_provider_usage_is_known_and_overages_are_reported(usage, code):
    fake = ScriptedAdapter([lambda req: success(req, usage=usage)])
    executor = GuardedLLMAdapter(fake, load_settings().models)
    if code:
        with pytest.raises(LLMExecutionError) as info:
            executor.execute(call())
        assert info.value.code == code
        summary = info.value.summary
    else:
        summary = executor.execute(call()).summary
    assert summary.prompt_tokens_known == usage["prompt_tokens"]
    assert summary.completion_tokens_known == usage["completion_tokens"]
    assert summary.unknown_usage_attempts == 0 and summary.attempt_count == 1


def test_earlier_failed_attempt_usage_remains_unknown_after_success():
    fake = ScriptedAdapter([DeepSeekAdapterError("NETWORK", "synthetic-secret"), lambda req: success(req, usage={"prompt_tokens": 12, "completion_tokens": 3})])
    summary = GuardedLLMAdapter(fake, load_settings().models, sleep=lambda _delay: None).execute(call()).summary
    assert summary.attempt_count == 2 and summary.retry_count == 1
    assert summary.unknown_usage_attempts == 1
    assert (summary.prompt_tokens_known, summary.completion_tokens_known) == (12, 3)


def test_incomplete_response_preserves_reported_usage():
    fake = ScriptedAdapter([lambda _req: LLMResponse(
        output_mode="text", raw_output="Synthetic partial answer", finish_reason="content_filter",
        usage={"prompt_tokens": 12, "completion_tokens": 3},
    )])
    with pytest.raises(LLMExecutionError) as info:
        GuardedLLMAdapter(fake, load_settings().models).execute(call())
    assert info.value.code == "RESPONSE_INCOMPLETE"
    assert info.value.summary.attempt_count == 1
    assert info.value.summary.unknown_usage_attempts == 0
    assert (info.value.summary.prompt_tokens_known, info.value.summary.completion_tokens_known) == (12, 3)


@pytest.mark.parametrize("response,code", [
    (lambda req: LLMResponse(output_mode="json", raw_output='{"synthetic":true}'), "INVALID_RESPONSE"),
    (lambda req: LLMResponse(output_mode="text", raw_output="Synthetic", finish_reason="content_filter"), "RESPONSE_INCOMPLETE"),
    (lambda req: LLMResponse(output_mode="text", raw_output="Synthetic", model_id="wrong-model"), "INVALID_RESPONSE"),
])
def test_response_contract_failures_do_not_retry(response, code):
    request = call()
    if code == "INVALID_RESPONSE" and response(request).model_id == "wrong-model":
        request = request.model_copy(update={"model_id": "expected-model"})
    fake = ScriptedAdapter([response])
    with pytest.raises(LLMExecutionError) as info:
        GuardedLLMAdapter(fake, load_settings().models).execute(request)
    assert info.value.code == code and len(fake.calls) == 1


def test_schema_validation_remains_an_explicit_post_step():
    class BusinessSchema(BaseModel):
        required: str
    fake = ScriptedAdapter([lambda req: LLMResponse(output_mode="json", raw_output='{"synthetic":true}')])
    executor = GuardedLLMAdapter(fake, load_settings().models)
    request = call(MEMORY_EXTRACTION_PROMPT)
    result = executor.execute(request)
    with pytest.raises(StructuredOutputError) as info:
        validate_structured_output(request, result.response, BusinessSchema)
    assert info.value.code == "SCHEMA_INVALID" and len(fake.calls) == 1


def test_two_in_flight_calls_and_waiting_third_are_bounded():
    entered = Event()
    release = Event()
    lock = Lock()
    state = {"active": 0, "max": 0, "entered": 0}

    class BlockingAdapter:
        def generate(self, request):
            with lock:
                state["active"] += 1
                state["entered"] += 1
                state["max"] = max(state["max"], state["active"])
                if state["entered"] == 2:
                    entered.set()
            try:
                assert release.wait(3)
                return success(request)
            finally:
                with lock:
                    state["active"] -= 1

    executor = GuardedLLMAdapter(BlockingAdapter(), load_settings().models)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(executor.execute, call()) for _ in range(3)]
        try:
            assert entered.wait(3)
            with lock:
                assert state["entered"] == 2 and state["max"] == 2
        finally:
            release.set()
        results = [future.result(timeout=3) for future in futures]
    assert state["entered"] == 3 and state["active"] == 0 and state["max"] == 2
    assert all(item.summary.attempt_count == 1 for item in results)


def test_retry_sleep_does_not_hold_provider_slot():
    sleeping = Event()
    resume = Event()
    lock = Lock()
    attempts = []

    class Adapter:
        def generate(self, request):
            with lock:
                attempts.append(request)
                count = len(attempts)
            if count == 1:
                raise DeepSeekAdapterError("TIMEOUT", "synthetic-secret")
            return success(request)

    def sleep(_delay):
        sleeping.set()
        assert resume.wait(3)

    config = load_settings().models.model_copy(update={"max_concurrent_api_requests": 1})
    executor = GuardedLLMAdapter(Adapter(), config, sleep=sleep)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(executor.execute, call())
        try:
            assert sleeping.wait(3)
            second = pool.submit(executor.execute, call())
            assert second.result(timeout=3).summary.attempt_count == 1
        finally:
            resume.set()
        assert first.result(timeout=3).summary.attempt_count == 2
    assert len(attempts) == 3


def test_error_releases_permit_and_wait_timeout_is_observable():
    entered = Event()
    release = Event()

    class BlockingAdapter:
        def generate(self, request):
            entered.set()
            assert release.wait(3)
            raise DeepSeekAdapterError("AUTH", "synthetic-secret")

    config = load_settings().models.model_copy(update={"max_concurrent_api_requests": 1, "max_retries": 0})
    executor = GuardedLLMAdapter(BlockingAdapter(), config, acquire_timeout_seconds=0)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(executor.execute, call())
        try:
            assert entered.wait(3)
            with pytest.raises(LLMExecutionError) as info:
                executor.execute(call())
            assert info.value.code == "CONCURRENCY_LIMIT" and info.value.summary.attempt_count == 0
        finally:
            release.set()
        with pytest.raises(LLMExecutionError) as info:
            first.result(timeout=3)
        assert info.value.code == "AUTH"
    with pytest.raises(LLMExecutionError) as info:
        executor.execute(call())
    assert info.value.code == "AUTH" and info.value.summary.attempt_count == 1


def test_bad_settings_invalid_request_and_nesting_fail_before_provider():
    fake = ScriptedAdapter([success])
    config = load_settings().models
    with pytest.raises(ValueError, match="models_settings validation"):
        GuardedLLMAdapter(fake, config.model_copy(update={"max_retries": "synthetic-secret"}))
    executor = GuardedLLMAdapter(fake, config)
    with pytest.raises(ValueError, match="nested"):
        GuardedLLMAdapter(executor, config)
    with pytest.raises(LLMExecutionError) as info:
        executor.execute(call().model_copy(update={"task": "invalid"}))
    assert info.value.code == "INVALID_REQUEST" and info.value.summary.attempt_count == 0
    assert fake.calls == []
