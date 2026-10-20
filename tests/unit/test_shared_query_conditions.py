"""Synthetic C analyzer fakes only; no C algorithm, retrieval or model call."""

from __future__ import annotations

from datetime import date

import pytest

from dynamic_memory.contracts.models import QueryAnalysis, QueryRequest, QueryResponse, TimeIssue
from dynamic_memory.pipeline.query_conditions import QueryPreparationError, prepare_shared_query
from dynamic_memory.pipeline.runtime import create_runtime
from dynamic_memory.pipeline.settings import load_settings


def request(*, query_id="synthetic_q1", sample_id="synthetic_sample", as_of=None):
    return QueryRequest(query_id=query_id, sample_id=sample_id,
                        question="Synthetic private question SECRET_GOLD?", as_of=as_of)


def analysis(req, *, intent="historical_state", mode="latest_known", resolved=None, issue=None):
    return QueryAnalysis(query_id=req.query_id, sample_id=req.sample_id, query_intent=intent,
                         entity_hints=["synthetic person"], attribute_hints=["synthetic job"],
                         time_mode=mode, resolved_as_of=resolved, time_issue=issue)


def conflict(req):
    return analysis(req, mode="conflict", issue=TimeIssue(
        code="QUERY_TIME_CONFLICT", message="Synthetic conflicting dates SECRET_GOLD",
        explicit_as_of=req.as_of.isoformat(), question_time="2024-01-02"))


def test_one_analyzer_call_snapshot_reused_for_three_methods():
    req = request()
    calls = []

    def fake_c_analyzer(given):
        calls.append(given)
        return analysis(given, intent="state_history")

    prepared = prepare_shared_query(req, fake_c_analyzer)
    assert len(calls) == 1
    assert prepared.request.query_id == req.query_id
    assert prepared.request.sample_id == req.sample_id
    assert prepared.request.question == req.question
    assert prepared.analysis.query_intent == "state_history"
    assert prepared.analysis.time_mode == "latest_known"
    assert prepared.analysis.entity_hints == ["synthetic person"]
    assert prepared.analysis.attribute_hints == ["synthetic job"]
    assert not prepared.must_early_stop
    assert all(prepared.make_early_response(method) is None for method in
               ("full_text", "vector_rag", "dynamic"))
    assert len(calls) == 1


@pytest.mark.parametrize("intent", [
    "current_state", "historical_state", "state_history", "event_fact", "general_fact", "unknown",
])
def test_intent_is_independent_from_latest_known_time_mode(intent):
    prepared = prepare_shared_query(request(), lambda req: analysis(req, intent=intent))
    assert prepared.analysis.query_intent == intent
    assert prepared.analysis.time_mode == "latest_known"
    assert not prepared.must_early_stop


def test_explicit_and_question_only_as_of_are_preserved():
    explicit = date(2023, 5, 1)
    prepared = prepare_shared_query(request(as_of=explicit), lambda req: analysis(
        req, mode="as_of", resolved=explicit))
    assert prepared.request.as_of == prepared.analysis.resolved_as_of == explicit
    question_only = prepare_shared_query(request(), lambda req: analysis(
        req, mode="as_of", resolved=date(2022, 1, 2)))
    assert question_only.request.as_of is None
    assert question_only.analysis.resolved_as_of == date(2022, 1, 2)
    assert not prepared.must_early_stop and not question_only.must_early_stop


def test_time_parse_uncertainty_is_not_guessed_or_early_stopped():
    marker = TimeIssue(code="TIME_PARSE_UNCERTAIN", message="Synthetic uncertain interval")
    prepared = prepare_shared_query(request(), lambda req: analysis(req, mode="unknown", issue=marker))
    assert prepared.analysis.time_mode == "unknown"
    assert prepared.analysis.resolved_as_of is None
    assert prepared.analysis.time_issue.code == "TIME_PARSE_UNCERTAIN"
    assert not prepared.must_early_stop
    assert prepared.make_early_response("dynamic") is None
    explicit = prepare_shared_query(request(as_of=date(2023, 5, 1)),
                                    lambda req: analysis(req, mode="unknown", issue=marker))
    assert explicit.request.as_of == date(2023, 5, 1)
    assert explicit.analysis.time_issue.code == "TIME_PARSE_UNCERTAIN"
    assert not explicit.must_early_stop


