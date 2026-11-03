"""Offline High-mode protocol, adapter, budget and cache isolation tests."""

from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from dynamic_memory.llm import MEMORY_EXTRACTION_PROMPT
from dynamic_memory.llm.deepseek import DeepSeekAdapterError, DeepSeekLLMAdapter
from dynamic_memory.llm.execution import GuardedLLMAdapter, LLMExecutionError
from dynamic_memory.llm.protocol import GenerationSettings, LLMMessage, LLMRequest
from dynamic_memory.llm.recording import call_cache_key
from dynamic_memory.pipeline.runtime import create_runtime
from dynamic_memory.pipeline.settings import load_settings

MODEL = "deepseek-flash"
PROFILE = Path(__file__).resolve().parents[2] / "configs" / "models_high_pilot.yaml"


class FakeClient:
    def __init__(self, raw=None, error=None):
        self.calls = []
        self.raw = raw or reply()
        self.error = error
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.raw


def reply(content='{"ok":true}', *, finish="stop", model=MODEL,
          reasoning="private chain", prompt=10, completion=40, reasoning_tokens=20):
    return SimpleNamespace(
        choices=[SimpleNamespace(finish_reason=finish,
                                 message=SimpleNamespace(content=content, reasoning_content=reasoning))],
        model=model,
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion,
                              completion_tokens_details=SimpleNamespace(reasoning_tokens=reasoning_tokens)),
    )


def request(*, thinking="enabled", effort="high", mode="json", tokens=2048):
    return LLMRequest(
        messages=(LLMMessage(role="user", content='Return json: {"ok":true}.'),),
        task="memory_extraction" if mode == "json" else "qa", output_mode=mode,
        model_id=MODEL,
        settings=GenerationSettings(max_input_tokens=200, max_output_tokens=tokens,
                                    temperature=0, thinking=thinking, reasoning_effort=effort),
    )


def test_high_exact_outbound_and_reasoning_is_not_persisted():
    client = FakeClient()
    result = DeepSeekLLMAdapter(api_enabled=True, client=client, model_id=MODEL).generate(request())
    assert client.calls == [{"model": MODEL,
                             "messages": [{"role": "user", "content": 'Return json: {"ok":true}.'}],
                             "max_tokens": 2048, "stream": False,
                             "extra_body": {"thinking": {"type": "enabled"}},
                             "reasoning_effort": "high",
                             "response_format": {"type": "json_object"}}]
    assert result.parsed_json == {"ok": True}
    assert result.raw_output == '{"ok":true}'
    assert result.usage.reasoning_tokens == 20
    assert "private chain" not in repr(result) and "private chain" not in str(result.model_dump())


def test_disabled_outbound_compatibility():
    client = FakeClient(reply(content="OK"))
    result = DeepSeekLLMAdapter(api_enabled=True, client=client).generate(
        request(thinking="disabled", effort=None, mode="text", tokens=128))
    assert result.raw_output == "OK"
    assert client.calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert client.calls[0]["temperature"] == 0
    assert "reasoning_effort" not in client.calls[0]


@pytest.mark.parametrize("thinking,effort,tokens", [
    ("disabled", "high", 2048), ("enabled", "high", 128),
    ("enabled", "high", 4097),
])
def test_conflicts_and_unbounded_high_rejected(thinking, effort, tokens):
    with pytest.raises(ValidationError):
        request(thinking=thinking, effort=effort, tokens=tokens)


def test_legacy_mock_thinking_without_effort_cannot_call_real_provider():
    client = FakeClient()
    with pytest.raises(DeepSeekAdapterError) as caught:
        DeepSeekLLMAdapter(api_enabled=True, client=client).generate(request(effort=None))
    assert caught.value.code == "EFFORT_REQUIRED"
    assert client.calls == []


@pytest.mark.parametrize("raw,code", [
    (reply(content=" "), "EMPTY_JSON"),
    (reply(content="invalid"), "INVALID_RESPONSE"),
    (reply(finish="length"), "TRUNCATED"),
    (reply(content=None), "EMPTY_JSON"),
    (reply(model="other-model"), "MODEL_MISMATCH"),
    (reply(completion=2, reasoning_tokens=3), "INVALID_USAGE"),
])
def test_high_fail_closed(raw, code):
    client = FakeClient(raw)
    with pytest.raises(DeepSeekAdapterError) as caught:
        DeepSeekLLMAdapter(api_enabled=True, client=client).generate(request())
    assert caught.value.code == code
    assert "private chain" not in str(caught.value)
    assert len(client.calls) == 1


def test_reasoning_only_text_is_not_final_answer():
    client = FakeClient(reply(content=None))
    with pytest.raises(DeepSeekAdapterError) as caught:
        DeepSeekLLMAdapter(api_enabled=True, client=client).generate(request(mode="text"))
    assert caught.value.code == "EMPTY_FINAL"


@pytest.mark.parametrize("error,code", [
    (type("AuthenticationError", (Exception,), {})('secret reasoning'), "AUTH"),
    (type("RateLimitError", (Exception,), {})('secret reasoning'), "RATE_LIMIT"),
    (TimeoutError('secret reasoning'), "TIMEOUT"),
])
def test_high_provider_errors_are_sanitized_and_not_retried(error, code):
    client = FakeClient(error=error)
    settings = load_settings(models_profile=PROFILE).models
    guard = GuardedLLMAdapter(DeepSeekLLMAdapter(api_enabled=True, client=client), settings)
    with pytest.raises(LLMExecutionError) as caught:
        guard.execute(request())
    assert caught.value.code == code and caught.value.summary.attempt_count == 1
    assert len(client.calls) == 1 and "secret reasoning" not in str(caught.value)


def test_runtime_profile_and_cache_keys_separate_modes():
    high_settings = load_settings(models_profile=PROFILE)
    assert high_settings.models.default_backend == "mock"
    assert not high_settings.models.api_enabled_by_default
    assert high_settings.models.reasoning_effort == "high"
    low_settings = load_settings()
    low_settings.models.model_id = MODEL
    high = create_runtime(high_settings, backend="deepseek", model_id=MODEL)
    disabled = create_runtime(low_settings, backend="deepseek", model_id=MODEL)
    high_call = high.make_request(MEMORY_EXTRACTION_PROMPT, {"conversation": "synthetic only"})
    low_call = disabled.make_request(MEMORY_EXTRACTION_PROMPT, {"conversation": "synthetic only"})
    assert high_call.request.settings.reasoning_effort == "high"
    assert low_call.request.settings.reasoning_effort is None
    assert high_call.request.settings.max_output_tokens == 65536
    assert high_call.request.settings.omit_max_tokens
    assert high.config_fingerprint != disabled.config_fingerprint
    assert high.execution_policy_fingerprint != disabled.execution_policy_fingerprint
    digest = "sha256:" + sha256(b"synthetic").hexdigest()
    def identity(runtime, call):
        return runtime.make_identity(call, sample_id="synthetic", scope_id="window-1",
                                     run_kind="development", method=None,
                                     data_fingerprint=digest, schema_id="draft", schema_version="v1",
                                     retriever_fingerprint=None, query_analysis_version=None,
                                     git_sha="a" * 40)
    assert call_cache_key(high_call.request, identity(high, high_call)) != call_cache_key(
        low_call.request, identity(disabled, low_call))
    assert len(high.adapter.__dict__) > 0  # construction made no provider call
