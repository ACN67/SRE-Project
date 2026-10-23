"""A-12 dispatch contracts with explicit synthetic B/C/D/A ports only."""

from __future__ import annotations

from datetime import date

import pytest

from dynamic_memory.contracts import (
    AnswerResult, CandidateItem, CandidateSet, ConversationSample, ConversationSession,
    DatasetBundle, MemoryDecision, MemoryRecord, QueryAnalysis, QueryRequest,
    QueryResponse, RelationEdge, ResolutionResult, SourceRef, SourceTurn, TimeIssue,
)
from dynamic_memory.contracts.memory_id import compute_memory_id
from dynamic_memory.data import SourceRepository
from dynamic_memory.pipeline.query_conditions import QueryPreparationError
from dynamic_memory.pipeline.query_runner import QueryOrchestrator


SAMPLE = "synthetic_sample"
REF = SourceRef(session_id="s1", dia_id="d1")


def source_repository():
    turn = SourceTurn(sample_id=SAMPLE, session_id="s1", dia_id="d1", speaker="Synthetic speaker",
                      text="Synthetic visible text, not a factual answer.")
    sample = ConversationSample(sample_id=SAMPLE, source_order_1_based=1,
                                sessions=[ConversationSession(session_id="s1", turns=[turn])])
    return SourceRepository(DatasetBundle(snapshot_sha256="a" * 64, samples=[sample],
                                          split_by_sample_id={SAMPLE: "main_dev"}))


def memory():
    draft = MemoryRecord(memory_id="mem_" + "0" * 20, sample_id=SAMPLE,
                         content="Synthetic memory only.", fact_kind="event", assertion_type="fact",
                         polarity="positive", source_refs=[REF], extractor_version="synthetic-v1")
    return draft.model_copy(update={"memory_id": compute_memory_id(draft)})


def query(*, as_of=None):
    return QueryRequest(query_id="synthetic_q1", sample_id=SAMPLE,
                        question="Synthetic private question SECRET_GOLD?", as_of=as_of)


def analysis(request):
    return QueryAnalysis(query_id=request.query_id, sample_id=request.sample_id,
                         query_intent="state_history", time_mode="latest_known")


def candidate_set(request, record):
    return CandidateSet(query_id=request.query_id, sample_id=request.sample_id,
                        base_top_k=8, max_candidates=12, retriever_fingerprint="synthetic-index",
                        items=[CandidateItem(memory_id=record.memory_id, rank=1, score=0.8,
                                             source_refs=record.source_refs, origin="semantic")])


def resolved(request, record):
    return ResolutionResult(query_id=request.query_id,
                            decisions=[MemoryDecision(memory_id=record.memory_id, action="RETAIN", reason="Synthetic")],
                            effective_memory_ids=[record.memory_id])


def answered(record):
    return AnswerResult(status="answered", answer="Synthetic answer.", used_memory_ids=[record.memory_id],
                        context_source_refs=[REF], evidence_refs=[REF])


def ports(*, event_log=None, records=None, candidates=None, resolver=None, rag_answer=None,
          dynamic_answer=None, full_answer=None, analyzer=None, index_loader=None):
    log = event_log if event_log is not None else []
    repo = source_repository()
    record = memory()
    loaded = [record] if records is None else records
    index = object()

    def mark(name, result):
        log.append(name)
        return result

    return dict(
        analyze_query=analyzer if analyzer is not None else lambda req: mark("analyze", analysis(req)),
        get_sample=lambda sample_id: mark("get_sample", repo.get_sample(sample_id)),
        load_verified_index=index_loader if index_loader is not None else lambda _sample: mark("verified_index", index),
        retrieve=lambda req, _analysis, got_index: mark("retrieve", candidates if candidates is not None else candidate_set(req, record)),
        load_candidates=lambda _candidates, _index: mark("load_candidates", loaded),
        resolve=resolver if resolver is not None else lambda req, _analysis, _memories: mark("resolve", resolved(req, record)),
        answer_full_text=full_answer if full_answer is not None else lambda _req, _analysis, _sample: mark(
            "full_answer", AnswerResult(status="answered", answer="Synthetic answer.",
                                        context_source_refs=[REF], evidence_refs=[REF])),
        answer_vector_rag=rag_answer if rag_answer is not None else lambda _req, _analysis, _memories: mark("rag_answer", answered(record)),
        answer_dynamic=dynamic_answer if dynamic_answer is not None else lambda _req, _analysis, _resolution, _memories: mark("dynamic_answer", answered(record)),
    )