def test_conflict_builds_same_valid_early_stop_for_three_methods():
    req = request(as_of=date(2023, 5, 1))
    calls = []

    def fake_c_analyzer(given):
        calls.append(given)
        return conflict(given)

    prepared = prepare_shared_query(req, fake_c_analyzer)
    assert prepared.must_early_stop and prepared.error_code is None
    for method in ("full_text", "vector_rag", "dynamic"):
        response = prepared.make_early_response(method)
        assert QueryResponse.model_validate_json(response.model_dump_json()) == response
        assert response.method == method
        assert (response.query_id, response.sample_id, response.question) == (
            req.query_id, req.sample_id, req.question)
        assert response.query_analysis.time_mode == "conflict"
        assert response.query_analysis.time_issue.code == "QUERY_TIME_CONFLICT"
        assert response.query_analysis.time_issue.explicit_as_of == "2023-05-01"
        assert response.query_analysis.time_issue.question_time == "2024-01-02"
        assert response.retrieved is None and response.resolution is None
        answer = response.answer_result
        assert answer.status == "time_uncertain" and answer.answer is None
        assert answer.error is None and answer.uncertainty
        assert answer.used_memory_ids == answer.context_source_refs == answer.evidence_refs == []
        assert "SECRET_GOLD" not in answer.uncertainty
    assert len(calls) == 1


def test_analyzer_failure_and_unavailable_are_sanitized_error_responses():
    def broken(_req):
        raise RuntimeError("SECRET_GOLD and private exception detail")

    for analyzer, code in ((broken, "ANALYZER_FAILURE"), (None, "ANALYZER_UNAVAILABLE")):
        prepared = prepare_shared_query(request(), analyzer)
        assert prepared.must_early_stop and prepared.analysis is None
        for method in ("full_text", "vector_rag", "dynamic"):
            response = prepared.make_early_response(method)
            assert QueryResponse.model_validate_json(response.model_dump_json()) == response
            assert response.answer_result.status == "error"
            assert response.answer_result.error.code == code
            assert response.answer_result.answer is None
            assert response.query_analysis is response.retrieved is response.resolution is None
            assert "SECRET_GOLD" not in response.answer_result.error.message
            assert "SECRET_GOLD" not in repr(prepared)


@pytest.mark.parametrize("factory, code", [
    (lambda req: {"time_mode": "latest_known"}, "ANALYSIS_TYPE_INVALID"),
    (lambda req: analysis(req).model_copy(update={"schema_version": "2.0"}), "ANALYSIS_INVALID"),
    (lambda req: analysis(req).model_copy(update={"time_mode": "bad"}), "ANALYSIS_INVALID"),
    (lambda req: analysis(req).model_copy(update={"query_intent": "bad"}), "ANALYSIS_INVALID"),
    (lambda req: analysis(req).model_copy(update={"time_issue": {"code": 3}}), "ANALYSIS_INVALID"),
    (lambda req: analysis(req).model_copy(update={"query_id": "other_query"}), "ANALYSIS_ID_MISMATCH"),
    (lambda req: analysis(req).model_copy(update={"sample_id": "other_sample"}), "ANALYSIS_ID_MISMATCH"),
])
def test_invalid_or_cross_identity_analysis_is_error(factory, code):
    prepared = prepare_shared_query(request(), factory)
    assert prepared.error_code == code
    assert prepared.analysis is None
    answer = prepared.make_early_response("dynamic").answer_result
    assert answer.status == "error" and answer.error.code == code
    assert "SECRET_GOLD" not in answer.error.message


@pytest.mark.parametrize("factory", [
    lambda req: analysis(req, mode="as_of", resolved=date(2023, 5, 2)),
    lambda req: analysis(req, mode="latest_known"),
    lambda req: analysis(req, mode="unknown"),
    lambda req: analysis(req, mode="conflict", issue=TimeIssue(
        code="QUERY_TIME_CONFLICT", message="Synthetic", explicit_as_of="2020-01-01",
        question_time="2024-01-01")),
    lambda req: conflict(req).model_copy(update={"resolved_as_of": date(2023, 5, 1)}),
])
def test_explicit_time_mismatch_is_error(factory):
    prepared = prepare_shared_query(request(as_of=date(2023, 5, 1)), factory)
    assert prepared.error_code == "TIME_CONDITION_MISMATCH"
    assert prepared.make_early_response("full_text").answer_result.status == "error"


