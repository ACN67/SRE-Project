"""B-05 extraction through the shared Mock, guarded and recording seams."""

import json
from dataclasses import replace
from datetime import datetime

import pytest

from dynamic_memory.contracts import ConversationSample, ConversationSession, SourceTurn
from dynamic_memory.llm import (
    CallIdentity, DeepSeekAdapterError, GuardedLLMAdapter, LLMResponse, MockLLMAdapter,
    RecordedLLMExecutor, request_fingerprint,
)
from dynamic_memory.memory.extraction import (
    MEMORY_DRAFT_PROMPT, WindowExtractionError, build_extraction_request, extract_window,
)
from dynamic_memory.memory.windows import plan_extraction_windows
from dynamic_memory.pipeline.settings import load_settings


def _window(*, text: str = "Ada plans to apply for a job.", caption: str | None = None):
    settings = load_settings()
    sample = ConversationSample(sample_id="sample_1", source_order_1_based=1, sessions=[
        ConversationSession(session_id="session_1", turns=[SourceTurn(
            sample_id="sample_1", session_id="session_1", dia_id="d1", speaker="Ada",
            text=text, image_caption=caption,
            caption_origin="blip_caption" if caption else None,
        )]),
    ])
    return plan_extraction_windows(sample, settings.extraction)[0], settings.models


def _identity(window):
    sha = "sha256:" + "a" * 64
    return CallIdentity(
        sample_id=window.sample_id, task="memory_extraction", scope_id=window.extraction_window_id,
        method=None, run_kind="development", backend="mock", resolved_model_id=None,
        data_fingerprint=sha, template_id=MEMORY_DRAFT_PROMPT.template_id,
        template_version=MEMORY_DRAFT_PROMPT.version,
        template_fingerprint=MEMORY_DRAFT_PROMPT.template_fingerprint,
        schema_id="atomic_fact_draft", schema_version="1", config_fingerprint=sha,
        execution_policy_fingerprint=sha, retriever_fingerprint=None,
        query_analysis_version=None, git_sha="0" * 40,
    )


def _fact(**changes):
    fact = {
        "content": "Ada plans to apply for a job.", "entity": "Ada",
        "attribute": "employment_plan", "value": "apply_for_job",
        "fact_kind": "state", "assertion_type": "plan", "polarity": "positive",
        "recorded_at": None, "event_time": None,
        "source_refs": [{"session_id": "session_1", "dia_id": "d1"}],
    }
    fact.update(changes)
    return fact


def _executor(window, models, raw: str):
    request = build_extraction_request(window, models)
    mock = MockLLMAdapter({
        request_fingerprint(request): LLMResponse(output_mode="json", raw_output=raw),
    }, strict=True)
    return RecordedLLMExecutor(GuardedLLMAdapter(mock, models, sleep=lambda _: None))


def test_one_atomic_fact_from_exact_mock_request() -> None:
    window, models = _window()
    result = extract_window(window, _executor(window, models, json.dumps({"facts": [_fact()]})),
                            _identity(window), models)

    assert result.extraction_window_id == window.extraction_window_id
    assert len(result.facts) == 1
    assert result.facts[0].assertion_type == "plan"
    assert result.facts[0].source_refs[0].dia_id == "d1"


def test_multiple_claim_kinds_and_unknown_time_are_preserved() -> None:
    window, models = _window()
    facts = [
        _fact(content="Ada started work.", fact_kind="event", assertion_type="fact"),
        _fact(content="Ada does not work there now.", fact_kind="state", assertion_type="fact",
              polarity="negative", event_time={"raw": "now", "start": None,
                                                "end_exclusive": None, "precision": "unknown",
                                                "resolution": "unknown"}),
        _fact(content="Ada may apply later.", fact_kind="unknown", assertion_type="uncertain",
              polarity="unknown", entity=None, attribute=None, value=None),
    ]
    result = extract_window(window, _executor(window, models, json.dumps({"facts": facts})),
                            _identity(window), models)
    assert [(fact.fact_kind, fact.assertion_type, fact.polarity) for fact in result.facts] == [
        ("event", "fact", "positive"), ("state", "fact", "negative"),
        ("unknown", "uncertain", "unknown"),
    ]
    assert result.facts[1].event_time.raw == "now"
    assert result.facts[2].recorded_at is None
    assert result.facts[2].entity is None


def test_empty_facts_is_success_and_replay_is_deterministic() -> None:
    window, models = _window()
    executor = _executor(window, models, '{"facts": []}')
    first = extract_window(window, executor, _identity(window), models)
    second = extract_window(window, executor, _identity(window), models)
    assert first.facts == second.facts == ()
    assert (first.cache_status, second.cache_status) == ("miss", "hit")


