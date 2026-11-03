"""Only a numeric HTTP status and fixed category survive provider failures."""

from types import SimpleNamespace
from pathlib import Path

import pytest

from dynamic_memory.llm.deepseek import DeepSeekAdapterError, DeepSeekLLMAdapter
from dynamic_memory.llm.execution import GuardedLLMAdapter, LLMExecutionError
from dynamic_memory.llm.protocol import GenerationSettings, LLMMessage, LLMRequest
from dynamic_memory.pipeline.settings import load_settings
from dynamic_memory.contracts import ConversationSample, ConversationSession, SourceTurn
from dynamic_memory.memory.extraction import build_extraction_request
from dynamic_memory.memory.windows import plan_extraction_windows

PROFILE = Path(__file__).resolve().parents[2] / "configs" / "models_high_pilot.yaml"


def request():
    return LLMRequest(messages=(LLMMessage(role="user", content='Return json: {"facts":[]}'),),
        task="memory_extraction", output_mode="json", model_id="deepseek-flash",
        settings=GenerationSettings(max_input_tokens=4096, max_output_tokens=2048,
            temperature=0, thinking="enabled", reasoning_effort="high"))


class UnsafeError(Exception):
    def __init__(self, status):
        super().__init__("sensitive-response-body and credential")
        self.status_code = status
        self.response = SimpleNamespace(text="sensitive-response-body", headers={"authorization": "secret"})


class FailingClient:
    def __init__(self, status):
        self.status = status
        self.calls = []
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        raise UnsafeError(self.status)


@pytest.mark.parametrize("status,code,reason", [
    (400, "API_STATUS", "INVALID_REQUEST"), (401, "AUTH", "AUTHENTICATION_FAILED"),
    (402, "API_STATUS", "INSUFFICIENT_BALANCE"), (403, "AUTH", "FORBIDDEN"),
    (422, "API_STATUS", "INVALID_PARAMETERS"), (429, "RATE_LIMIT", "RATE_LIMIT"),
    (500, "API_STATUS", "SERVER_ERROR"), (503, "API_STATUS", "OVERLOADED"),
    (418, "API_STATUS", "HTTP_OTHER"), (None, "PROVIDER_ERROR", "UNKNOWN"),
    ("402", "PROVIDER_ERROR", "UNKNOWN"),
])
def test_status_is_safe_and_old_code_is_stable(status, code, reason):
    client = FailingClient(status)
    settings = load_settings(models_profile=PROFILE).models
    guard = GuardedLLMAdapter(DeepSeekLLMAdapter(api_enabled=True, client=client), settings)
    with pytest.raises(LLMExecutionError) as caught:
        guard.execute(request())
    error = caught.value
    assert error.code == code
    assert error.summary.safe_status_code == (status if type(status) is int else None)
    assert error.summary.safe_reason == reason
    assert error.summary.attempt_count == 1 and error.summary.retry_count == 0
    assert len(client.calls) == 1
    assert "sensitive-response-body" not in str(error)
    assert "credential" not in str(error)
    assert client.calls[0]["response_format"] == {"type": "json_object"}
    assert client.calls[0]["reasoning_effort"] == "high"


def test_http_error_status_can_be_read_from_response_without_body():
    error = type("HTTPStatusError", (Exception,), {})('sensitive-response-body')
    error.response = SimpleNamespace(status_code=422, text="sensitive-response-body")
    client = FailingClient(None)
    client.create = lambda **kwargs: (_ for _ in ()).throw(error)
    settings = load_settings(models_profile=PROFILE).models
    with pytest.raises(LLMExecutionError) as caught:
        GuardedLLMAdapter(DeepSeekLLMAdapter(api_enabled=True, client=client), settings).execute(request())
    assert caught.value.summary.safe_status_code == 422
    assert caught.value.summary.safe_reason == "INVALID_PARAMETERS"
    assert "sensitive-response-body" not in str(caught.value)


