"""Synthetic, offline tests for the explicit post-provider validation step."""

import json

import pytest
from pydantic import BaseModel, ConfigDict, Field

from dynamic_memory.contracts.memory_id import compute_memory_id
from dynamic_memory.contracts.models import MemoryRecord, QueryAnalysis, ResolutionResult
from dynamic_memory.llm import (
    GenerationSettings, LLMMessage, LLMRequest, LLMResponse,
    StructuredOutputError, validate_structured_output,
)


def request(task="state_reasoning", model_id="synthetic-model"):
    return LLMRequest(
        messages=(LLMMessage(role="user", content="synthetic input"),),
        task=task,
        output_mode="text" if task == "qa" else "json",
        settings=GenerationSettings(
            max_input_tokens=100, max_output_tokens=100,
            temperature=0, thinking="disabled",
        ),
        model_id=model_id,
    )


def response(payload, **kwargs):
    return LLMResponse(output_mode="json", raw_output=json.dumps(payload), **kwargs)


def analysis_payload():
    return {
        "schema_version": "2.1", "query_id": "synthetic-q1",
        "sample_id": "synthetic-s1", "query_intent": "historical_state",
        "time_mode": "as_of", "resolved_as_of": "2024-01-01",
    }


def resolution_payload():
    return {
        "schema_version": "2.1", "query_id": "synthetic-q1",
        "decisions": [{"memory_id": "mem_a", "action": "RETAIN", "reason": "synthetic"}],
        "effective_memory_ids": ["mem_a"],
    }


def memory_payload():
    payload = {
        "schema_version": "2.1", "memory_id": "mem_00000000000000000000",
        "sample_id": "synthetic-s1", "content": "Synthetic fact.",
        "fact_kind": "state", "assertion_type": "fact", "polarity": "positive",
        "source_refs": [{"session_id": "synthetic-session", "dia_id": "synthetic-turn"}],
        "extractor_version": "synthetic",
    }
    payload["memory_id"] = compute_memory_id(MemoryRecord.model_validate(payload))
    return payload


class DraftItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1)
    fact_kind: str


class ExtractionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[DraftItem]


@pytest.mark.parametrize(
    ("payload", "schema", "attribute", "expected"),
    [
        (analysis_payload(), QueryAnalysis, "query_intent", "historical_state"),
        (resolution_payload(), ResolutionResult, "effective_memory_ids", ["mem_a"]),
        (memory_payload(), MemoryRecord, "sample_id", "synthetic-s1"),
        ({"items": [{"content": "synthetic draft", "fact_kind": "state"}]}, ExtractionDraft, "items", None),
    ],
)
def test_returns_business_model_without_mutating_response(payload, schema, attribute, expected):
    call = request("memory_extraction" if schema in (MemoryRecord, ExtractionDraft) else "state_reasoning")
    result = response(payload, model_id=call.model_id, finish_reason="stop")
    original_raw, original_parsed = result.raw_output, result.parsed_json.copy()
    validated = validate_structured_output(call, result, schema)
    assert isinstance(validated, schema)
    if schema is ExtractionDraft:
        assert validated.items[0].content == "synthetic draft"
    else:
        assert getattr(validated, attribute) == expected
    assert result.raw_output == original_raw and result.parsed_json == original_parsed


@pytest.mark.parametrize(
    ("schema", "change"),
    [
        (QueryAnalysis, lambda p: p.pop("query_intent")),
        (QueryAnalysis, lambda p: p.update(unknown_field="x")),
        (QueryAnalysis, lambda p: p.update(query_intent="invented")),
        (QueryAnalysis, lambda p: p.update(query_id=123)),
        (QueryAnalysis, lambda p: p.update(time_issue={"code": 123})),
        (QueryAnalysis, lambda p: p.update(schema_version="2.0")),
        (QueryAnalysis, lambda p: p.pop("schema_version")),
        (MemoryRecord, lambda p: p.update(memory_id="bad-id")),
        (MemoryRecord, lambda p: p["source_refs"].append(p["source_refs"][0].copy())),
        (MemoryRecord, lambda p: p["source_refs"].append({"session_id": "other", "dia_id": "x"})),
        (ResolutionResult, lambda p: p["decisions"].append(p["decisions"][0].copy())),
        (ResolutionResult, lambda p: p.update(effective_memory_ids=[])),
    ],
)
def test_syntax_valid_but_schema_invalid(schema, change):
    payload = {QueryAnalysis: analysis_payload, MemoryRecord: memory_payload, ResolutionResult: resolution_payload}[schema]()
    change(payload)
    with pytest.raises(StructuredOutputError) as info:
        validate_structured_output(request(), response(payload), schema)
    assert info.value.code == "SCHEMA_INVALID"


