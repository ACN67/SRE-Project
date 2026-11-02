"""Offline A-13 source-only QA contract tests; all data and answers are synthetic."""

from __future__ import annotations

from datetime import date, datetime, timezone
from hashlib import sha256

import pytest

from dynamic_memory.baselines import FullTextAnswerer, FullTextError, FullTextProvenance
from dynamic_memory.contracts import (
    ConversationSample, ConversationSession, QueryAnalysis, QueryRequest, SourceTurn,
)
from dynamic_memory.llm import LLMResponse
from dynamic_memory.pipeline.query_runner import QueryOrchestrator
from dynamic_memory.pipeline.runtime import create_runtime
from dynamic_memory.pipeline.settings import load_settings


def fingerprint(value):
    return "sha256:" + sha256(value.encode("utf-8")).hexdigest()


PROVENANCE = FullTextProvenance(fingerprint("synthetic dataset"), "synthetic-analysis-v1",
                                "a" * 40, "development")


def sample(sample_id="synthetic_sample"):
    return ConversationSample(sample_id=sample_id, source_order_1_based=1, sessions=[
        ConversationSession(session_id="session_1", turns=[
            SourceTurn(sample_id=sample_id, session_id="session_1", dia_id="dia_1",
                       speaker="Person A", text="Synthetic first event.",
                       recorded_at=datetime(2024, 1, 2, tzinfo=timezone.utc)),
            SourceTurn(sample_id=sample_id, session_id="session_1", dia_id="dia_empty",
                       speaker="Person B", text=""),
        ]),
        ConversationSession(session_id="session_2", turns=[
            SourceTurn(sample_id=sample_id, session_id="session_2", dia_id="dia_2",
                       speaker="Person B", text="Synthetic second event.",
                       image_caption="Synthetic scene", caption_origin="blip_caption"),
        ]),
    ])


def query(sample_id="synthetic_sample"):
    return QueryRequest(query_id="synthetic_q", sample_id=sample_id,
                        question="Which synthetic event?", as_of=date(2024, 3, 1))


def analysis(request, *, intent="historical_state", time_mode="as_of"):
    return QueryAnalysis(query_id=request.query_id, sample_id=request.sample_id,
                         query_intent=intent, time_mode=time_mode,
                         resolved_as_of=request.as_of if time_mode == "as_of" else None)


def estimator(messages):
    return sum(len(message.content) for message in messages)


class FakeAdapter:
    def __init__(self, *, text="Synthetic answer.", finish_reason="stop", failure=False):
        self.requests = []
        self.text = text
        self.finish_reason = finish_reason
        self.failure = failure

    def generate(self, request):
        self.requests.append(request)
        if self.failure:
            raise RuntimeError("SECRET_GOLD provider failure")
        return LLMResponse(output_mode="text", raw_output=self.text,
                           finish_reason=self.finish_reason)


def fixture(*, fake=None, token_estimator=estimator, strict=True, settings=None, provenance=PROVENANCE):
    fake = fake or FakeAdapter()
    runtime = create_runtime(settings or load_settings(), synthetic_adapter=fake,
                             token_estimator=token_estimator,
                             estimator_version="synthetic-char-count-v1" if token_estimator else None,
                             strict_budget=strict)
    diagnostics = []
    answerer = FullTextAnswerer(runtime, provenance, on_diagnostic=diagnostics.append,
                                synthetic_test_mode=True)
    return answerer, runtime, fake, diagnostics


