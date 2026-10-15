"""Only synthetic offline inputs; no business answer or real usage is implied."""

import subprocess
import sys

import pytest
from pydantic import ValidationError

from dynamic_memory.llm import (
    GenerationSettings, LLMAdapter, LLMMessage, LLMRequest, LLMResponse,
    MockLLMAdapter, MockPresetNotFound, request_fingerprint,
    validate_response_for_request,
)
from dynamic_memory.pipeline.settings import load_settings


def make_request(task="qa", message="synthetic question", model_id=None):
    config = load_settings().models
    limits = {
        "memory_extraction": (config.extraction_max_input_tokens, config.extraction_max_output_tokens),
        "state_reasoning": (config.reasoning_max_input_tokens, config.reasoning_max_output_tokens),
        "qa": (config.qa_max_input_tokens, config.qa_max_output_tokens),
    }
    max_input, max_output = limits[task]
    return LLMRequest(
        messages=(LLMMessage(role="user", content=message),),
        task=task,
        output_mode="text" if task == "qa" else "json",
        settings=GenerationSettings(
            max_input_tokens=max_input, max_output_tokens=max_output,
            temperature=config.temperature, thinking=config.thinking,
        ),
        model_id=model_id,
    )


def test_three_tasks_use_one_protocol_and_safe_defaults():
    mock = MockLLMAdapter()
    assert isinstance(mock, LLMAdapter)
    for task in ("memory_extraction", "state_reasoning", "qa"):
        call = make_request(task)
        response = mock.generate(call)
        validate_response_for_request(call, response)
        assert response.output_mode == call.output_mode
        assert response.raw_output
        assert response.usage is None and response.finish_reason is None and response.model_id is None
        assert call.settings.thinking == "disabled" and call.settings.temperature == 0
        if task == "qa":
            assert response.parsed_json is None
            assert "Synthetic Mock" in response.raw_output
            assert "no factual answer" in response.raw_output
        else:
            assert response.parsed_json == {
                "mock": True, "synthetic": True, "task": task,
                "request_fingerprint": request_fingerprint(call),
            }
            assert response.raw_output.startswith('{"mock":true,')


def test_repeated_calls_instances_and_order_are_identical():
    first = make_request("qa", "synthetic one")
    second = make_request("qa", "synthetic two")
    adapter = MockLLMAdapter()
    before = adapter.generate(first).model_dump()
    adapter.generate(second)
    assert adapter.generate(first).model_dump() == before
    assert MockLLMAdapter().generate(first).model_dump() == before
    assert request_fingerprint(first) != request_fingerprint(second)


def test_fingerprint_and_result_are_stable_across_processes():
    code = (
        "from dynamic_memory.llm import LLMRequest, LLMMessage, GenerationSettings, MockLLMAdapter, request_fingerprint; "
        "r=LLMRequest(messages=(LLMMessage(role='user',content='synthetic cross process'),),"
        "task='qa',output_mode='text',settings=GenerationSettings(max_input_tokens=32768,"
        "max_output_tokens=256,temperature=0,thinking='disabled')); "
        "print(request_fingerprint(r)); print(MockLLMAdapter().generate(r).raw_output)"
    )
    outputs = [subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout for _ in range(3)]
    assert outputs[0] == outputs[1] == outputs[2]
    assert outputs[0].splitlines()[0].startswith("sha256:")


def test_exact_preset_hit_and_isolated_return_objects():
    call = make_request("memory_extraction")
    preset = LLMResponse(output_mode="json", raw_output='{"synthetic":true,"fixture":"B"}')
    mock = MockLLMAdapter({request_fingerprint(call): preset}, strict=True)
    first = mock.generate(call)
    assert first.parsed_json == {"synthetic": True, "fixture": "B"}
    first.parsed_json["fixture"] = "mutated"
    preset.parsed_json["fixture"] = "mutated-original"
    assert mock.generate(call).parsed_json == {"synthetic": True, "fixture": "B"}
    assert mock.generate(call).raw_output == '{"synthetic":true,"fixture":"B"}'


def test_unmatched_presets_default_or_strict_and_no_cross_task_hit():
    call = make_request("qa", "synthetic source")
    preset = LLMResponse(output_mode="text", raw_output="Synthetic fixture answer")
    key = request_fingerprint(call)
    default = MockLLMAdapter({key: preset})
    strict = MockLLMAdapter({key: preset}, strict=True)
    assert default.generate(call).raw_output == preset.raw_output
    different_message = make_request("qa", "another synthetic source")
    different_task = make_request("state_reasoning", "synthetic source")
    for other in (different_message, different_task):
        assert request_fingerprint(other) != key
        assert "Mock" in default.generate(other).raw_output or default.generate(other).parsed_json["mock"] is True
        with pytest.raises(MockPresetNotFound, match="no Mock preset for sha256:"):
            strict.generate(other)


def test_invalid_preset_key_mode_and_payload_rejected():
    call = make_request("qa")
    with pytest.raises(ValueError, match="preset key"):
        MockLLMAdapter({"wrong-key": LLMResponse(output_mode="text", raw_output="Synthetic")})
    wrong_mode = MockLLMAdapter({request_fingerprint(call): LLMResponse(output_mode="json", raw_output='{"synthetic":true}')})
    with pytest.raises(ValueError, match="output_mode"):
        wrong_mode.generate(call)
    with pytest.raises(ValidationError, match="valid JSON"):
        LLMResponse(output_mode="json", raw_output="bad json")
    with pytest.raises(ValidationError, match="raw_output"):
        LLMResponse(output_mode="text", raw_output="")
    with pytest.raises(ValidationError, match="prompt_tokens"):
        LLMResponse(output_mode="text", raw_output="Synthetic", usage={"prompt_tokens": -1, "completion_tokens": 0})
    bypassed = LLMResponse(output_mode="text", raw_output="Synthetic").model_copy(update={"raw_output": ""})
    with pytest.raises(ValidationError, match="raw_output"):
        MockLLMAdapter({request_fingerprint(call): bypassed})


def test_model_id_none_and_explicit_are_accepted_without_real_metadata():
    adapter = MockLLMAdapter()
    for model_id in (None, "synthetic-model-name"):
        call = make_request("qa", model_id=model_id)
        response = adapter.generate(call)
        validate_response_for_request(call, response)
        assert response.model_id is None and response.usage is None


def test_invalid_configuration_and_mutated_request_are_rejected():
    with pytest.raises(TypeError, match="strict"):
        MockLLMAdapter(strict="yes")
    with pytest.raises(TypeError, match="presets"):
        MockLLMAdapter(presets=["not a mapping"])
    call = make_request("qa")
    invalid = call.model_copy(update={"task": "unknown"})
    with pytest.raises(ValidationError, match="task"):
        MockLLMAdapter().generate(invalid)