def test_stray_conflict_marker_cannot_continue_normally():
    req = request()
    marker = TimeIssue(code="QUERY_TIME_CONFLICT", message="Synthetic",
                       explicit_as_of="2023-05-01", question_time="2024-01-01")
    prepared = prepare_shared_query(req, lambda given: analysis(given, issue=marker))
    assert prepared.error_code == "TIME_CONDITION_MISMATCH"


def test_bad_request_and_method_fail_without_sensitive_text():
    for bad in (request().model_copy(update={"schema_version": "2.0"}),
                request().model_copy(update={"as_of": "2023-05-01"})):
        with pytest.raises(QueryPreparationError) as exc:
            prepare_shared_query(bad, lambda req: analysis(req))
        assert exc.value.code == "INVALID_QUERY_REQUEST"
        assert "SECRET_GOLD" not in str(exc.value)
    prepared = prepare_shared_query(request(), lambda req: analysis(req))
    with pytest.raises(QueryPreparationError) as exc:
        prepared.make_early_response("unknown_method")
    assert exc.value.code == "INVALID_METHOD"


def test_prepared_snapshots_cannot_be_modified_through_dto_access():
    original = request()
    produced = None

    def fake(req):
        nonlocal produced
        produced = analysis(req)
        req.question = "mutated input"
        return produced

    prepared = prepare_shared_query(original, fake)
    produced.entity_hints.append("mutation")
    original.question = "mutated original"
    exposed = prepared.analysis
    exposed.attribute_hints.append("external mutation")
    exposed.query_intent = "current_state"
    assert prepared.request.question == "Synthetic private question SECRET_GOLD?"
    assert prepared.analysis.query_intent == "historical_state"
    assert prepared.analysis.entity_hints == ["synthetic person"]
    assert prepared.analysis.attribute_hints == ["synthetic job"]


@pytest.mark.parametrize("extra_name", ["qa", "observation", "event_summary"])
def test_model_copy_extra_request_fields_are_rejected_before_serialization(extra_name):
    secret = "SECRET_GOLD_EXTRA_FIELD"
    corrupted = request().model_copy(update={extra_name: {"answer": secret}})
    with pytest.raises(QueryPreparationError) as caught:
        prepare_shared_query(corrupted, lambda req: analysis(req))
    assert caught.value.code == "INVALID_QUERY_REQUEST"
    assert secret not in str(caught.value)


@pytest.mark.parametrize("extra_name", ["qa", "observation", "event_summary"])
def test_model_copy_extra_analysis_fields_are_rejected_before_serialization(extra_name):
    secret = "SECRET_GOLD_EXTRA_FIELD"
    prepared = prepare_shared_query(
        request(), lambda req: analysis(req).model_copy(update={extra_name: {"answer": secret}})
    )
    assert prepared.error_code == "ANALYSIS_INVALID"
    response = prepared.make_early_response("dynamic")
    assert response.answer_result.status == "error"
    assert response.answer_result.error.code == "ANALYSIS_INVALID"
    assert secret not in response.model_dump_json()


def test_model_copy_extra_nested_time_issue_fields_are_rejected():
    secret = "SECRET_GOLD_NESTED_FIELD"
    def fake(req):
        issue = TimeIssue(code="TIME_PARSE_UNCERTAIN", message="Synthetic interval")
        issue = issue.model_copy(update={"qa": {"answer": secret}})
        return analysis(req, mode="unknown", issue=issue)

    prepared = prepare_shared_query(request(), fake)
    assert prepared.error_code == "ANALYSIS_INVALID"
    response = prepared.make_early_response("full_text")
    assert response.answer_result.status == "error"
    assert response.answer_result.error.code == "ANALYSIS_INVALID"
    assert secret not in response.model_dump_json()


def test_model_copy_replaces_nested_time_issue_with_plain_mapping_is_rejected():
    issue = {"code": "TIME_PARSE_UNCERTAIN", "message": "Synthetic interval"}
    broken = prepare_shared_query(
        request(),
        lambda req: analysis(req, mode="unknown").model_copy(update={"time_issue": issue}),
    )
    assert broken.error_code == "ANALYSIS_INVALID"


def test_two_preparations_are_independent_and_runtime_stays_offline():
    one = prepare_shared_query(request(query_id="synthetic_q1"), lambda req: analysis(req, intent="event_fact"))
    two = prepare_shared_query(request(query_id="synthetic_q2"), lambda req: analysis(req, intent="state_history"))
    assert one.analysis.query_id != two.analysis.query_id
    assert one.analysis.query_intent != two.analysis.query_intent
    assert create_runtime(load_settings()).backend == "mock"
