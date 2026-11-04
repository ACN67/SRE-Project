"""Synthetic QA aggregate and persisted scoring artifact checks."""

import hashlib
import json
import math
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

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
    with pytest.raises(EvaluationInputError, match="QA_SCORING_PATH_IN_USE"):
        save_qa_scoring_manifest(path, prediction, manifest, receipt, report,
                                 annotation_source="synthetic_fixture")
    assert path.read_bytes() == first


def test_scoring_requires_complete_proof_and_trusted_digest(tmp_path):
    source, split, manifest, prediction, receipt, _ = scored_fixture(tmp_path)
    report = evaluate_saved_qa(prediction, manifest, receipt, source, split,
                               annotation_source="synthetic_fixture")
    path = tmp_path / "qa_scoring_manifest.json"
    saved = save_qa_scoring_manifest(path, prediction, manifest, receipt, report,
                                     annotation_source="synthetic_fixture")
    with pytest.raises(EvaluationInputError, match="QA_SCORING_HASH_MISMATCH"):
        verify_qa_scoring_manifest(path, prediction, manifest, receipt, report,
            annotation_source="synthetic_fixture", expected_sha256="0" * 64)
    complete = path.with_suffix(".complete.json")
    assert complete.is_file()
    original = complete.read_bytes()
    complete.unlink()
    with pytest.raises(EvaluationInputError):
        verify_qa_scoring_manifest(path, prediction, manifest, receipt, report,
                                   annotation_source="synthetic_fixture")
    complete.write_bytes(original)
    assert verify_qa_scoring_manifest(path, prediction, manifest, receipt, report,
                                      annotation_source="synthetic_fixture") == saved
    path.with_suffix(".sha256").unlink()
    with pytest.raises(EvaluationInputError):
        verify_qa_scoring_manifest(path, prediction, manifest, receipt, report,
                                   annotation_source="synthetic_fixture")


def test_scoring_rejects_outside_and_symlink_escape(tmp_path, monkeypatch):
    source, split, manifest, prediction, receipt, _ = scored_fixture(tmp_path)
    report = evaluate_saved_qa(prediction, manifest, receipt, source, split,
                               annotation_source="synthetic_fixture")
    with pytest.raises(EvaluationInputError, match="QA_SCORING_PATH_INVALID"):
        save_qa_scoring_manifest(tmp_path / "wrong.json", prediction, manifest, receipt, report,
                                 annotation_source="synthetic_fixture")
    outside = Path(__file__).resolve().parents[2] / "qa_scoring_manifest.json"
    original_write = Path.write_bytes
    def guarded_write(path, data):
        if path == outside:
            raise AssertionError("outside write attempted")
        return original_write(path, data)
    monkeypatch.setattr(Path, "write_bytes", guarded_write)
    with pytest.raises(EvaluationInputError, match="QA_SCORING_PATH_INVALID"):
        save_qa_scoring_manifest(outside, prediction, manifest, receipt, report,
                                 annotation_source="synthetic_fixture")


def test_scoring_rejects_symlink_directory_escape(tmp_path, monkeypatch):
    source, split, manifest, prediction, receipt, _ = scored_fixture(tmp_path)
    report = evaluate_saved_qa(prediction, manifest, receipt, source, split,
                               annotation_source="synthetic_fixture")
    outside = Path(__file__).resolve().parents[2]
    link = tmp_path / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        # Windows may deny symlink creation. Exercise the same resolved-path gate.
        original_resolve = Path.resolve
        def resolved(self, *args, **kwargs):
            if self == link / "qa_scoring_manifest.json":
                return outside / "qa_scoring_manifest.json"
            return original_resolve(self, *args, **kwargs)
        monkeypatch.setattr(Path, "resolve", resolved)
    with pytest.raises(EvaluationInputError, match="QA_SCORING_PATH_INVALID"):
        save_qa_scoring_manifest(link / "qa_scoring_manifest.json", prediction,
            manifest, receipt, report, annotation_source="synthetic_fixture")
    assert not (outside / "qa_scoring_manifest.json").exists()


def test_scoring_concurrent_publication_has_one_winner(tmp_path):
    source, split, manifest, prediction, receipt, _ = scored_fixture(tmp_path)
    report = evaluate_saved_qa(prediction, manifest, receipt, source, split,
                               annotation_source="synthetic_fixture")
    path = tmp_path / "qa_scoring_manifest.json"
    def publish():
        try:
            return save_qa_scoring_manifest(path, prediction, manifest, receipt, report,
                                            annotation_source="synthetic_fixture")
        except EvaluationInputError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: publish(), range(2)))
    assert sum(isinstance(item, str) for item in outcomes) == 1
    assert any(isinstance(item, str) and "QA_SCORING_PATH_IN_USE" in item for item in outcomes)
    assert verify_qa_scoring_manifest(path, prediction, manifest, receipt, report,
                                      annotation_source="synthetic_fixture") == next(
        item for item in outcomes if not isinstance(item, str))


