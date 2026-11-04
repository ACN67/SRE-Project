"""Deterministic, Gold-free aggregate artifact produced after QA scoring."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from evaluation.predictions import (
    PredictionReceipt, _publish_new_file, verify_predictions,
)
from evaluation.question_manifest import EvaluationInputError, QuestionManifest, _pairs

from .answer_adapter import adapt_answer
from .qa_summary import QAReport, summarize_questions


@dataclass(frozen=True, slots=True)
class ScoringReceipt:
    path: Path
    file_sha256: str


def _target(path: str | Path) -> Path:
    try:
        if ".." in Path(path).parts:
            raise EvaluationInputError("QA_SCORING_PATH_INVALID")
        target = Path(path).resolve()
        results = (Path(__file__).resolve().parents[2] / "results").resolve()
        temporary = Path(tempfile.gettempdir()).resolve()
        if (target.name != "qa_scoring_manifest.json" or
                not (target.is_relative_to(results) or target.is_relative_to(temporary))):
            raise EvaluationInputError("QA_SCORING_PATH_INVALID")
        return target
    except (OSError, TypeError, ValueError):
        raise EvaluationInputError("QA_SCORING_PATH_INVALID") from None


def _publish_new(target: Path, raw: bytes) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=target.parent,
                prefix=".qa-scoring-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _publish_new_file(temporary, target)
    except FileExistsError:
        raise EvaluationInputError("QA_SCORING_PATH_IN_USE") from None
    except OSError:
        raise EvaluationInputError("QA_SCORING_SAVE_FAILED") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _run_sha(path: str | Path | None, manifest: QuestionManifest,
             receipt: PredictionReceipt) -> str | None:
    if path is None:
        return None
    try:
        from evaluation.runners.pure_vector_ablation import verify_pure_vector_run
        from evaluation.runners.three_method_runner import verify_run

        run_path = Path(path)
        if ".." in run_path.parts or run_path.name != "run_manifest.json" or run_path.is_symlink():
            raise ValueError
        run_path = run_path.resolve()
        if receipt.path != run_path.parent / f"{receipt.method}.predictions.json":
            raise ValueError
        if run_path.with_suffix(".sha256").is_symlink():
            raise ValueError
        if receipt.method == "pure_vector_topk":
            verified = verify_pure_vector_run(run_path.parent, manifest)
            receipts = (verified.receipt,)
        else:
            verified = verify_run(run_path.parent, manifest)
            receipts = verified.receipts
        if verified.manifest_path != run_path or receipt not in receipts:
            raise ValueError
        return verified.manifest_sha256
    except (OSError, UnicodeError, TypeError, ValueError, KeyError, EvaluationInputError):
        raise EvaluationInputError("QA_SCORING_RUN_INVALID") from None


def _json_bytes(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _checked_report(prediction_path: str | Path, manifest: QuestionManifest,
                    receipt: PredictionReceipt, report: QAReport, annotation_source: str) -> None:
    predictions = verify_predictions(prediction_path, manifest, receipt)
    if type(report) is not QAReport or report.summary != summarize_questions(report.questions):
        raise EvaluationInputError("QA_REPORT_INVALID")
    if len(report.questions) != len(predictions) or annotation_source not in (
            "course_gold", "team_annotated", "synthetic_fixture"):
        raise EvaluationInputError("QA_REPORT_INVALID")
    for question, prediction, frozen in zip(report.questions, predictions, manifest.records):
        adapted = adapt_answer(prediction.answer_result)
        if (question.query_id != frozen.query_id or question.sample_id != frozen.sample_id or
                question.gold_record_key != frozen.gold_record_key or question.method != receipt.method or
                question.original_status != adapted.original_status or
                question.original_prediction != prediction.answer_result.answer or
                question.adapted_text != adapted.adapted_text or
                question.execution_failed != adapted.execution_failed or
                (not question.gold_missing and question.annotation_source != annotation_source)):
            raise EvaluationInputError("QA_REPORT_ID_MISMATCH")


def _payload(manifest: QuestionManifest, receipt: PredictionReceipt,
             report: QAReport, annotation_source: str,
             run_manifest_sha256: str | None) -> dict:
    summary = report.summary
    return {
        "format_version": 1,
        "report_type": "qa_scoring_manifest",
        "snapshot_sha256": manifest.snapshot_sha256,
        "prediction_method": receipt.method,
        "prediction_sha256": receipt.file_sha256,
        "prediction_query_ids": list(receipt.query_ids),
        "run_manifest_sha256": run_manifest_sha256,
        "annotation_source": annotation_source,
        "score_units": {
            "official_qa_score": "LoCoMo five-category composite score [0,1]",
            "text_token_f1": "ordinary text token F1 [0,1]",
        },
        "total_predictions": summary.total_predictions,
        "gold_scored": summary.gold_scored,
        "gold_missing": summary.gold_missing,
        "execution_failed": summary.execution_failed,
        "conditional_scored": summary.conditional_scored,
        "by_category": [asdict(item) for item in summary.by_category],
        "by_sample": [asdict(item) for item in summary.by_sample],
        "by_method": [asdict(item) for item in summary.by_method],
        "by_category_sample": [asdict(item) for item in summary.by_category_sample],
        "reason_counts": dict(summary.reason_counts),
        "other_status_counts": dict(summary.other_status_counts),
        "official_qa_summary": {"main_mean": summary.main_mean,
                                "conditional_mean": summary.conditional_mean,
                                "main_denominator": summary.gold_scored,
                                "conditional_denominator": summary.conditional_scored},
        "text_f1_summary": {"main_mean": summary.text_f1_main_mean,
                            "conditional_mean": summary.text_f1_conditional_mean,
                            "main_denominator": summary.text_f1_scored,
                            "conditional_denominator": summary.text_f1_conditional_scored},
        "qa_summary": asdict(summary),
    }


def save_qa_scoring_manifest(path: str | Path, prediction_path: str | Path,
                             manifest: QuestionManifest, receipt: PredictionReceipt,
                             report: QAReport, *, annotation_source: str,
                             run_manifest_path: str | Path | None = None) -> ScoringReceipt:
    """Save a scoring artifact after independently reopening the prediction bytes."""
    _checked_report(prediction_path, manifest, receipt, report, annotation_source)
    run_sha = _run_sha(run_manifest_path, manifest, receipt)
    raw = _json_bytes(_payload(manifest, receipt, report, annotation_source, run_sha))
    target = _target(path)
    sidecar = target.with_suffix(".sha256")
    complete = target.with_suffix(".complete.json")
    if any(p.exists() or p.is_symlink() for p in (target, sidecar, complete)):
        raise EvaluationInputError("QA_SCORING_PATH_IN_USE")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(raw).hexdigest()
        _publish_new(target, raw)
        _publish_new(sidecar, (digest + "\n").encode("ascii"))
        marker = _json_bytes({"format_version": 1, "manifest_sha256": digest,
            "prediction_sha256": receipt.file_sha256, "snapshot_sha256": manifest.snapshot_sha256,
            "prediction_method": receipt.method, "run_manifest_sha256": run_sha})
        _publish_new(complete, marker)
    except EvaluationInputError:
        raise
    except OSError:
        raise EvaluationInputError("QA_SCORING_SAVE_FAILED") from None
    return ScoringReceipt(target, digest)


def verify_qa_scoring_manifest(path: str | Path, prediction_path: str | Path,
                               manifest: QuestionManifest, receipt: PredictionReceipt,
                               report: QAReport, *, annotation_source: str,
                               run_manifest_path: str | Path | None = None,
                               expected_sha256: str | None = None) -> ScoringReceipt:
    """Reopen canonical JSON and compare every field to verified prediction and score data."""
    _checked_report(prediction_path, manifest, receipt, report, annotation_source)
    target = _target(path)
    try:
        if any(p.is_symlink() for p in (target, target.with_suffix(".sha256"),
                                       target.with_suffix(".complete.json"))):
            raise EvaluationInputError("QA_SCORING_PATH_INVALID")
        raw = target.read_bytes()
        recorded = target.with_suffix(".sha256").read_text(encoding="ascii")
        marker_raw = target.with_suffix(".complete.json").read_bytes()
    except EvaluationInputError:
        raise
    except (OSError, UnicodeError):
        raise EvaluationInputError("QA_SCORING_NOT_FOUND") from None
    digest = hashlib.sha256(raw).hexdigest()
    if recorded != digest + "\n" or (expected_sha256 is not None and digest != expected_sha256):
        raise EvaluationInputError("QA_SCORING_HASH_MISMATCH")
    run_sha = _run_sha(run_manifest_path, manifest, receipt)
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        expected = _payload(manifest, receipt, report, annotation_source, run_sha)
        marker = json.loads(marker_raw.decode("utf-8"), object_pairs_hook=_pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        expected_marker = {"format_version": 1, "manifest_sha256": digest,
            "prediction_sha256": receipt.file_sha256, "snapshot_sha256": manifest.snapshot_sha256,
            "prediction_method": receipt.method, "run_manifest_sha256": run_sha}
        if (_json_bytes(data) != raw or _json_bytes(expected) != raw or
                _json_bytes(marker) != marker_raw or marker != expected_marker):
            raise ValueError()
    except (ValueError, TypeError, KeyError, OSError, UnicodeError, EvaluationInputError):
        raise EvaluationInputError("QA_SCORING_INVALID") from None
    return ScoringReceipt(target, digest)