def assert_valid_response(response, method, status="answered"):
    assert response.method == method
    assert response.query_id == "synthetic_q1" and response.sample_id == SAMPLE
    assert response.answer_result.status == status
    assert response.query_analysis.query_intent == "state_history"
    assert QueryResponse.model_validate_json(response.model_dump_json()) == response


def test_default_dynamic_calls_one_analyzer_and_c_then_d_in_order():
    log = []
    result = QueryOrchestrator(**ports(event_log=log)).run_query(query())
    assert log == ["analyze", "get_sample", "verified_index", "retrieve", "load_candidates", "resolve", "dynamic_answer"]
    assert_valid_response(result, "dynamic")
    assert result.retrieved.items[0].memory_id == result.resolution.effective_memory_ids[0]


def test_full_text_uses_only_source_sample_and_can_run_without_memory_ports():
    log = []
    repo = source_repository()

    def full(req, info, sample):
        log.append("full_answer")
        assert info.query_intent == "state_history"
        assert sample.sample_id == req.sample_id
        return AnswerResult(status="answered", answer="Synthetic answer.",
                            context_source_refs=[REF], evidence_refs=[REF])

    runner = QueryOrchestrator(analyze_query=lambda req: (log.append("analyze"), analysis(req))[1],
                               get_sample=lambda sid: (log.append("get_sample"), repo.get_sample(sid))[1],
                               answer_full_text=full)
    result = runner.run_query(query(), "full_text")
    assert log == ["analyze", "get_sample", "full_answer"]
    assert_valid_response(result, "full_text")
    assert result.retrieved is result.resolution is None
    assert result.answer_result.used_memory_ids == []


def test_vector_rag_shares_candidates_without_d_ports():
    log = []
    all_ports = ports(event_log=log)
    all_ports.pop("resolve")
    all_ports.pop("answer_dynamic")
    result = QueryOrchestrator(**all_ports).run_query(query(), "vector_rag")
    assert log == ["analyze", "get_sample", "verified_index", "retrieve", "load_candidates", "rag_answer"]
    assert_valid_response(result, "vector_rag")
    assert result.retrieved is not None and result.resolution is None


def test_fixed_synthetic_vector_dynamic_candidate_identity_and_budget():
    log = []
    runner = QueryOrchestrator(**ports(event_log=log))
    prepared = runner.prepare_query(query())
    rag = runner.run_prepared(prepared, "vector_rag")
    dynamic = runner.run_prepared(prepared, "dynamic")
    assert rag.retrieved == dynamic.retrieved
    assert rag.retrieved.base_top_k == 8 and rag.retrieved.max_candidates == 12
    assert rag.query_analysis == dynamic.query_analysis
    assert log.count("analyze") == 1


