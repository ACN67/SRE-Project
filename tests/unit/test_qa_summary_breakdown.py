"""Synthetic QA aggregate and persisted scoring artifact checks."""

import hashlib
import json
import math
from dataclasses import replace

import pytest

from evaluation.metrics.qa_summary import QuestionScore, QAReport, summarize_questions
from evaluation.metrics.qa_scoring_manifest import (save_qa_scoring_manifest,
                                                     verify_qa_scoring_manifest)
from evaluation.question_manifest import EvaluationInputError
from test_qa_score_summary import scored_fixture
from evaluation.metrics.qa_summary import evaluate_saved_qa


def row(query, *, category=2, sample="s1", status="answered", score=1.0,
        f1=1.0, missing=False, failure=False):
    return QuestionScore(sample, query, query, "full_text", None if missing else category,
                         status, None, None if failure else "synthetic", failure, missing,
                         None if missing else score, None if missing else "synthetic_fixture",
                         None if missing else f1)


def test_empty_and_zero_denominator():
    summary = summarize_questions(())
    assert summary.total_predictions == 0
    assert len(summary.by_category) == 5
    assert summary.by_sample == summary.by_category_sample == ()
    assert all(group.main_mean is None and group.text_f1_main_mean is None
               for group in summary.by_category)


@pytest.mark.parametrize("score,f1", [(1.0, 1.0), (0.0, 0.0)])
def test_single_correct_or_wrong(score, f1):
    summary = summarize_questions((row("q", score=score, f1=f1),))
    assert (summary.gold_scored, summary.conditional_scored) == (1, 1)
    assert summary.main_mean == summary.conditional_mean == score
    assert summary.text_f1_main_mean == summary.text_f1_conditional_mean == f1


def test_unknown_category_sample_and_reason_priority():
    values = (row("a", missing=True, sample="", status="time_uncertain"),
              row("b", missing=True, sample=None, status="error", failure=True))
    summary = summarize_questions(values)
    assert dict(summary.reason_counts) == dict(execution_failure=1, missing_gold=1,
        no_evidence=0, time_uncertain=0, other=0)
    assert dict((g.key, g.total_predictions) for g in summary.by_category)["unknown"] == 2
    assert summary.by_sample[0].key == "unknown" and summary.by_sample[0].total_predictions == 2
    assert summary.by_category_sample[0].total_predictions == 2
    assert summary.text_f1_main_mean is None


@pytest.mark.parametrize("status,expected", [("no_evidence", "no_evidence"),
                          ("time_uncertain", "time_uncertain"),
                          ("unresolved_conflict", "other"), ("future_status", "other")])
def test_status_distribution(status, expected):
    summary = summarize_questions((row("q", status=status),))
    assert dict(summary.reason_counts)[expected] == 1
    assert sum(dict(summary.reason_counts).values()) == 1
    if expected == "other":
        assert dict(summary.other_status_counts) == {status: 1}


def test_manual_multisample_multicategory_fixture():
    values = (row("a", category=1, sample="s1"),
              row("b", category=1, sample="s2", score=0, f1=0),
              row("c", category=2, sample="s1", missing=True),
              row("d", category=2, sample="s2", status="error", failure=True, score=0, f1=0),
              row("e", category=3, sample="s1", status="no_evidence", score=0, f1=0),
              row("f", category=4, sample="s2", status="time_uncertain", score=0, f1=0))
    summary = summarize_questions(values)
    assert (summary.total_predictions, summary.gold_scored, summary.gold_missing,
            summary.execution_failed, summary.conditional_scored) == (6, 5, 1, 1, 4)
    assert summary.main_mean == pytest.approx(1/5)
    assert summary.conditional_mean == pytest.approx(1/4)
    assert dict(summary.reason_counts) == dict(execution_failure=1, missing_gold=1,
        no_evidence=1, time_uncertain=1, other=2)
    assert sum(g.total_predictions for g in summary.by_category) == 6
    assert sum(g.total_predictions for g in summary.by_sample) == 6
    assert sum(g.total_predictions for g in summary.by_category_sample) == 6
    assert next(g for g in summary.by_category if g.key == "5").main_mean is None
    assert next(g for g in summary.by_category_sample if g.key == "1|s1").conditional_mean == 1


