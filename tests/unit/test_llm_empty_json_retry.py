"""G-01: one shared retry budget for retryable provider failures and blank JSON."""
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from dynamic_memory.llm import (
    DeepSeekLLMAdapter, GuardedLLMAdapter, LLMExecutionError, LLMMessage,
    LLMRequest, GenerationSettings, validate_structured_output,
)
from dynamic_memory.llm.structured_output import StructuredOutputError
from dynamic_memory.pipeline.settings import load_settings


MODEL = "synthetic-unit-model"


def req(task="memory_extraction"):
    cfg = load_settings().models
    prefix = {"memory_extraction": "extraction", "state_reasoning": "reasoning", "qa": "qa"}[task]
    return LLMRequest(
        messages=(LLMMessage(role="system", content="Return JSON on JSON tasks."),
                  LLMMessage(role="user", content="synthetic test input")),
        task=task,
        output_mode="text" if task == "qa" else "json",
        settings=GenerationSettings(
            max_input_tokens=getattr(cfg, f"{prefix}_max_input_tokens"),
            max_output_tokens=getattr(cfg, f"{prefix}_max_output_tokens"),
            temperature=0, thinking="disabled",
        ),
        model_id=MODEL,
    )


def reply(content, finish_reason="stop", usage=None):
    choice = SimpleNamespace(
        message=SimpleNamespace(content=content),
        finish_reason=finish_reason,
    )
    return SimpleNamespace(choices=[choice], model=MODEL, usage=usage)


class Completions:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def guard(outcomes, max_retries=2):
    completions = Completions(outcomes)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    provider = DeepSeekLLMAdapter(api_enabled=True, client=client, model_id=MODEL)
    cfg = load_settings().models.model_copy(update={"max_retries": max_retries})
    pauses = []
    return GuardedLLMAdapter(provider, cfg, sleep=pauses.append), completions, pauses


@pytest.mark.parametrize("blank", [None, "", " \n\t "])
@pytest.mark.parametrize("task", ["memory_extraction", "state_reasoning"])
def test_blank_json_retries_once_before_valid_response(blank, task):
    executor, provider, pauses = guard([reply(blank), reply('{"items": []}')])
    result = executor.execute(req(task))
    assert result.response.parsed_json == {"items": []}
    assert len(provider.calls) == 2
    assert pauses == [0.1]
    assert result.summary.failure_codes == ("EMPTY_JSON",)
    assert result.summary.attempt_count == 2
    assert result.summary.retry_count == 1
    assert result.summary.unknown_usage_attempts == 2


def test_mixed_provider_failures_share_single_max_three_attempt_budget():
    executor, provider, pauses = guard([
        TimeoutError("synthetic-secret raw vendor exception"),
        reply(" \t "),
        reply('{"items": []}', usage=SimpleNamespace(prompt_tokens=12, completion_tokens=2)),
    ])
    result = executor.execute(req())
    assert result.summary.attempt_count == 3
    assert result.summary.retry_count == 2
    assert result.summary.failure_codes == ("TIMEOUT", "EMPTY_JSON")
    assert result.summary.unknown_usage_attempts == 2
    assert result.summary.prompt_tokens_known == 12
    assert result.summary.completion_tokens_known == 2
    assert pauses == [0.1, 0.2]
    assert len(provider.calls) == 3


@pytest.mark.parametrize("retries", [0, 1, 2])
def test_empty_json_exhausted_has_exact_retry_count(retries):
    executor, provider, pauses = guard([reply(None)] * (retries + 1), max_retries=retries)
    with pytest.raises(LLMExecutionError) as info:
        executor.execute(req())
    assert info.value.code == "EMPTY_JSON"
    assert info.value.summary.attempt_count == retries + 1
    assert info.value.summary.retry_count == retries
    assert info.value.summary.failure_codes == ("EMPTY_JSON",) * (retries + 1)
    assert len(provider.calls) == retries + 1 and len(pauses) == retries
    assert "synthetic-secret" not in str(info.value)


