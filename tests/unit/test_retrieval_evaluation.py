"""Hand-computable retrieval evaluation; no official Gold or answer model."""

import pytest

from dynamic_memory.retrieval.evaluation import RetrievalEvaluationCase, evaluate_retrieval
from test_history_expansion import history_artifact, run


def prepared():
    source = history_artifact()
    _, retriever, index, encoder, request = run(source)
    case = RetrievalEvaluationCase(case_id="synthetic-history", label_kind="synthetic_fixture", split="main_dev",
        request=request, source_manifest_fingerprint=index.source_manifest_fingerprint,
        relevant_memory_ids=[m.memory_id for m in source.memories[:3]])
    return case, retriever, index, encoder


def evaluate(case, retriever, index):
    return evaluate_retrieval([case], indexes={index.sample_id: index}, retriever=retriever)


def test_known_history_gain_and_budget_limited_recall():
    case, retriever, index, encoder = prepared()
    before = encoder.query_calls
    report = evaluate(case, retriever, index)
    assert encoder.query_calls == before + 1
    assert report["summary"]["pure_vector"]["micro_recall"] == pytest.approx(1 / 3)
    assert report["summary"]["final_candidates"]["micro_recall"] == 1
    row = report["queries"][0]
    assert row["pure_vector"]["recall_at_k"]["10"] is None  # Cannot call Top-8 Recall@10.
    assert row["final_candidates"]["recall_at_k"]["5"] == pytest.approx(1 / 3)
    assert row["elapsed_ms"] >= 0
    assert "question" not in row and "answer" not in row
    assert report["official_qa_score"] is None


def test_empty_labels_are_na_not_perfect_recall():
    case, retriever, index, _ = prepared()
    case.relevant_memory_ids = []
    report = evaluate(case, retriever, index)
    assert report["summary"]["scored_query_count"] == 0
    assert report["summary"]["no_relevant_label_query_count"] == 1
    assert report["summary"]["pure_vector"]["macro_recall"] is None


@pytest.mark.parametrize("bad", ["stale", "unknown_id", "duplicate_ids", "unreviewed", "test_split"])
def test_invalid_labels_fail_before_retrieval(bad):
    case, retriever, index, encoder = prepared()
    if bad == "stale": case.source_manifest_fingerprint = "sha256:" + "0" * 64
    if bad == "unknown_id": case.relevant_memory_ids = ["mem_" + "0" * 20]
    if bad == "duplicate_ids": case.relevant_memory_ids *= 2
    if bad == "unreviewed": case.label_kind = "team_annotated"
    if bad == "test_split": case.split = "test"
    before = encoder.query_calls
    with pytest.raises(ValueError): evaluate(case, retriever, index)
    assert encoder.query_calls == before


def test_mixed_labels_or_duplicate_cases_are_rejected():
    case, retriever, index, _ = prepared()
    other = case.model_copy(update={"case_id": "team", "label_kind": "team_annotated", "annotator": "one", "reviewer": "two"})
    for cases in ([case, other], [case, case], []):
        with pytest.raises(ValueError):
            evaluate_retrieval(cases, indexes={index.sample_id: index}, retriever=retriever)


def test_aggregate_micro_macro_have_explicit_denominators():
    case, retriever, index, _ = prepared()
    other = case.model_copy(update={"case_id": "single-label", "relevant_memory_ids": case.relevant_memory_ids[:1]})
    report = evaluate_retrieval([case, other], indexes={index.sample_id: index}, retriever=retriever)
    pure = report["summary"]["pure_vector"]
    assert pure["hits"] == 2 and pure["relevant_count"] == 4
    assert pure["micro_recall"] == 0.5
    assert pure["macro_recall"] == pytest.approx(2 / 3)