def test_conflict_and_analysis_error_stop_before_source_or_index():
    log = []
    req = query(as_of=date(2023, 5, 1))

    def conflict(given):
        log.append("analyze")
        return QueryAnalysis(query_id=given.query_id, sample_id=given.sample_id,
                             query_intent="historical_state", time_mode="conflict",
                             time_issue=TimeIssue(code="QUERY_TIME_CONFLICT", message="Synthetic conflict",
                                                  explicit_as_of="2023-05-01", question_time="2024-01-01"))

    config = ports(event_log=log, analyzer=conflict)
    for method in ("full_text", "vector_rag", "dynamic"):
        result = QueryOrchestrator(**config).run_query(req, method)
        assert result.answer_result.status == "time_uncertain"
        assert result.retrieved is result.resolution is None
        assert result.answer_result.answer is None
        assert result.query_analysis.time_issue.explicit_as_of == "2023-05-01"
    assert log == ["analyze"] * 3

    def broken(_request):
        raise RuntimeError("SECRET_GOLD private failure")

    result = QueryOrchestrator(**ports(event_log=log, analyzer=broken)).run_query(query(), "dynamic")
    assert result.answer_result.status == "error"
    assert result.answer_result.error.code == "ANALYZER_FAILURE"
    assert result.retrieved is result.resolution is None
    assert "SECRET_GOLD" not in result.answer_result.error.message
    assert log == ["analyze"] * 3


def test_invalid_method_and_request_rejected_before_ports():
    log = []
    runner = QueryOrchestrator(**ports(event_log=log))
    with pytest.raises(QueryPreparationError, match="INVALID_METHOD"):
        runner.run_query(query(), "unknown")
    with pytest.raises(QueryPreparationError, match="INVALID_QUERY_REQUEST"):
        runner.run_query(query().model_copy(update={"schema_version": "2.0"}))
    with pytest.raises(QueryPreparationError, match="INVALID_PREPARED_QUERY"):
        runner.run_prepared(None)
    assert log == []


def test_missing_port_and_unknown_exception_are_sanitized_errors():
    absent = QueryOrchestrator(analyze_query=analysis).run_query(query(), "full_text")
    assert absent.answer_result.error.code == "COMPONENT_UNAVAILABLE"
    assert absent.answer_result.status == "error" and absent.retrieved is None

    def broken(_sample):
        raise RuntimeError("SECRET_GOLD private data")

    result = QueryOrchestrator(**ports(index_loader=broken)).run_query(query(), "dynamic")
    assert result.answer_result.error.code == "COMPONENT_FAILED"
    assert "SECRET_GOLD" not in result.answer_result.error.message


@pytest.mark.parametrize("code", [
    "DATA_NOT_LOADED", "SAMPLE_NOT_FOUND", "ARTIFACT_INCOMPLETE",
    "MEMORY_BUILD_FAILED", "ARTIFACT_STALE", "INDEX_STALE",
])
def test_known_source_build_and_index_errors_preserve_codes(code):
    class KnownFailure(Exception):
        def __init__(self):
            self.code = code

    def broken(_sample):
        raise KnownFailure()

    result = QueryOrchestrator(**ports(index_loader=broken)).run_query(query(), "vector_rag")
    assert result.answer_result.status == "error"
    assert result.answer_result.error.code == code
    assert result.answer_result.answer is None
    assert result.retrieved is result.resolution is None


def test_bad_sample_and_full_text_ref_are_rejected():
    config = ports()
    config["get_sample"] = lambda _sid: source_repository().get_sample(SAMPLE).model_copy(update={"sample_id": "other"})
    assert QueryOrchestrator(**config).run_query(query(), "full_text").answer_result.error.code == "SAMPLE_INVALID"
    bad_ref = SourceRef(session_id="other", dia_id="missing")
    config = ports(full_answer=lambda _req, _analysis, _sample: AnswerResult(
        status="answered", answer="Synthetic", context_source_refs=[bad_ref], evidence_refs=[bad_ref]))
    assert QueryOrchestrator(**config).run_query(query(), "full_text").answer_result.error.code == "RESPONSE_INVALID"