def test_complete_source_prompt_runtime_and_a12_integration():
    answerer, runtime, fake, diagnostics = fixture()
    req = query()
    runner = QueryOrchestrator(analyze_query=lambda got: analysis(got),
                               get_sample=lambda _sample_id: sample(),
                               answer_full_text=answerer)
    response = runner.run_query(req, "full_text")
    assert response.answer_result.status == "answered"
    assert response.answer_result.answer == "Synthetic answer."
    assert response.retrieved is response.resolution is None
    assert response.answer_result.used_memory_ids == []
    assert response.answer_result.evidence_refs == []
    assert [(r.session_id, r.dia_id) for r in response.answer_result.context_source_refs] == [
        ("session_1", "dia_1"), ("session_2", "dia_2")]
    assert diagnostics[0].empty_refs == (("session_1", "dia_empty"),)
    assert not diagnostics[0].truncated_full_text and diagnostics[0].input_budget_verified
    assert len(fake.requests) == 1
    call = fake.requests[0]
    assert call.task == "qa" and call.output_mode == "text"
    assert call.settings.max_input_tokens == 32768 and call.settings.max_output_tokens == 256
    assert "Session=session_1 | dia_id=dia_1 | speaker=Person A | recorded_at=2024-01-02T00:00:00+00:00" in call.messages[1].content
    assert "Session=session_2 | dia_id=dia_2 | speaker=Person B" in call.messages[1].content
    assert "[image_caption: Synthetic scene]" in call.messages[1].content
    assert call.messages[1].content.index("dia_1") < call.messages[1].content.index("dia_2")
    assert "resolved_as_of=2024-03-01" in call.messages[1].content
    assert req.question in call.messages[1].content
    event = runtime.recorder.store.events()[0]
    assert event["identity"]["method"] == "full_text"
    assert event["identity"]["data_fingerprint"] == PROVENANCE.data_fingerprint


def test_truncation_keeps_recent_complete_turn_and_records_dropped():
    req = query()
    source = sample()
    # The configured limit includes the complete system and user messages.
    full_answerer, _, full_fake, _ = fixture()
    result = full_answerer(req, analysis(req), source)
    assert len(result.context_source_refs) == 2
    full_size = estimator(full_fake.requests[0].messages)
    settings = load_settings()
    limited = settings.model_copy(update={"models": settings.models.model_copy(
        update={"qa_max_input_tokens": full_size - 1})})
    answerer, _, fake, diagnostics = fixture(settings=limited)
    output = answerer(req, analysis(req), source)
    assert [(r.session_id, r.dia_id) for r in output.context_source_refs] == [("session_2", "dia_2")]
    assert diagnostics[0].dropped_refs == (("session_1", "dia_1"),)
    assert diagnostics[0].included_refs == (("session_2", "dia_2"),)
    assert diagnostics[0].truncated_full_text
    assert "Synthetic first event" not in fake.requests[0].messages[1].content

    exact = settings.model_copy(update={"models": settings.models.model_copy(
        update={"qa_max_input_tokens": full_size})})
    exact_answerer, _, _, exact_diagnostics = fixture(settings=exact)
    assert len(exact_answerer(req, analysis(req), source).context_source_refs) == 2
    assert exact_diagnostics[0].input_tokens_estimated == full_size
    assert not exact_diagnostics[0].truncated_full_text


def test_empty_unknown_and_unfit_budget_do_not_call_model():
    req = query()
    answerer, _, fake, diagnostics = fixture()
    empty = sample().model_copy(update={"sessions": []})
    result = answerer(req, analysis(req), empty)
    assert result.status == "no_evidence" and result.uncertainty
    assert not fake.requests and not diagnostics[0].included_refs

    unknown, _, unknown_fake, unknown_diag = fixture(token_estimator=None)
    with pytest.raises(FullTextError, match="INPUT_BUDGET_UNKNOWN"):
        unknown(req, analysis(req), sample())
    assert not unknown_fake.requests and not unknown_diag[0].input_budget_verified

    settings = load_settings()
    tiny = settings.model_copy(update={"models": settings.models.model_copy(
        update={"qa_max_input_tokens": 10})})
    unfit, _, unfit_fake, unfit_diag = fixture(settings=tiny)
    with pytest.raises(FullTextError, match="INPUT_BUDGET_EXCEEDED"):
        unfit(req, analysis(req), sample())
    assert not unfit_fake.requests and unfit_diag[0].truncated_full_text

    relaxed, _, relaxed_fake, relaxed_diag = fixture(token_estimator=None, strict=False)
    assert relaxed(req, analysis(req), sample()).status == "answered"
    assert len(relaxed_fake.requests) == 1
    assert relaxed_diag[0].input_tokens_estimated is None
    assert not relaxed_diag[0].input_budget_verified

    invalid, _, invalid_fake, invalid_diag = fixture(token_estimator=lambda _messages: -1)
    with pytest.raises(FullTextError, match="INVALID_ESTIMATE"):
        invalid(req, analysis(req), sample())
    assert not invalid_fake.requests and not invalid_diag[0].input_budget_verified