@pytest.mark.parametrize("content,finish,expected", [
    ("not-json", "stop", "INVALID_RESPONSE"),
    ('{"items": ', "stop", "INVALID_RESPONSE"),
    ('{"items":[]}', "length", "TRUNCATED"),
    ('{"items":[]}', "content_filter", "INCOMPLETE"),
])
def test_nonblank_invalid_or_truncated_json_never_retries(content, finish, expected):
    executor, provider, pauses = guard([reply(content, finish), reply('{"items":[]}')])
    with pytest.raises(LLMExecutionError) as info:
        executor.execute(req())
    assert info.value.code == expected
    assert info.value.summary.attempt_count == 1
    assert len(provider.calls) == 1 and pauses == []


def test_empty_json_object_is_nonblank_and_schema_failure_does_not_retry():
    class Required(BaseModel):
        items: list[str] = []
        required_field: str

    executor, provider, pauses = guard([reply("{}"), reply('{"required_field":"x"}')])
    request = req()
    result = executor.execute(request)
    assert result.response.parsed_json == {}
    with pytest.raises(StructuredOutputError) as error:
        validate_structured_output(request, result.response, Required)
    assert error.value.code == "SCHEMA_INVALID"
    assert len(provider.calls) == 1 and pauses == []


def test_text_mode_blank_is_not_retryable():
    executor, provider, pauses = guard([reply(None), reply("Synthetic text")])
    with pytest.raises(LLMExecutionError) as info:
        executor.execute(req("qa"))
    assert info.value.code == "INVALID_RESPONSE"
    assert len(provider.calls) == 1 and pauses == []


def recorder_identity():
    from hashlib import sha256
    from dynamic_memory.llm import CallIdentity

    def digest(text):
        return "sha256:" + sha256(text.encode("utf-8")).hexdigest()

    return CallIdentity(
        sample_id="synthetic_001", scope_id="window_001", task="memory_extraction",
        method=None, run_kind="development", backend="deepseek",
        resolved_model_id=MODEL, data_fingerprint=digest("data"),
        template_id="synthetic_extraction", template_version="test_v1",
        template_fingerprint=digest("prompt"), schema_id="test_items",
        schema_version="v1", config_fingerprint=digest("config"),
        execution_policy_fingerprint=digest("policy"), retriever_fingerprint=None,
        query_analysis_version=None, git_sha="a" * 40,
    )


def recorder_validator(request, response):
    class Extracted(BaseModel):
        items: list[str]
    return validate_structured_output(request, response, Extracted)


def test_a08_cache_and_log_preserve_empty_json_retry_counts_without_rebilling_on_hit():
    from dynamic_memory.llm import MemoryRecordingStore, RecordedLLMExecutor

    executor, provider, _ = guard([reply(" \n "), reply('{"items": []}')])
    store = MemoryRecordingStore()
    recorded = RecordedLLMExecutor(executor, store=store)
    request = req()
    identity = recorder_identity()
    first = recorded.execute(request, identity, validator=recorder_validator)
    assert first.cache_status == "miss"
    assert first.summary.attempt_count == 2
    assert first.summary.failure_codes == ("EMPTY_JSON",)
    second = recorded.execute(request, identity, validator=recorder_validator)
    assert second.cache_status == "hit" and second.summary.attempt_count == 0
    assert second.source_summary is not None and second.source_summary.attempt_count == 2
    assert len(provider.calls) == 2
    events = store.events()
    assert len(events) == 2
    assert [item["cache_status"] for item in events] == ["miss", "hit"]
    assert [item["summary"]["attempt_count"] for item in events] == [2, 0]


def test_empty_json_failed_call_is_logged_but_never_cached():
    from dynamic_memory.llm import MemoryRecordingStore, RecordedLLMExecutor

    executor, provider, _ = guard(
        [reply(None), reply(""), reply("\t"), reply('{"items": []}')]
    )
    store = MemoryRecordingStore()
    recorder = RecordedLLMExecutor(executor, store=store)
    request = req()
    identity = recorder_identity()
    with pytest.raises(LLMExecutionError) as info:
        recorder.execute(request, identity, validator=recorder_validator)
    assert info.value.code == "EMPTY_JSON"
    assert info.value.summary.attempt_count == 3
    result = recorder.execute(request, identity, validator=recorder_validator)
    assert result.cache_status == "miss" and result.summary.attempt_count == 1
    assert len(provider.calls) == 4
    events = store.events()
    assert [item["status"] for item in events] == ["error", "success"]
    assert [item["summary"]["attempt_count"] for item in events] == [3, 1]