def test_invalid_candidates_and_cross_sample_sources_fail_before_answer():
    record = memory()
    base = candidate_set(query(), record)
    variants = [
        base.model_copy(update={"query_id": "wrong"}),
        base.model_copy(update={"sample_id": "wrong"}),
        base.model_copy(update={"items": [base.items[0], base.items[0]]}),
        base.model_copy(update={"unexpected": "SECRET_GOLD"}),
        CandidateSet(query_id=query().query_id, sample_id=SAMPLE, base_top_k=8, max_candidates=12,
                     retriever_fingerprint="synthetic-index", items=[CandidateItem(
                         memory_id=record.memory_id, rank=1, score=0.8,
                         source_refs=[SourceRef(session_id="other", dia_id="d1")], origin="semantic")]),
    ]
    for bad in variants:
        log = []
        result = QueryOrchestrator(**ports(event_log=log, candidates=bad)).run_query(query(), "vector_rag")
        assert result.answer_result.error.code == "CANDIDATE_INVALID"
        assert "load_candidates" not in log and "rag_answer" not in log


def test_loaded_memories_must_match_rank_identity_and_real_sources():
    record = memory()
    variants = [[], [record.model_copy(update={"sample_id": "other"})],
                [record.model_copy(update={"source_refs": [SourceRef(session_id="other", dia_id="d1")]})],
                [record.model_copy(update={"unexpected": "SECRET_GOLD"})]]
    for bad in variants:
        result = QueryOrchestrator(**ports(records=bad)).run_query(query(), "vector_rag")
        assert result.answer_result.error.code == "CANDIDATE_INVALID"


def test_bad_resolution_coverage_and_outside_edge_stop_before_dynamic_answer():
    record = memory()
    incomplete = ResolutionResult(query_id=query().query_id, decisions=[], effective_memory_ids=[])
    outside = ResolutionResult(query_id=query().query_id,
                               decisions=[MemoryDecision(memory_id=record.memory_id, action="RETAIN", reason="Synthetic")],
                               effective_memory_ids=[record.memory_id],
                               relations=[RelationEdge(from_memory_id=record.memory_id, to_memory_id="outside",
                                                       relation="UPDATE", scope_attribute="synthetic", reason="Synthetic")])
    for bad in (incomplete, outside, resolved(query(), record).model_copy(update={"unexpected": "SECRET_GOLD"})):
        log = []
        result = QueryOrchestrator(**ports(event_log=log, resolver=lambda _req, _a, _m: (log.append("resolve"), bad)[1])).run_query(query())
        assert result.answer_result.error.code == "RESOLUTION_INVALID"
        assert "dynamic_answer" not in log


def test_resolution_relation_evidence_must_be_in_loaded_candidate_sources():
    first = memory()
    second = first.model_copy(update={"content": "Another synthetic memory."})
    second = second.model_copy(update={"memory_id": compute_memory_id(second)})
    candidates = CandidateSet(query_id=query().query_id, sample_id=SAMPLE, base_top_k=8,
                              max_candidates=12, retriever_fingerprint="synthetic-index",
                              items=[CandidateItem(memory_id=record.memory_id, rank=rank, score=0.8,
                                                   source_refs=record.source_refs, origin="semantic")
                                     for rank, record in enumerate((first, second), 1)])
    resolution = ResolutionResult(query_id=query().query_id,
                                  decisions=[MemoryDecision(memory_id=record.memory_id, action="RETAIN", reason="Synthetic")
                                             for record in (first, second)],
                                  effective_memory_ids=[first.memory_id, second.memory_id],
                                  relations=[RelationEdge(from_memory_id=first.memory_id, to_memory_id=second.memory_id,
                                                          relation="UPDATE", scope_attribute="synthetic", reason="Synthetic",
                                                          evidence_refs=[SourceRef(session_id="other", dia_id="missing")])])
    result = QueryOrchestrator(**ports(records=[first, second], candidates=candidates,
                                        resolver=lambda _r, _a, _m: resolution)).run_query(query())
    assert result.answer_result.error.code == "RESOLUTION_INVALID"


def test_dynamic_excluded_memory_cannot_be_used_or_cited():
    record = memory()
    excluded = ResolutionResult(query_id=query().query_id,
                                decisions=[MemoryDecision(memory_id=record.memory_id, action="EXCLUDE", reason="Synthetic")])
    result = QueryOrchestrator(**ports(resolver=lambda _req, _a, _m: excluded)).run_query(query())
    assert result.answer_result.error.code == "ANSWER_INVALID"


