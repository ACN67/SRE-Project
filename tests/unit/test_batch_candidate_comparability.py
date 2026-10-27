"""Observed final candidate comparison, including two independent retrieves."""

from dataclasses import replace
from uuid import uuid4

import pytest

from dynamic_memory.contracts import (AnswerResult, CandidateItem, CandidateSet,
    ErrorInfo, MemoryDecision, QueryResponse, ResolutionResult, SourceRef)
from dynamic_memory.contracts.memory_id import compute_memory_id
from dynamic_memory.pipeline.query_runner import QueryOrchestrator
from evaluation.runners import RunProtocol, run_three_method_batch, verify_run
from evaluation.runners.three_method_runner import compare_candidates
from test_run_query_dispatch import SAMPLE, memory, ports
from test_three_method_batch_runner import setup


def pair():
    a = CandidateItem(memory_id="m1", rank=1, score=.9,
                      source_refs=[SourceRef(session_id="s", dia_id="d1")], origin="semantic")
    b = CandidateItem(memory_id="m2", rank=2, score=.8,
                      source_refs=[SourceRef(session_id="s", dia_id="d2")], origin="history_expansion")
    candidates = CandidateSet(query_id="q", sample_id=SAMPLE, base_top_k=8,
                              max_candidates=12, retriever_fingerprint="synthetic-index", items=[a, b])
    answer = AnswerResult(status="no_evidence", uncertainty="synthetic")
    vector = QueryResponse(query_id="q", sample_id=SAMPLE, question="Synthetic?", method="vector_rag",
                           query_analysis=None, retrieved=candidates, resolution=None, answer_result=answer)
    dynamic = QueryResponse(query_id="q", sample_id=SAMPLE, question="Synthetic?", method="dynamic",
                            query_analysis=None, retrieved=candidates,
                            resolution=ResolutionResult(query_id="q"), answer_result=answer)
    protocol = RunProtocol("synthetic", "a" * 64, ("main_dev",), "prompt", "model", "settings",
                           "synthetic-index", "index", "latest_known", 8, 12)
    return vector, dynamic, protocol


@pytest.mark.parametrize("change", ["score", "source", "rank_order", "fingerprint", "budget", "origin"])
def test_same_fingerprint_cannot_hide_candidate_difference(change):
    vector, dynamic, protocol = pair()
    items = list(dynamic.retrieved.items)
    if change == "score":
        items[0] = items[0].model_copy(update={"score": .7})
    elif change == "source":
        items[0] = items[0].model_copy(update={"source_refs": [SourceRef(session_id="s", dia_id="other")]})
    elif change == "rank_order":
        items = [items[1].model_copy(update={"rank": 1}), items[0].model_copy(update={"rank": 2})]
    elif change == "origin":
        items[0] = items[0].model_copy(update={"origin": "history_expansion"})
    changes = {"items": items}
    if change == "fingerprint":
        changes["retriever_fingerprint"] = "different"
    if change == "budget":
        changes["base_top_k"] = 7
    changed = dynamic.model_copy(update={"retrieved": dynamic.retrieved.model_copy(update=changes)})
    assert compare_candidates(vector, changed, protocol).status == "mismatch"


def test_equal_missing_and_unverified():
    vector, dynamic, protocol = pair()
    assert compare_candidates(vector, dynamic, protocol).status == "identical"
    assert compare_candidates(vector, dynamic, replace(protocol, model_fingerprint="unknown/unverified")).status == "unverified"
    missing = dynamic.model_copy(update={"retrieved": None, "resolution": None,
        "answer_result": AnswerResult(status="error", error=ErrorInfo(code="FAILED", message="synthetic"))})
    assert compare_candidates(vector, missing, protocol).status == "not_comparable"


def test_actual_two_retrieves_return_different_memory_and_block_comparable_run(tmp_path):
    manifest, protocol = setup(1, run_id=f"different-{uuid4().hex}")
    first = memory()
    draft = first.model_copy(update={"content": "A different synthetic memory."})
    second = draft.model_copy(update={"memory_id": compute_memory_id(draft)})
    all_memories = {first.memory_id: first, second.memory_id: second}
    calls = []
    config = ports()
    def retrieve(request, _analysis, _index):
        record = first if not calls else second
        calls.append(record.memory_id)
        return CandidateSet(query_id=request.query_id, sample_id=request.sample_id,
            base_top_k=8, max_candidates=12, retriever_fingerprint="synthetic-index",
            items=[CandidateItem(memory_id=record.memory_id, rank=1, score=.8,
                                 source_refs=record.source_refs, origin="semantic")])
    config["retrieve"] = retrieve
    config["load_candidates"] = lambda candidates, _index: [all_memories[item.memory_id] for item in candidates.items]
    config["resolve"] = lambda request, _analysis, memories: ResolutionResult(query_id=request.query_id,
        decisions=[MemoryDecision(memory_id=m.memory_id, action="RETAIN", reason="synthetic") for m in memories],
        effective_memory_ids=[m.memory_id for m in memories])
    config["answer_vector_rag"] = lambda *_: AnswerResult(status="no_evidence", uncertainty="synthetic")
    config["answer_dynamic"] = lambda *_: AnswerResult(status="no_evidence", uncertainty="synthetic")
    result = run_three_method_batch(manifest, QueryOrchestrator(**config), protocol, results_root=tmp_path)
    assert calls == [first.memory_id, second.memory_id]
    assert result.comparisons[0].status == "mismatch"
    assert result.status == "complete_not_comparable"
    assert verify_run(result.directory, manifest).status == "complete_not_comparable"
