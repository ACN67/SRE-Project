"""In-memory Chat Completions fakes only; no SDK, key or network required."""

import sys
from types import ModuleType, SimpleNamespace

import pytest

from dynamic_memory.llm import (
    DeepSeekAdapterError, DeepSeekLLMAdapter, GenerationSettings, LLMAdapter,
    LLMMessage, LLMRequest, MockLLMAdapter,
)
from dynamic_memory.pipeline.settings import load_settings


MODEL = "synthetic-test-model"  # never a project default or actual model selection


def reply(content="Synthetic answer", *, model=MODEL, finish_reason="stop", usage=None, choices=True, reasoning_content=None):
    item = SimpleNamespace(message=SimpleNamespace(content=content, reasoning_content=reasoning_content), finish_reason=finish_reason)
    return SimpleNamespace(choices=[item] if choices else [], model=model, usage=usage)


class FakeCompletions:
    def __init__(self, result=None, error=None):
        self.calls = []
        self.result = result if result is not None else reply()
        self.error = error

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


class FakeClient:
    def __init__(self, result=None, error=None):
        self.completions = FakeCompletions(result, error)
        self.chat = SimpleNamespace(completions=self.completions)


def make_request(task="qa", *, model_id=MODEL, thinking="disabled"):
    config = load_settings().models
    budgets = {
        "memory_extraction": (config.extraction_max_input_tokens, config.extraction_max_output_tokens),
        "state_reasoning": (config.reasoning_max_input_tokens, config.reasoning_max_output_tokens),
        "qa": (config.qa_max_input_tokens, config.qa_max_output_tokens),
    }
    max_input, max_output = budgets[task]
    return LLMRequest(
        messages=(LLMMessage(role="system", content="Return JSON for synthetic tests."), LLMMessage(role="user", content="Synthetic only")),
        task=task,
        output_mode="text" if task == "qa" else "json",
        settings=GenerationSettings(max_input_tokens=max_input, max_output_tokens=max_output, temperature=config.temperature, thinking=thinking),
        model_id=model_id,
    )