def test_answer_type_status_and_source_are_checked():
    bads = ["not an AnswerResult", AnswerResult(status="answered", answer="Synthetic",
                                                 used_memory_ids=["outside"], context_source_refs=[REF], evidence_refs=[REF]),
            AnswerResult(status="answered", answer="Synthetic", used_memory_ids=[memory().memory_id],
                         context_source_refs=[SourceRef(session_id="other", dia_id="d1")],
                         evidence_refs=[SourceRef(session_id="other", dia_id="d1")]),
            answered(memory()).model_copy(update={"status": "invalid"}),
            answered(memory()).model_copy(update={"unexpected": "SECRET_GOLD"})]
    for bad in bads:
        result = QueryOrchestrator(**ports(rag_answer=lambda _req, _a, _m: bad)).run_query(query(), "vector_rag")
        assert result.answer_result.status == "error"
        assert result.answer_result.error.code in {"ANSWER_INVALID", "RESPONSE_INVALID"}


def test_downstream_error_answer_does_not_echo_private_message():
    from dynamic_memory.contracts import ErrorInfo

    bad = AnswerResult(status="error", error=ErrorInfo(code="MODEL_FAILED", message="SECRET_GOLD raw failure"))
    result = QueryOrchestrator(**ports(rag_answer=lambda _req, _a, _m: bad)).run_query(query(), "vector_rag")
    assert result.answer_result.error.code == "COMPONENT_FAILED"
    assert "SECRET_GOLD" not in result.answer_result.error.message
    malformed = AnswerResult(status="no_evidence", uncertainty="Synthetic uncertain",
                             error=ErrorInfo(code="MODEL_FAILED", message="SECRET_GOLD raw failure"))
    result = QueryOrchestrator(**ports(rag_answer=lambda _req, _a, _m: malformed)).run_query(query(), "vector_rag")
    assert result.answer_result.error.code == "ANSWER_INVALID"
    assert "SECRET_GOLD" not in result.answer_result.error.message


def test_empty_candidates_have_real_candidate_set_and_correct_status():
    empty = CandidateSet(query_id=query().query_id, sample_id=SAMPLE,
                         base_top_k=8, max_candidates=12, retriever_fingerprint="synthetic-index", items=[])
    for method in ("vector_rag", "dynamic"):
        log = []
        resolver = lambda req, _a, _m: (log.append("resolve"), ResolutionResult(query_id=req.query_id))[1]
        result = QueryOrchestrator(**ports(event_log=log, candidates=empty, records=[], resolver=resolver)).run_query(query(), method)
        assert_valid_response(result, method, "no_evidence")
        assert result.retrieved.items == []
        assert result.resolution is None if method == "vector_rag" else result.resolution is not None
        assert "rag_answer" not in log and "dynamic_answer" not in log
        assert ("resolve" in log) == (method == "dynamic")


def test_business_uncertainty_statuses_are_not_collapsed_to_no_evidence():
    for status in ("time_uncertain", "unresolved_conflict"):
        special = AnswerResult(status=status, uncertainty="Synthetic evidence is uncertain.",
                               context_source_refs=[REF], evidence_refs=[REF])
        result = QueryOrchestrator(**ports(dynamic_answer=lambda _req, _a, _r, _m: special)).run_query(query())
        assert result.answer_result.status == status
        assert result.answer_result.error is None


def test_two_instances_do_not_share_ports_or_state():
    first_log, second_log = [], []
    first = QueryOrchestrator(**ports(event_log=first_log))
    second = QueryOrchestrator(**ports(event_log=second_log))
    first.run_query(query(), "full_text")
    assert first_log == ["analyze", "get_sample", "full_answer"]
    assert second_log == []
    second.run_query(query(), "vector_rag")
    assert second_log == ["analyze", "get_sample", "verified_index", "retrieve", "load_candidates", "rag_answer"]
