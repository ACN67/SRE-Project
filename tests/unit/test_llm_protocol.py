"""Synthetic contract tests. FakeAdapter is local to tests, not A-03 Mock."""

import pytest
from pydantic import ValidationError

from dynamic_memory.llm import (
    GenerationSettings, LLMAdapter, LLMMessage, LLMRequest, LLMResponse,
    TokenUsage, validate_response_for_request,
)
from dynamic_memory.pipeline.settings import load_settings


def generation(max_input=4096, max_output=4096):
    config = load_settings().models
    return GenerationSettings(
        max_input_tokens=max_input,
        max_output_tokens=max_output,
        temperature=config.temperature,
        thinking=config.thinking,
    )


def request(task="memory_extraction", output_mode="json", settings=None):
    return LLMRequest(
        messages=(LLMMessage(role="system", content="Return JSON."), LLMMessage(role="user", content="Synthetic input")),
        task=task,
        output_mode=output_mode,
        settings=settings or generation(),
        model_id=load_settings().models.model_id,
    )


def test_three_tasks_use_loaded_budgets_and_undecided_model():
    config = load_settings().models
    extraction = request(settings=generation(config.extraction_max_input_tokens, config.extraction_max_output_tokens))
    reasoning = request("state_reasoning", "json", generation(config.reasoning_max_input_tokens, config.reasoning_max_output_tokens))
    qa = request("qa", "text", generation(config.qa_max_input_tokens, config.qa_max_output_tokens))
    assert (extraction.settings.max_input_tokens, extraction.settings.max_output_tokens) == (4096, 4096)
    assert (reasoning.settings.max_input_tokens, reasoning.settings.max_output_tokens) == (8192, 2048)
    assert (qa.settings.max_input_tokens, qa.settings.max_output_tokens) == (32768, 256)
    assert all(item.settings.temperature == 0 and item.settings.thinking == "disabled" for item in (extraction, reasoning, qa))
    assert extraction.model_id is None  # mock/undecided is valid
    assert generation(123, 9).max_output_tokens == 9  # no hardcoded frozen-only enum


def test_local_fake_is_provider_interchangeable():
    class FakeAdapter:
        def generate(self, call: LLMRequest) -> LLMResponse:
            if call.output_mode == "json":
                return LLMResponse(output_mode="json", raw_output='{"synthetic": true}')
            return LLMResponse(output_mode="text", raw_output="Synthetic answer")

    adapter: LLMAdapter = FakeAdapter()
    assert isinstance(adapter, LLMAdapter)
    for call in (request(), request("state_reasoning"), request("qa", "text")):
        result = adapter.generate(call)
        validate_response_for_request(call, result)
        assert result.parsed_json == {"synthetic": True} if call.output_mode == "json" else result.raw_output == "Synthetic answer"
        assert result.usage is None and result.finish_reason is None and result.model_id is None


@pytest.mark.parametrize(
    ("changes", "diagnostic"),
    [
        ({"messages": ()}, "messages"),
        ({"messages": (LLMMessage(role="system", content="only system"),)}, "user message"),
        ({"task": "unknown"}, "task"),
        ({"output_mode": "xml"}, "output_mode"),
        ({"output_mode": "text"}, "requires output_mode=json"),
        ({"model_id": "   "}, "model_id"),
    ],
)
def test_invalid_request(changes, diagnostic):
    data = dict(messages=(LLMMessage(role="user", content="x"),), task="memory_extraction", output_mode="json", settings=generation())
    data.update(changes)
    with pytest.raises(ValidationError, match=diagnostic):
        LLMRequest(**data)


def test_invalid_message_and_generation_settings():
    for fields in ({"role": "assistant", "content": "x"}, {"role": "user", "content": "   "}, {"role": "user", "content": ""}):
        with pytest.raises(ValidationError):
            LLMMessage(**fields)
    for fields in (
        {"max_input_tokens": 0}, {"max_output_tokens": -1},
        {"temperature": -0.1}, {"temperature": 3}, {"thinking": "maybe"},
    ):
        data = dict(max_input_tokens=100, max_output_tokens=10, temperature=0.0, thinking="disabled")
        data.update(fields)
        with pytest.raises(ValidationError):
            GenerationSettings(**data)


@pytest.mark.parametrize(
    ("fields", "diagnostic"),
    [
        ({"raw_output": ""}, "raw_output"),
        ({"raw_output": "  "}, "raw_output"),
        ({"raw_output": "not-json"}, "valid JSON"),
        ({"raw_output": "[1,2]"}, "must be an object"),
        ({"raw_output": "NaN"}, "valid JSON"),
        ({"raw_output": '{"ok":true}', "parsed_json": {"ok": False}}, "differs"),
        ({"raw_output": '{"ok":true}', "finish_reason": "length"}, "truncated"),
        ({"raw_output": '{"ok":true}', "usage": {"prompt_tokens": -1, "completion_tokens": 0}}, "prompt_tokens"),
        ({"raw_output": '{"ok":true}', "usage": {"prompt_tokens": 1, "completion_tokens": -1}}, "completion_tokens"),
    ],
)
def test_invalid_json_response(fields, diagnostic):
    data = {"output_mode": "json", "raw_output": '{"ok":true}'}
    data.update(fields)
    with pytest.raises(ValidationError, match=diagnostic):
        LLMResponse(**data)


def test_optional_usage_finish_reason_and_mode_validation():
    response = LLMResponse(
        output_mode="json", raw_output='{"item": 1}', finish_reason="stop",
        usage=TokenUsage(prompt_tokens=0, completion_tokens=2), model_id="synthetic-model",
    )
    assert response.parsed_json == {"item": 1}
    assert response.usage.prompt_tokens == 0
    call = request()
    with pytest.raises(ValueError, match="model_id"):
        validate_response_for_request(call.model_copy(update={"model_id": "other-model"}), response)
    with pytest.raises(ValueError, match="output_mode"):
        validate_response_for_request(call, LLMResponse(output_mode="text", raw_output="answer"))
    with pytest.raises(ValidationError, match="parsed_json"):
        LLMResponse(output_mode="text", raw_output="answer", parsed_json={"incorrect": True})
