"""Deterministic, Gold-free aggregate artifact produced after QA scoring."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from evaluation.predictions import PredictionReceipt, verify_predictions
from evaluation.question_manifest import EvaluationInputError, QuestionManifest, _pairs

from .answer_adapter import adapt_answer
from .qa_summary import QAReport, summarize_questions


@dataclass(frozen=True, slots=True)
class ScoringReceipt:
    path: Path
    file_sha256: str


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
    run_sha = (hashlib.sha256(Path(run_manifest_path).read_bytes()).hexdigest()
               if run_manifest_path is not None else None)
    raw = _json_bytes(_payload(manifest, receipt, report, annotation_source, run_sha))
    target = Path(path).resolve()
    if target.name != "qa_scoring_manifest.json":
        raise EvaluationInputError("QA_SCORING_PATH_INVALID")
    target.write_bytes(raw)
    target.with_suffix(".sha256").write_text(hashlib.sha256(raw).hexdigest() + "\n", encoding="ascii")
    return ScoringReceipt(target, hashlib.sha256(raw).hexdigest())


def verify_qa_scoring_manifest(path: str | Path, prediction_path: str | Path,
                               manifest: QuestionManifest, receipt: PredictionReceipt,
                               report: QAReport, *, annotation_source: str,
                               run_manifest_path: str | Path | None = None,
                               expected_sha256: str | None = None) -> ScoringReceipt:
    """Reopen canonical JSON and compare every field to verified prediction and score data."""
    _checked_report(prediction_path, manifest, receipt, report, annotation_source)
    target = Path(path).resolve()
    try:
        raw = target.read_bytes()
        recorded = target.with_suffix(".sha256").read_text(encoding="ascii")
    except (OSError, UnicodeError):
        raise EvaluationInputError("QA_SCORING_NOT_FOUND") from None
    digest = hashlib.sha256(raw).hexdigest()
    if recorded != digest + "\n" or (expected_sha256 is not None and digest != expected_sha256):
        raise EvaluationInputError("QA_SCORING_HASH_MISMATCH")
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        run_sha = (hashlib.sha256(Path(run_manifest_path).read_bytes()).hexdigest()
                   if run_manifest_path is not None else None)
        expected = _payload(manifest, receipt, report, annotation_source, run_sha)
        if _json_bytes(data) != raw or _json_bytes(expected) != raw:
            raise ValueError()
    except (ValueError, TypeError, KeyError, OSError, UnicodeError, EvaluationInputError):
        raise EvaluationInputError("QA_SCORING_INVALID") from None
    return ScoringReceipt(target, digest)