@pytest.mark.parametrize("stage", [1, 2, 3])
def test_scoring_interrupted_publish_fails_closed(tmp_path, monkeypatch, stage):
    import evaluation.metrics.qa_scoring_manifest as scoring
    source, split, manifest, prediction, receipt, _ = scored_fixture(tmp_path)
    report = evaluate_saved_qa(prediction, manifest, receipt, source, split,
                               annotation_source="synthetic_fixture")
    path = tmp_path / "qa_scoring_manifest.json"
    publisher_name = "rename" if os.name == "nt" else "link"
    original = getattr(scoring.os, publisher_name)
    calls = 0
    def interrupted(source_path, destination):
        nonlocal calls
        calls += 1
        if calls == stage:
            raise OSError("SECRET_GOLD interruption")
        return original(source_path, destination)
    monkeypatch.setattr(scoring.os, publisher_name, interrupted)
    with pytest.raises(EvaluationInputError, match="QA_SCORING_SAVE_FAILED") as error:
        save_qa_scoring_manifest(path, prediction, manifest, receipt, report,
                                 annotation_source="synthetic_fixture")
    assert "SECRET_GOLD" not in str(error.value)
    with pytest.raises(EvaluationInputError):
        verify_qa_scoring_manifest(path, prediction, manifest, receipt, report,
                                   annotation_source="synthetic_fixture")


def test_scoring_rejects_marker_and_rehashed_manifest(tmp_path):
    source, split, manifest, prediction, receipt, _ = scored_fixture(tmp_path)
    report = evaluate_saved_qa(prediction, manifest, receipt, source, split,
                               annotation_source="synthetic_fixture")
    path = tmp_path / "qa_scoring_manifest.json"
    save_qa_scoring_manifest(path, prediction, manifest, receipt, report,
                             annotation_source="synthetic_fixture")
    complete = path.with_suffix(".complete.json")
    complete.write_bytes(b"{}")
    with pytest.raises(EvaluationInputError, match="QA_SCORING_INVALID"):
        verify_qa_scoring_manifest(path, prediction, manifest, receipt, report,
                                   annotation_source="synthetic_fixture")
    data = json.loads(path.read_bytes())
    data["prediction_method"] = "dynamic"
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                     allow_nan=False).encode("utf-8")
    path.write_bytes(raw)
    path.with_suffix(".sha256").write_text(hashlib.sha256(raw).hexdigest() + "\n", encoding="ascii")
    with pytest.raises(EvaluationInputError):
        verify_qa_scoring_manifest(path, prediction, manifest, receipt, report,
                                   annotation_source="synthetic_fixture")


def test_scoring_fsync_failure_has_no_complete_artifact(tmp_path, monkeypatch):
    import evaluation.metrics.qa_scoring_manifest as scoring
    source, split, manifest, prediction, receipt, _ = scored_fixture(tmp_path)
    report = evaluate_saved_qa(prediction, manifest, receipt, source, split,
                               annotation_source="synthetic_fixture")
    path = tmp_path / "qa_scoring_manifest.json"
    monkeypatch.setattr(scoring.os, "fsync", lambda _: (_ for _ in ()).throw(OSError("SECRET_GOLD")))
    with pytest.raises(EvaluationInputError, match="QA_SCORING_SAVE_FAILED") as error:
        save_qa_scoring_manifest(path, prediction, manifest, receipt, report,
                                 annotation_source="synthetic_fixture")
    assert "SECRET_GOLD" not in str(error.value)
    assert not path.exists() and not path.with_suffix(".complete.json").exists()
    with pytest.raises(EvaluationInputError):
        verify_qa_scoring_manifest(path, prediction, manifest, receipt, report,
                                   annotation_source="synthetic_fixture")