def test_saved_scoring_and_distinct_units(tmp_path):
    source, split, manifest, prediction, receipt, _ = scored_fixture(tmp_path)
    report = evaluate_saved_qa(prediction, manifest, receipt, source, split,
                               annotation_source="synthetic_fixture")
    summary = report.summary
    assert summary.main_mean == pytest.approx(2/3)
    assert summary.text_f1_main_mean == pytest.approx(10/21)
    assert (summary.text_f1_scored, summary.text_f1_conditional_scored) == (7, 6)
    assert dict(summary.reason_counts) == dict(execution_failure=1, missing_gold=1,
        no_evidence=1, time_uncertain=0, other=5)
    assert dict(summary.other_status_counts) == {"answered": 4, "unresolved_conflict": 1}
    path = tmp_path / "qa_scoring_manifest.json"
    saved = save_qa_scoring_manifest(path, prediction, manifest, receipt, report,
                                     annotation_source="synthetic_fixture")
    first = path.read_bytes()
    assert hashlib.sha256(first).hexdigest() == saved.file_sha256
    assert b"SECRET_GOLD" not in first
    loaded = verify_qa_scoring_manifest(path, prediction, manifest, receipt, report,
                                         annotation_source="synthetic_fixture")
    assert loaded == saved
    data = json.loads(first)
    assert data["total_predictions"] == 8
    assert data["official_qa_summary"]["main_denominator"] == 7
    assert data["text_f1_summary"]["main_denominator"] == 7
    save_qa_scoring_manifest(path, prediction, manifest, receipt, report,
                             annotation_source="synthetic_fixture")
    assert path.read_bytes() == first


def test_rehashed_tampering_and_identity_mismatch(tmp_path):
    source, split, manifest, prediction, receipt, _ = scored_fixture(tmp_path)
    report = evaluate_saved_qa(prediction, manifest, receipt, source, split,
                               annotation_source="synthetic_fixture")
    path = tmp_path / "qa_scoring_manifest.json"
    save_qa_scoring_manifest(path, prediction, manifest, receipt, report,
                             annotation_source="synthetic_fixture")
    data = json.loads(path.read_bytes())
    data["reason_counts"]["other"] += 1
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                     allow_nan=False).encode("utf-8")
    path.write_bytes(raw)
    path.with_suffix(".sha256").write_text(hashlib.sha256(raw).hexdigest() + "\n", encoding="ascii")
    with pytest.raises(EvaluationInputError, match="QA_SCORING_INVALID"):
        verify_qa_scoring_manifest(path, prediction, manifest, receipt, report,
                                   annotation_source="synthetic_fixture")
    with pytest.raises(EvaluationInputError, match="PREDICTION_RECEIPT_INVALID"):
        verify_qa_scoring_manifest(path, prediction, manifest,
            replace(receipt, snapshot_sha256="0" * 64), report,
            annotation_source="synthetic_fixture")
    with pytest.raises(EvaluationInputError, match="PREDICTION_HASH_MISMATCH"):
        verify_qa_scoring_manifest(path, prediction, manifest,
            replace(receipt, file_sha256="0" * 64), report,
            annotation_source="synthetic_fixture")
    with pytest.raises(EvaluationInputError, match="PREDICTION_INVALID"):
        verify_qa_scoring_manifest(path, prediction, manifest,
            replace(receipt, method="vector_rag"), report,
            annotation_source="synthetic_fixture")


def test_legacy_f1_unavailable_and_invalid_records():
    legacy = replace(row("q"), text_token_f1=None)
    summary = summarize_questions((legacy,))
    assert summary.main_mean == 1 and summary.text_f1_main_mean is None
    for bad in (replace(legacy, score=math.nan), replace(legacy, score=math.inf),
                replace(legacy, text_token_f1=math.nan)):
        with pytest.raises(EvaluationInputError, match="QA_RESULT_INVALID"):
            summarize_questions((bad,))
    with pytest.raises(EvaluationInputError, match="DUPLICATE_QA_RESULT"):
        summarize_questions((legacy, legacy))