def test_source_and_identity_fail_closed():
    req = query()
    answerer, _, fake, _ = fixture()
    with pytest.raises(FullTextError, match="SOURCE_ID_MISMATCH"):
        answerer(req, analysis(req), sample("other_sample"))
    duplicate = sample()
    duplicate.sessions[0].turns.append(duplicate.sessions[0].turns[0].model_copy())
    with pytest.raises(FullTextError, match="SAMPLE_INVALID|SOURCE_DUPLICATE"):
        answerer(req, analysis(req), duplicate)
    bad_provenance = FullTextProvenance("fake", "synthetic-analysis-v1", "a" * 40, "development")
    bad, _, bad_fake, _ = fixture(provenance=bad_provenance)
    with pytest.raises(FullTextError, match="IDENTITY_INVALID"):
        bad(req, analysis(req), sample())
    assert not fake.requests and not bad_fake.requests


def test_default_mock_and_provider_failures_never_become_answers():
    req = query()
    runtime = create_runtime(load_settings(), token_estimator=estimator,
                             estimator_version="synthetic-char-count-v1", strict_budget=True)
    with pytest.raises(FullTextError, match="SYNTHETIC_ANSWER_DISALLOWED"):
        FullTextAnswerer(runtime, PROVENANCE, synthetic_test_mode=True)(req, analysis(req), sample())
    assert not runtime.recorder.store.events()
    for fake in (FakeAdapter(text="  "), FakeAdapter(finish_reason="length"), FakeAdapter(failure=True)):
        answerer, _, _, _ = fixture(fake=fake)
        with pytest.raises(FullTextError, match="MODEL_FAILED|ANSWER_INVALID") as failure:
            answerer(req, analysis(req), sample())
        assert "SECRET_GOLD" not in str(failure.value)

    failing, _, _, _ = fixture(fake=FakeAdapter(failure=True))
    runner = QueryOrchestrator(analyze_query=lambda got: analysis(got),
                               get_sample=lambda _sample_id: sample(),
                               answer_full_text=failing)
    response = runner.run_query(req, "full_text")
    assert response.answer_result.status == "error"
    assert response.answer_result.error.code == "COMPONENT_FAILED"
    assert "SECRET_GOLD" not in response.answer_result.error.message


@pytest.mark.parametrize("intent,time_mode", [
    ("current_state", "latest_known"), ("state_history", "latest_known"),
    ("event_fact", "unknown"), ("general_fact", "unknown"),
])
def test_shared_analysis_conditions_are_passed_without_reinterpretation(intent, time_mode):
    req = query().model_copy(update={"as_of": None})
    answerer, _, fake, _ = fixture()
    assert answerer(req, analysis(req, intent=intent, time_mode=time_mode), sample()).status == "answered"
    assert f"query_intent={intent}; time_mode={time_mode}" in fake.requests[0].messages[1].content
    assert "resolved_as_of=" not in fake.requests[0].messages[1].content


def test_two_samples_and_configuration_have_distinct_runtime_calls():
    answerer, runtime, fake, _ = fixture()
    for sample_id in ("sample_one", "sample_two"):
        req = query(sample_id)
        answerer(req, analysis(req), sample(sample_id))
    assert len(fake.requests) == 2
    events = runtime.recorder.store.events()
    assert len({event["cache_key"] for event in events}) == 2
    assert {event["identity"]["sample_id"] for event in events} == {"sample_one", "sample_two"}