def test_scoring_orphan_sidecar_and_traversal_rejected(tmp_path):
    source, split, manifest, prediction, receipt, _ = scored_fixture(tmp_path)
    report = evaluate_saved_qa(prediction, manifest, receipt, source, split,
                               annotation_source="synthetic_fixture")
    path = tmp_path / "qa_scoring_manifest.json"
    path.with_suffix(".sha256").write_text("0" * 64 + "\n", encoding="ascii")
    with pytest.raises(EvaluationInputError):
        verify_qa_scoring_manifest(path, prediction, manifest, receipt, report,
                                   annotation_source="synthetic_fixture")
    with pytest.raises(EvaluationInputError, match="QA_SCORING_PATH_IN_USE"):
        save_qa_scoring_manifest(path, prediction, manifest, receipt, report,
                                 annotation_source="synthetic_fixture")
    with pytest.raises(EvaluationInputError, match="QA_SCORING_PATH_INVALID"):
        save_qa_scoring_manifest(tmp_path / "child" / ".." / path.name,
            prediction, manifest, receipt, report, annotation_source="synthetic_fixture")


def test_scoring_modified_prediction_and_report_identity_rejected(tmp_path):
    source, split, manifest, prediction, receipt, _ = scored_fixture(tmp_path)
    report = evaluate_saved_qa(prediction, manifest, receipt, source, split,
                               annotation_source="synthetic_fixture")
    path = tmp_path / "qa_scoring_manifest.json"
    save_qa_scoring_manifest(path, prediction, manifest, receipt, report,
                             annotation_source="synthetic_fixture")
    changed = replace(report.questions[0], query_id="wrong")
    changed_questions = (changed, *report.questions[1:])
    with pytest.raises(EvaluationInputError, match="QA_REPORT_ID_MISMATCH"):
        verify_qa_scoring_manifest(path, prediction, manifest, receipt,
            replace(report, questions=changed_questions, summary=summarize_questions(changed_questions)),
            annotation_source="synthetic_fixture")
    prediction.write_bytes(prediction.read_bytes() + b" ")
    with pytest.raises(EvaluationInputError, match="PREDICTION_HASH_MISMATCH"):
        verify_qa_scoring_manifest(path, prediction, manifest, receipt, report,
                                   annotation_source="synthetic_fixture")


def test_scoring_run_receipt_identity_and_sidecar(tmp_path):
    from dynamic_memory.pipeline.query_runner import QueryOrchestrator
    from evaluation.runners import RunProtocol, run_three_method_batch
    from test_run_query_dispatch import ports

    source, split, manifest, _, _, _ = scored_fixture(tmp_path)
    protocol = RunProtocol("synthetic_run", manifest.snapshot_sha256, manifest.selected_splits,
        "synthetic-prompt", "synthetic-model", "synthetic-settings", "synthetic-index",
        "synthetic-index-artifact", "latest_known", 8, 12)
    run = run_three_method_batch(manifest, QueryOrchestrator(**ports()), protocol,
                                 results_root=tmp_path)
    prediction, receipt = run.receipts[0].path, run.receipts[0]
    report = evaluate_saved_qa(prediction, manifest, receipt, source, split,
                               annotation_source="synthetic_fixture")
    run_path = run.manifest_path
    target = tmp_path / "qa_scoring_manifest.json"
    saved = save_qa_scoring_manifest(target, prediction, manifest, receipt, report,
        annotation_source="synthetic_fixture", run_manifest_path=run_path)
    assert verify_qa_scoring_manifest(target, prediction, manifest, receipt, report,
        annotation_source="synthetic_fixture", run_manifest_path=run_path) == saved
    run_data = json.loads(run_path.read_bytes())
    run_data["query_ids"] = ["wrong"]
    changed = json.dumps(run_data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    run_path.write_bytes(changed)
    run_path.with_suffix(".sha256").write_text(hashlib.sha256(changed).hexdigest() + "\n", encoding="ascii")
    with pytest.raises(EvaluationInputError, match="QA_SCORING_RUN_INVALID"):
        verify_qa_scoring_manifest(target, prediction, manifest, receipt, report,
            annotation_source="synthetic_fixture", run_manifest_path=run_path)


def test_scoring_integrity_readback_needs_no_gold(tmp_path, monkeypatch):
    import evaluation.gold_loader as gold_loader

    source, split, manifest, prediction, receipt, _ = scored_fixture(tmp_path)
    report = evaluate_saved_qa(prediction, manifest, receipt, source, split,
                               annotation_source="synthetic_fixture")
    target = tmp_path / "qa_scoring_manifest.json"
    saved = save_qa_scoring_manifest(target, prediction, manifest, receipt, report,
        annotation_source="synthetic_fixture")
    source.unlink()
    split.unlink()
    monkeypatch.setattr(gold_loader, "load_gold_after_predictions",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Gold read")))
    assert verify_qa_scoring_manifest(target, prediction, manifest, receipt, report,
        annotation_source="synthetic_fixture", expected_sha256=saved.file_sha256) == saved


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