@pytest.mark.parametrize("change", [
    {"source_refs": [{"session_id": "session_1", "dia_id": "missing"}]},
    {"source_refs": [{"session_id": "other_session", "dia_id": "d1"}]},
    {"recorded_at": "2024-01-01T00:00:00"},
])
def test_outside_source_or_invented_recorded_date_fails_without_cache(change) -> None:
    window, models = _window()
    executor = _executor(window, models, json.dumps({"facts": [_fact(**change)]}))
    for _ in range(2):
        with pytest.raises(WindowExtractionError) as caught:
            extract_window(window, executor, _identity(window), models)
        assert caught.value.code in {"SOURCE_OUT_OF_WINDOW", "RECORDED_AT_MISMATCH"}
        assert caught.value.extraction_window_id == window.extraction_window_id
    assert [event["cache_status"] for event in executor.store.events()] == ["miss", "miss"]


def test_recorded_date_may_match_one_of_multiple_cited_turns() -> None:
    settings = load_settings()
    sample = ConversationSample(sample_id="sample_1", source_order_1_based=1, sessions=[
        ConversationSession(session_id="session_1", turns=[SourceTurn(
            sample_id="sample_1", session_id="session_1", dia_id=dia_id,
            speaker="Ada", text="Ada works there.", recorded_at=datetime.fromisoformat(date),
        ) for dia_id, date in (("d1", "2024-01-01T10:00:00"),
                             ("d2", "2024-01-02T10:00:00"))]),
    ])
    window = plan_extraction_windows(sample, settings.extraction)[0]
    fact = _fact(recorded_at="2024-01-01T10:00:00", source_refs=[
        {"session_id": "session_1", "dia_id": "d1"},
        {"session_id": "session_1", "dia_id": "d2"},
    ])
    result = extract_window(window, _executor(window, settings.models, json.dumps({"facts": [fact]})),
                            _identity(window), settings.models)
    assert result.facts[0].recorded_at == "2024-01-01T10:00:00"


@pytest.mark.parametrize("change", [
    {"fact_kind": "fiction"}, {"assertion_type": "done"}, {"polarity": "neutral"},
    {"source_refs": []}, {"source_refs": [{"session_id": "session_1", "dia_id": "d1", "extra": 1}]},
    {"extra": "forbidden"}, {"content": 10}, {"content": "   "},
])
def test_invalid_schema_is_not_success(change) -> None:
    window, models = _window()
    executor = _executor(window, models, json.dumps({"facts": [_fact(**change)]}))
    with pytest.raises(WindowExtractionError) as caught:
        extract_window(window, executor, _identity(window), models)
    assert caught.value.code == "SCHEMA_INVALID"
    assert executor.store.events()[0]["status"] == "error"


def test_missing_required_field_is_rejected() -> None:
    window, models = _window()
    fact = _fact()
    del fact["event_time"]
    with pytest.raises(WindowExtractionError) as caught:
        extract_window(window, _executor(window, models, json.dumps({"facts": [fact]})),
                       _identity(window), models)
    assert caught.value.code == "SCHEMA_INVALID"


def test_complete_request_contains_caption_and_no_gold() -> None:
    window, models = _window(caption="a red bicycle")
    request = build_extraction_request(window, models)
    assert request.task == "memory_extraction" and request.output_mode == "json"
    assert request.settings.max_input_tokens == 4096
    assert "[image_caption: a red bicycle]" in request.messages[1].content
    assert '"speaker":"Ada"' in request.messages[1].content
    assert '"dia_id":"d1"' in request.messages[1].content
    assert all(term not in request.messages[1].content for term in
               ('"qa"', '"answer"', '"evidence"', '"observation"', '"session_summary"'))


def test_budget_is_checked_on_full_request_before_mock_call() -> None:
    window, models = _window()
    executor = _executor(window, models, '{"facts": []}')
    request = build_extraction_request(window, models)
    assert len(request.messages[0].content) > 512
    with pytest.raises(WindowExtractionError) as caught:
        extract_window(window, executor, _identity(window), models,
                       token_estimator=lambda _: 4096)
    assert caught.value.code == "INPUT_BUDGET_EXCEEDED"
    assert executor.store.events() == ()


def test_budget_boundary_accepts_and_unknown_estimate_rejects() -> None:
    window, models = _window()
    executor = _executor(window, models, '{"facts": []}')
    result = extract_window(window, executor, _identity(window), models,
                            token_estimator=lambda _: 4096 - 128)
    assert result.input_tokens_estimated == 3968
    with pytest.raises(WindowExtractionError) as caught:
        extract_window(window, executor, _identity(window), models,
                       token_estimator=lambda _: None)
    assert caught.value.code == "INPUT_BUDGET_UNKNOWN"


def test_b04_reserve_can_be_too_small_for_formal_prompt_without_truncation() -> None:
    window, models = _window(text="A" * 3000)
    assert window.estimated_total_tokens < window.max_input_tokens
    executor = _executor(window, models, '{"facts": []}')
    with pytest.raises(WindowExtractionError) as caught:
        extract_window(window, executor, _identity(window), models)
    assert caught.value.code == "INPUT_BUDGET_EXCEEDED"
    assert executor.store.events() == ()


def test_zero_or_invalid_estimate_fails_closed() -> None:
    window, models = _window()
    executor = _executor(window, models, '{"facts": []}')
    for estimate in (0, -1, True, None):
        with pytest.raises(WindowExtractionError) as caught:
            extract_window(window, executor, _identity(window), models,
                           token_estimator=lambda _: estimate)
        assert caught.value.code == "INPUT_BUDGET_UNKNOWN"
    assert executor.store.events() == ()


