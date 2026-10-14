from datetime import datetime

import pytest
from pydantic import ValidationError

from dynamic_memory.contracts.memory_id import compute_memory_id, validate_memory_id
from dynamic_memory.contracts.models import (
    AnswerResult, CandidateItem, CandidateSet, MemoryDecision, MemoryRecord,
    QueryAnalysis, QueryRequest, QueryResponse, RelationEdge,
    ResolutionResult, SourceRef, SourceTurn, TimeSpan, render_visible_turn,
)
from dynamic_memory.contracts.validation import validate_resolution_against_candidates, validate_response_context


REF = SourceRef(session_id="s1", dia_id="d1")


def memory():
    payload = dict(memory_id="mem_00000000000000000000", sample_id="synthetic_001", content="Alex opened a shop.", entity="Alex", attribute="shop", value="opened", fact_kind="event", assertion_type="fact", polarity="positive", source_refs=[REF], extractor_version="synthetic")
    draft = MemoryRecord(**payload)
    payload["memory_id"] = compute_memory_id(draft)
    return MemoryRecord(**payload)


def candidate_set(item=True):
    m = memory()
    return CandidateSet(query_id="q1", sample_id=m.sample_id, base_top_k=8, max_candidates=12, retriever_fingerprint="synthetic", items=[CandidateItem(memory_id=m.memory_id, rank=1, score=0.5, source_refs=m.source_refs, origin="semantic")] if item else [])


def analysis(intent="event_fact"):
    return QueryAnalysis(query_id="q1", sample_id="synthetic_001", query_intent=intent, time_mode="latest_known")


def response(method, answer, retrieved=None, resolution=None):
    return QueryResponse(query_id="q1", sample_id="synthetic_001", question="When?", method=method, query_analysis=analysis(), retrieved=retrieved, resolution=resolution, answer_result=answer)


def test_version_and_intent_time_independence():
    for intent in ("current_state", "historical_state", "state_history", "event_fact", "general_fact", "unknown"):
        assert analysis(intent).time_mode == "latest_known"
    with pytest.raises(ValidationError):
        QueryAnalysis(query_id="q1", sample_id="s", time_mode="latest_known")
    with pytest.raises(ValidationError):
        QueryRequest(schema_version="2.0", query_id="q1", sample_id="s", question="x")
    with pytest.raises(ValidationError):
        QueryAnalysis(query_id="q1", sample_id="s", query_intent="invalid", time_mode="latest_known")
    with pytest.raises(ValidationError):
        QueryAnalysis(query_id="q1", sample_id="s", query_intent="unknown", time_mode="conflict")


def test_time_and_caption():
    with pytest.raises(ValidationError):
        TimeSpan(start=datetime(2023, 1, 2), end_exclusive=datetime(2023, 1, 1), precision="day", resolution="explicit")
    turn = SourceTurn(sample_id="synthetic_001", session_id="s1", dia_id="d1", speaker="Alex", text="original", image_caption="an image", caption_origin="blip_caption")
    assert turn.text == "original"
    assert render_visible_turn(turn) == "original [image_caption: an image]"
    with pytest.raises(ValidationError):
        SourceTurn(sample_id="s", session_id="a", dia_id="b", speaker="x", text="x", image_caption="caption")


def test_memory_id_deterministic_and_gold_absent():
    m = memory()
    validate_memory_id(m)
    assert compute_memory_id(m) == m.memory_id
    assert "gold" not in str(m.model_dump()).lower()
    assert "gold" not in str(QueryRequest.model_fields).lower()
    modified = m.model_copy(update={"content": "Different fact."})
    assert compute_memory_id(modified) != m.memory_id
    with pytest.raises(ValueError):
        validate_memory_id(modified)


def test_candidate_rank_duplicates_and_budget():
    item = candidate_set().items[0]
    for items, budget in [([item, item], 12), ([item.model_copy(update={"rank": 2})], 12), ([item], 0)]:
        with pytest.raises(ValidationError):
            CandidateSet(query_id="q1", sample_id="synthetic_001", base_top_k=1, max_candidates=budget, retriever_fingerprint="synthetic", items=items)


def test_resolution_and_candidate_context():
    c = candidate_set()
    mid = c.items[0].memory_id
    r = ResolutionResult(query_id="q1", decisions=[MemoryDecision(memory_id=mid, action="RETAIN", reason="synthetic")], effective_memory_ids=[mid])
    validate_resolution_against_candidates(r, c)
    with pytest.raises(ValidationError):
        ResolutionResult(query_id="q1", decisions=r.decisions, effective_memory_ids=[])
    with pytest.raises(ValueError):
        validate_resolution_against_candidates(ResolutionResult(query_id="q1", decisions=[]), c)
    outside = RelationEdge(from_memory_id=mid, to_memory_id="other", relation="UPDATE", scope_attribute="shop", reason="synthetic")
    with pytest.raises(ValueError):
        validate_resolution_against_candidates(r.model_copy(update={"relations": [outside]}), c)


def test_three_methods_and_real_context_reference_check():
    c = candidate_set()
    m = memory()
    turn = SourceTurn(sample_id=m.sample_id, session_id="s1", dia_id="d1", speaker="Alex", text="Alex opened a shop.")
    no = AnswerResult(status="no_evidence", uncertainty="synthetic: no generated answer")
    assert response("full_text", no).retrieved is None
    assert response("vector_rag", no, c).resolution is None
    r = ResolutionResult(query_id="q1", decisions=[MemoryDecision(memory_id=m.memory_id, action="RETAIN", reason="synthetic")], effective_memory_ids=[m.memory_id])
    assert response("dynamic", no, c, r).resolution is not None
    with pytest.raises(ValidationError):
        response("vector_rag", no)
    with pytest.raises(ValidationError):
        response("full_text", no, c)
    answered = AnswerResult(status="answered", answer="A shop.", used_memory_ids=[m.memory_id], context_source_refs=[REF], evidence_refs=[REF])
    valid = response("dynamic", answered, c, r)
    validate_response_context(valid, [m], [turn])
    with pytest.raises(ValueError):
        validate_response_context(valid, [m], [])
    with pytest.raises(ValidationError):
        AnswerResult(status="answered", answer="A shop.", used_memory_ids=[m.memory_id], evidence_refs=[REF])


def test_time_conflict_early_stop():
    a = QueryAnalysis(query_id="q1", sample_id="synthetic_001", query_intent="historical_state", time_mode="conflict", time_issue={"code": "QUERY_TIME_CONFLICT", "message": "different dates", "explicit_as_of": "2023-01-01", "question_time": "2023-02-01"})
    response = QueryResponse(query_id="q1", sample_id="synthetic_001", question="Where?", method="dynamic", query_analysis=a, retrieved=None, resolution=None, answer_result=AnswerResult(status="time_uncertain", uncertainty="conflicting dates"))
    assert response.answer_result.status == "time_uncertain"