@pytest.mark.parametrize("raw", [
    '{"items":[],"items":[]}',
    '{"items":[{"content":"a","content":"b","fact_kind":"state"}]}',
])
def test_duplicate_keys_at_any_depth(raw):
    with pytest.raises(StructuredOutputError) as info:
        validate_structured_output(request(), LLMResponse(output_mode="json", raw_output=raw), ExtractionDraft)
    assert info.value.code == "JSON_INTEGRITY"


def test_floating_point_overflow_is_not_accepted_as_infinity():
    raw = '{"items":[],"overflow":1e400}'
    with pytest.raises(StructuredOutputError) as info:
        validate_structured_output(request(), LLMResponse(output_mode="json", raw_output=raw), ExtractionDraft)
    assert info.value.code == "JSON_INTEGRITY"


@pytest.mark.parametrize("raw", ["[]", "```json\n{}\n```", '{"items": [NaN]}', '{"items": [Infinity]}', '{"items":'])
def test_invalid_json_shapes_and_nonfinite_are_rejected(raw):
    # A-02 already rejects these at response construction; model_copy also must not bypass A-05.
    bypassed = response({"items": []}).model_copy(update={"raw_output": raw})
    with pytest.raises(StructuredOutputError) as info:
        validate_structured_output(request(), bypassed, ExtractionDraft)
    assert info.value.code in {"RESPONSE_INCONSISTENT", "JSON_INTEGRITY"}


def test_mutated_cache_and_model_copy_bypass_are_rejected():
    result = response(analysis_payload())
    result.parsed_json["query_intent"] = "current_state"
    with pytest.raises(StructuredOutputError) as info:
        validate_structured_output(request(), result, QueryAnalysis)
    assert info.value.code == "RESPONSE_INCONSISTENT"
    with pytest.raises(StructuredOutputError) as info:
        validate_structured_output(request().model_copy(update={"task": "qa"}), response(analysis_payload()), QueryAnalysis)
    assert info.value.code == "INVALID_ARGUMENT"
    with pytest.raises(StructuredOutputError) as info:
        validate_structured_output(request(), response(analysis_payload()).model_copy(update={"parsed_json": None}), QueryAnalysis)
    assert info.value.code == "RESPONSE_INCONSISTENT"


@pytest.mark.parametrize("kind", ["qa", "mode", "model", "length", "filtered", "schema"])
def test_mode_model_completion_and_schema_argument(kind):
    call, result, schema = request(), response(analysis_payload()), QueryAnalysis
    if kind == "qa":
        call = request("qa")
        result = LLMResponse(output_mode="text", raw_output="synthetic answer")
    elif kind == "mode":
        result = LLMResponse(output_mode="text", raw_output="synthetic answer")
    elif kind == "model":
        result = response(analysis_payload(), model_id="wrong-model")
    elif kind == "length":
        result = result.model_copy(update={"finish_reason": "length"})
    elif kind == "filtered":
        result = response(analysis_payload(), finish_reason="content_filter")
    else:
        schema = dict
    with pytest.raises(StructuredOutputError) as info:
        validate_structured_output(call, result, schema)
    assert info.value.code == {
        "qa": "INVALID_ARGUMENT", "mode": "RESPONSE_INCONSISTENT",
        "model": "RESPONSE_INCONSISTENT", "length": "RESPONSE_INCOMPLETE",
        "filtered": "RESPONSE_INCOMPLETE", "schema": "INVALID_ARGUMENT",
    }[kind]


def test_errors_never_echo_payload_prompt_or_pydantic_input_value():
    secret = "synthetic-secret"
    call = request().model_copy(update={"messages": (LLMMessage(role="user", content=secret),)})
    raw = json.dumps({**analysis_payload(), "query_intent": secret})
    result = LLMResponse(output_mode="json", raw_output=raw)
    with pytest.raises(StructuredOutputError) as info:
        validate_structured_output(call, result, QueryAnalysis)
    assert info.value.code == "SCHEMA_INVALID"
    assert secret not in str(info.value) and "input_value" not in str(info.value)
    assert info.value.__cause__ is None