def test_two_windows_have_independent_success_and_failure_diagnostics() -> None:
    settings = load_settings()
    sample = ConversationSample(sample_id="sample_1", source_order_1_based=1, sessions=[
        ConversationSession(session_id=name, turns=[SourceTurn(
            sample_id="sample_1", session_id=name, dia_id="d1", speaker="Ada", text="fact",
        )]) for name in ("session_1", "session_2")
    ])
    windows = plan_extraction_windows(sample, settings.extraction)
    responses = {}
    for window in windows:
        raw = '{"facts": []}' if window.session_id == "session_1" else json.dumps({"facts": [
            _fact(source_refs=[{"session_id": "session_1", "dia_id": "d1"}]),
        ]})
        responses[request_fingerprint(build_extraction_request(window, settings.models))] = LLMResponse(
            output_mode="json", raw_output=raw,
        )
    executor = RecordedLLMExecutor(GuardedLLMAdapter(
        MockLLMAdapter(responses, strict=True), settings.models, sleep=lambda _: None,
    ))
    assert extract_window(windows[0], executor, _identity(windows[0]), settings.models).facts == ()
    with pytest.raises(WindowExtractionError) as caught:
        extract_window(windows[1], executor, _identity(windows[1]), settings.models)
    assert caught.value.code == "SOURCE_OUT_OF_WINDOW"
    assert caught.value.extraction_window_id == windows[1].extraction_window_id
    assert windows[0].extraction_window_id != windows[1].extraction_window_id
    assert [event["status"] for event in executor.store.events()] == ["success", "error"]


def test_sample_or_prompt_identity_mismatch_blocks_call() -> None:
    window, models = _window()
    executor = _executor(window, models, '{"facts": []}')
    for identity in (replace(_identity(window), sample_id="wrong_sample"),
                     replace(_identity(window), template_fingerprint="sha256:" + "b" * 64)):
        with pytest.raises(WindowExtractionError) as caught:
            extract_window(window, executor, identity, models)
        assert caught.value.code == "IDENTITY_MISMATCH"
    assert executor.store.events() == ()


@pytest.mark.parametrize("raw,code", [
    ('{"facts": [', "INVALID_RESPONSE"),
    ('{}', "SCHEMA_INVALID"),
    ('{"facts": [{"content": "x"}]}', "SCHEMA_INVALID"),
])
def test_bad_json_or_business_schema_cannot_become_empty_success(raw, code) -> None:
    window, models = _window()
    if code == "INVALID_RESPONSE":
        class InvalidJSONAdapter:
            def generate(self, _request):
                return LLMResponse.model_construct(output_mode="json", raw_output=raw)
        executor = RecordedLLMExecutor(GuardedLLMAdapter(
            InvalidJSONAdapter(), models, sleep=lambda _: None,
        ))
    else:
        executor = _executor(window, models, raw)
    with pytest.raises(WindowExtractionError) as caught:
        extract_window(window, executor, _identity(window), models)
    assert caught.value.code == code
    assert executor.store.events()[0]["status"] == "error"


def test_truncated_output_and_provider_failure_do_not_retry_twice() -> None:
    window, models = _window()

    class OfflineAdapter:
        def __init__(self, result):
            self.result = result
            self.calls = 0

        def generate(self, _request):
            self.calls += 1
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    truncated = OfflineAdapter(LLMResponse.model_construct(
        output_mode="json", raw_output='{"facts": []}', finish_reason="length",
    ))
    executor = RecordedLLMExecutor(GuardedLLMAdapter(truncated, models, sleep=lambda _: None))
    with pytest.raises(WindowExtractionError) as caught:
        extract_window(window, executor, _identity(window), models)
    assert caught.value.code == "INVALID_RESPONSE"
    assert truncated.calls == 1

    failing = OfflineAdapter(DeepSeekAdapterError("NETWORK", "synthetic offline failure"))
    executor = RecordedLLMExecutor(GuardedLLMAdapter(failing, models, sleep=lambda _: None))
    with pytest.raises(WindowExtractionError) as caught:
        extract_window(window, executor, _identity(window), models)
    assert caught.value.code == "NETWORK"
    assert failing.calls == models.max_retries + 1 == 3
    assert executor.store.events()[0]["summary"]["attempt_count"] == 3


def test_empty_json_uses_shared_retry_budget_only() -> None:
    window, models = _window()

    class EmptyJSONAdapter:
        def __init__(self):
            self.calls = 0

        def generate(self, _request):
            self.calls += 1
            raise DeepSeekAdapterError("EMPTY_JSON", "synthetic blank")

    adapter = EmptyJSONAdapter()
    executor = RecordedLLMExecutor(GuardedLLMAdapter(adapter, models, sleep=lambda _: None))
    with pytest.raises(WindowExtractionError) as caught:
        extract_window(window, executor, _identity(window), models)
    assert caught.value.code == "EMPTY_JSON"
    assert adapter.calls == 3
    assert executor.store.events()[0]["status"] == "error"