def test_import_and_construction_are_offline_and_disabled_by_default(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-secret-not-read")
    client = FakeClient()
    adapter = DeepSeekLLMAdapter(client=client, model_id=MODEL)
    assert isinstance(adapter, LLMAdapter)
    assert client.completions.calls == []
    with pytest.raises(DeepSeekAdapterError, match="API_DISABLED") as error:
        adapter.generate(make_request())
    assert "synthetic-secret-not-read" not in str(error.value)
    assert client.completions.calls == []
    assert load_settings().models.default_backend == "mock"
    assert load_settings().models.api_enabled_by_default is False
    assert MockLLMAdapter().generate(make_request(model_id=None)).usage is None


@pytest.mark.parametrize("task", ["memory_extraction", "state_reasoning", "qa"])
def test_three_tasks_map_to_one_chat_completions_call(task):
    raw = '{"synthetic":true}' if task != "qa" else "Synthetic answer"
    client = FakeClient(reply(raw, usage=SimpleNamespace(prompt_tokens=11, completion_tokens=3)))
    adapter = DeepSeekLLMAdapter(api_enabled=True, client=client, model_id=MODEL)
    call = make_request(task)
    response = adapter.generate(call)
    assert len(client.completions.calls) == 1
    kwargs = client.completions.calls[0]
    assert kwargs["model"] == MODEL
    assert kwargs["messages"] == [
        {"role": "system", "content": "Return JSON for synthetic tests."},
        {"role": "user", "content": "Synthetic only"},
    ]
    assert kwargs["max_tokens"] == call.settings.max_output_tokens
    assert kwargs["temperature"] == 0
    assert kwargs["stream"] is False
    assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
    if task == "qa":
        assert "response_format" not in kwargs
        assert response.parsed_json is None
    else:
        assert kwargs["response_format"] == {"type": "json_object"}
        assert response.parsed_json == {"synthetic": True}
    assert response.finish_reason == "stop" and response.model_id == MODEL
    assert response.usage.prompt_tokens == 11 and response.usage.completion_tokens == 3


def test_max_tokens_uses_output_budget_when_input_differs():
    client = FakeClient()
    base = make_request()
    call = base.model_copy(update={"settings": GenerationSettings(max_input_tokens=1234, max_output_tokens=17, temperature=0, thinking="disabled")})
    DeepSeekLLMAdapter(api_enabled=True, client=client).generate(call)
    assert client.completions.calls[0]["max_tokens"] == 17


def test_missing_optional_response_metadata_and_reasoning_content_ignored():
    client = FakeClient(reply("Synthetic final", model=None, finish_reason=None, usage=None, reasoning_content="private synthetic thought"))
    result = DeepSeekLLMAdapter(api_enabled=True, client=client).generate(make_request())
    assert result.raw_output == "Synthetic final"
    assert result.usage is None and result.finish_reason is None and result.model_id is None
    assert "thought" not in result.raw_output


def test_model_and_credential_fail_before_any_request(monkeypatch):
    fake = FakeClient()
    adapter = DeepSeekLLMAdapter(api_enabled=True, client=fake)
    with pytest.raises(DeepSeekAdapterError, match="MODEL_REQUIRED"):
        adapter.generate(make_request(model_id=None))
    with pytest.raises(DeepSeekAdapterError, match="MODEL_MISMATCH"):
        DeepSeekLLMAdapter(api_enabled=True, client=fake, model_id="other-synthetic-model").generate(make_request())
    with pytest.raises(DeepSeekAdapterError, match="UNSUPPORTED_THINKING"):
        adapter.generate(make_request(thinking="enabled"))
    assert fake.completions.calls == []
    no_client = DeepSeekLLMAdapter(api_enabled=True, model_id=MODEL)
    with pytest.raises(DeepSeekAdapterError, match="CREDENTIAL_REQUIRED"):
        no_client.generate(make_request())
    assert fake.completions.calls == []


def test_missing_sdk_reports_install_hint_without_import_side_effect(monkeypatch):
    from dynamic_memory.llm import deepseek

    def missing_sdk(*args):
        raise DeepSeekAdapterError("SDK_MISSING", 'Install with pip install -e ".[api]"')

    monkeypatch.setattr(deepseek, "_new_sdk_client", missing_sdk)
    adapter = DeepSeekLLMAdapter(api_enabled=True, api_key="synthetic-credential", model_id=MODEL)
    with pytest.raises(DeepSeekAdapterError, match="SDK_MISSING") as error:
        adapter.generate(make_request())
    assert "synthetic-credential" not in str(error.value)


def test_optional_sdk_client_uses_official_endpoint_and_no_sdk_retries(monkeypatch):
    from dynamic_memory.llm import deepseek

    captured = []
    fake_module = ModuleType("openai")
    fake_module.OpenAI = lambda **kwargs: captured.append(kwargs) or object()
    monkeypatch.setitem(sys.modules, "openai", fake_module)
    deepseek._new_sdk_client("synthetic-credential", 120)
    assert captured == [{
        "api_key": "synthetic-credential",
        "base_url": "https://api.deepseek.com",
        "timeout": 120,
        "max_retries": 0,
    }]


@pytest.mark.parametrize(
    ("result", "code"),
    [
        (reply(choices=False), "INVALID_RESPONSE"),
        (reply(content=None, reasoning_content="synthetic thought"), "INVALID_RESPONSE"),
        (reply(content="  "), "INVALID_RESPONSE"),
        (reply(content="bad JSON"), "INVALID_RESPONSE"),
        (reply(content="[1,2]"), "INVALID_RESPONSE"),
        (reply(finish_reason="length"), "TRUNCATED"),
        (reply(finish_reason="content_filter"), "INCOMPLETE"),
        (reply(finish_reason="aborted"), "INCOMPLETE"),
        (reply(finish_reason="tool_calls"), "INCOMPLETE"),
        (reply(usage=SimpleNamespace(prompt_tokens=-1, completion_tokens=1)), "INVALID_USAGE"),
        (reply(model="other-synthetic-model"), "MODEL_MISMATCH"),
    ],
)
def test_bad_responses_are_failures(result, code):
    call = make_request("memory_extraction") if result.choices and getattr(result.choices[0].message, "content", None) in {"bad JSON", "[1,2]"} else make_request()
    client = FakeClient(result)
    with pytest.raises(DeepSeekAdapterError) as error:
        DeepSeekLLMAdapter(api_enabled=True, client=client).generate(call)
    assert error.value.code == code
    assert len(client.completions.calls) == 1


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (TimeoutError("synthetic-secret and prompt"), "TIMEOUT"),
        (ConnectionError("synthetic-secret and prompt"), "NETWORK"),
        (type("AuthenticationError", (Exception,), {})("synthetic-secret and prompt"), "AUTH"),
        (type("RateLimitError", (Exception,), {})("synthetic-secret and prompt"), "RATE_LIMIT"),
        (type("APIStatusError", (Exception,), {"status_code": 503})("synthetic-secret and prompt"), "API_STATUS"),
    ],
)
def test_provider_exceptions_are_sanitized_and_not_retried(error, code):
    client = FakeClient(error=error)
    with pytest.raises(DeepSeekAdapterError) as caught:
        DeepSeekLLMAdapter(api_enabled=True, client=client).generate(make_request())
    assert caught.value.code == code
    assert "synthetic-secret" not in str(caught.value)
    assert "prompt" not in str(caught.value)
    assert len(client.completions.calls) == 1


def test_explicit_constructor_model_is_checked_against_response():
    client = FakeClient(reply(model="different-synthetic-model"))
    with pytest.raises(DeepSeekAdapterError, match="MODEL_MISMATCH"):
        DeepSeekLLMAdapter(api_enabled=True, client=client, model_id=MODEL).generate(make_request(model_id=None))


def test_constructor_validation():
    for kwargs in ({"api_enabled": "yes"}, {"model_id": " "}, {"request_timeout_seconds": 0}, {"request_timeout_seconds": float("nan")}):
        with pytest.raises((TypeError, ValueError)):
            DeepSeekLLMAdapter(**kwargs)
