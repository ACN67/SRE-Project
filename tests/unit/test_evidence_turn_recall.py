"""Synthetic source-verified turn recall; no course Gold or provider calls."""

import json
from dataclasses import replace

import pytest

from dynamic_memory.contracts import (AnswerResult, CandidateItem, CandidateSet,
                                      ConversationSample, ConversationSession, DatasetBundle,
                                      ErrorInfo, QueryResponse, SourceRef, SourceTurn)
from dynamic_memory.data.repository import SourceRepository
from evaluation.gold_loader import GoldLoadResult, GoldRecord
from evaluation.metrics.evidence_recall import evaluate_saved_evidence, score_evidence_turns
from evaluation.question_manifest import EvaluationInputError, QuestionManifest, QuestionRecord
from test_qa_score_summary import scored_fixture


def fixture():
    sample = "synthetic-a"
    refs = [SourceRef(session_id="s", dia_id=f"d{i}") for i in range(1, 4)]
    repo = SourceRepository(DatasetBundle(snapshot_sha256="a" * 64,
        samples=[ConversationSample(sample_id=sample, source_order_1_based=1,
            sessions=[ConversationSession(session_id="s", turns=[SourceTurn(
                sample_id=sample, session_id="s", dia_id=f"d{i}", speaker="Synthetic", text="synthetic")
                for i in range(1, 4)]), ConversationSession(session_id="s2", turns=[SourceTurn(
                    sample_id=sample, session_id="s2", dia_id="d1", speaker="Synthetic", text="synthetic")])])],
        split_by_sample_id={sample: "main_dev"}))
    q = QuestionRecord(sample, "q1", 0, "Synthetic?", "key", "main_dev")
    manifest = QuestionManifest("a" * 64, (q,), ("main_dev",), None)
    gold = GoldRecord(sample, "q1", "key", 0, "synthetic", json.dumps([
        {"session_id": "s", "dia_id": "d1"}, {"session_id": "s", "dia_id": "d2"},
        {"session_id": "s", "dia_id": "d2"}]), 2, "synthetic_fixture")
    items = [CandidateItem(memory_id=f"m{i}", rank=i, score=1.0 / i,
                           source_refs=[refs[0], refs[2]] if i == 1 else [refs[0]] if i < 6 else [refs[1]],
                           origin="semantic") for i in range(1, 7)]
    candidates = CandidateSet(query_id="q1", sample_id=sample, base_top_k=5,
                              max_candidates=10, retriever_fingerprint="synthetic-fp", items=items)
    response = QueryResponse(query_id="q1", sample_id=sample, question="Synthetic?", method="vector_rag",
                             query_analysis=None, retrieved=candidates, resolution=None,
                             answer_result=AnswerResult(status="no_evidence", uncertainty="synthetic"))
    return manifest, response, GoldLoadResult((gold,), ()), repo, refs


def score(manifest, response, gold, repo):
    return score_evidence_turns(manifest, (response,), gold, repo, response.method)


def test_ranked_memory_k_and_deduplicated_turn_units():
    manifest, response, gold, repo, _ = fixture()
    report = score(manifest, response, gold, repo)
    a, b = report.questions
    assert (a.k, a.top_k_memory_count, a.retrieved_turn_count, a.gold_turn_count,
            a.matched_turn_count, a.evidence_turn_recall) == (5, 5, 2, 2, 1, .5)
    assert (b.k, b.top_k_memory_count, b.retrieved_turn_count,
            b.matched_turn_count, b.evidence_turn_recall) == (10, 6, 3, 2, 1)
    assert report.by_method[0].main_mean == .5 and report.by_method[1].main_mean == 1
    assert a.retriever_fingerprint == "synthetic-fp"


@pytest.mark.parametrize("evidence,status", [
    ("[]", "gold_empty"), ('["D1:3"]', "source_invalid"),
    ('["d1"]', "session_ambiguous"), ('[123]', "evidence_unrecognized"),
    ('[{"session_id":"s","dia_id":"absent"}]', "source_invalid"),
    ('[{"session_id":"other","dia_id":"d1"}]', "source_invalid"),
])
def test_gold_shape_empty_and_source_failures(evidence, status):
    manifest, response, gold, repo, _ = fixture()
    altered = GoldLoadResult((replace(gold.records[0], evidence_json=evidence),), ())
    assert [r.status for r in score(manifest, response, altered, repo).questions] == [status, status]