def test_b1_shaped_json_request_serializes_without_real_data_or_network():
    settings = load_settings(models_profile=PROFILE)
    sample = ConversationSample(sample_id="synthetic-0", source_order_1_based=1,
        sessions=[ConversationSession(session_id="session_1", turns=[SourceTurn(
            sample_id="synthetic-0", session_id="session_1", dia_id="d1",
            speaker="Ada", text="Ada kept a note.")])])
    window = plan_extraction_windows(sample, settings.extraction,
                                     reserved_prompt_tokens=1800)[0]
    built = build_extraction_request(window, settings.models)
    class Capture:
        def __init__(self):
            self.calls = []
            self.chat = SimpleNamespace(completions=self)
        def create(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
                message=SimpleNamespace(content='{"facts":[]}', reasoning_content="private"))],
                model="deepseek-flash", usage=None)
    client = Capture()
    DeepSeekLLMAdapter(api_enabled=True, client=client).generate(built)
    kwargs = client.calls[0]
    assert len(kwargs["messages"]) == 2 and kwargs["messages"][0]["role"] == "system"
    assert kwargs["response_format"] == {"type": "json_object"}
    assert "max_tokens" not in kwargs and kwargs["reasoning_effort"] == "high"
    assert kwargs["extra_body"] == {"thinking": {"type": "enabled"}}
    assert "temperature" not in kwargs and "reasoning_content" not in str(kwargs)


def test_synthetic_adapter_cannot_inject_secret_through_safe_category():
    error = DeepSeekAdapterError("API_STATUS", "safe message",
        safe_status_code="402", safe_reason="private provider response")
    assert error.safe_status_code is None and error.safe_reason == "UNKNOWN"


@pytest.mark.parametrize("finish,content,expected", [
    ("length", '{"facts":[', "TRUNCATED"),
    ("stop", " ", "EMPTY_JSON"),
    ("stop", '{"facts":', "INVALID_RESPONSE"),
])
def test_rejected_completion_keeps_safe_actual_usage(finish, content, expected):
    settings = load_settings(models_profile=PROFILE)
    sample = ConversationSample(sample_id="synthetic-0", source_order_1_based=1,
        sessions=[ConversationSession(session_id="session_1", turns=[SourceTurn(
            sample_id="synthetic-0", session_id="session_1", dia_id="d1",
            speaker="Ada", text="Ada kept a note.")])])
    window = plan_extraction_windows(sample, settings.extraction,
                                     reserved_prompt_tokens=1800)[0]
    built = build_extraction_request(window, settings.models)
    class Capture:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)
            self.calls = []
        def create(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish,
                message=SimpleNamespace(content=content, reasoning_content="private reasoning"))],
                model="deepseek-flash", usage=SimpleNamespace(prompt_tokens=100,
                    completion_tokens=500, prompt_cache_hit_tokens=20,
                    prompt_cache_miss_tokens=80,
                    completion_tokens_details=SimpleNamespace(reasoning_tokens=480)))
    client = Capture()
    guard = GuardedLLMAdapter(DeepSeekLLMAdapter(api_enabled=True, client=client),
                              settings.models)
    with pytest.raises(LLMExecutionError) as caught:
        guard.execute(built)
    summary = caught.value.summary
    assert caught.value.code == expected
    assert summary.attempt_count == 1 and summary.unknown_usage_attempts == 0
    assert (summary.prompt_tokens_known, summary.completion_tokens_known,
            summary.reasoning_tokens_known, summary.final_tokens_known) == (100, 500, 480, 20)
    assert (summary.prompt_cache_hit_tokens_known,
            summary.prompt_cache_miss_tokens_known) == (20, 80)
    assert summary.finish_reason == finish and summary.http_response_received
    assert summary.response_model_id == "deepseek-flash"
    assert "max_tokens" not in client.calls[0]
    assert "private reasoning" not in str(summary)