def test_empty_candidates_missing_retrieval_and_full_text():
    manifest, response, gold, repo, _ = fixture()
    empty = response.model_copy(update={"retrieved": response.retrieved.model_copy(update={"items": []})})
    assert [(r.status, r.evidence_turn_recall, r.top_k_memory_count) for r in score(manifest, empty, gold, repo).questions] == [
        ("ready", 0, 0), ("ready", 0, 0)]
    failed = response.model_copy(update={"retrieved": None, "answer_result": AnswerResult(
        status="error", error=ErrorInfo(code="MODEL_FAILED", message="synthetic"))})
    assert all(r.status == "model_error" and r.evidence_turn_recall is None for r in score(manifest, failed, gold, repo).questions)
    full = response.model_copy(update={"method": "full_text", "retrieved": None})
    assert all(r.status == "not_applicable" for r in score(manifest, full, gold, repo).questions)
    missing = GoldLoadResult((), ("key",))
    assert all(r.status == "gold_missing" for r in score(manifest, response, missing, repo).questions)


def test_candidate_source_must_exist():
    manifest, response, gold, repo, _ = fixture()
    bad = response.retrieved.items[0].model_copy(update={"source_refs": [SourceRef(session_id="s", dia_id="absent")]})
    candidates = response.retrieved.model_copy(update={"items": [bad, *response.retrieved.items[1:]]})
    changed = response.model_copy(update={"retrieved": candidates})
    assert all(r.status == "candidate_source_invalid" for r in score(manifest, changed, gold, repo).questions)


def test_unique_string_dia_id_is_resolved_by_actual_sample():
    manifest, response, gold, repo, _ = fixture()
    altered = GoldLoadResult((replace(gold.records[0], evidence_json='["d2"]'),), ())
    assert [r.evidence_turn_recall for r in score(manifest, response, altered, repo).questions] == [0, 1]


def test_saved_entry_rejects_changed_prediction_before_gold_open(tmp_path):
    source, split, manifest, path, receipt, _ = scored_fixture(tmp_path)
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(EvaluationInputError, match="PREDICTION_HASH_MISMATCH"):
        evaluate_saved_evidence(path, manifest, receipt, tmp_path / "absent-gold.json", split,
                                SourceRepository(), annotation_source="synthetic_fixture")


def test_source_repository_snapshot_must_match_manifest():
    manifest, response, gold, repo, _ = fixture()
    assert repo.snapshot_sha256 == manifest.snapshot_sha256
    with pytest.raises(AttributeError):
        repo.snapshot_sha256 = "b" * 64
    other = SourceRepository(DatasetBundle(
        snapshot_sha256="b" * 64,
        samples=[repo.get_sample(response.sample_id)],
        split_by_sample_id={response.sample_id: "main_dev"},
    ))
    with pytest.raises(EvaluationInputError, match="EVIDENCE_SNAPSHOT_MISMATCH"):
        score_evidence_turns(manifest, (response,), gold, other, response.method)
    with pytest.raises(EvaluationInputError, match="EVIDENCE_SOURCE_UNAVAILABLE"):
        score_evidence_turns(manifest, (response,), gold, SourceRepository(), response.method)


def test_saved_entry_checks_source_snapshot_before_opening_gold(tmp_path):
    _, split, manifest, path, receipt, _ = scored_fixture(tmp_path)
    _, _, _, repo, _ = fixture()
    assert repo.snapshot_sha256 != manifest.snapshot_sha256
    with pytest.raises(EvaluationInputError, match="EVIDENCE_SNAPSHOT_MISMATCH"):
        evaluate_saved_evidence(path, manifest, receipt, tmp_path / "absent-gold.json", split,
                                repo, annotation_source="synthetic_fixture")
