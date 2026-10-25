"""Evaluation-only Gold loading, gated by verified persisted predictions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .predictions import PredictionReceipt, verify_predictions
from .question_manifest import (
    EvaluationInputError, QuestionManifest, _manifest_from_verified, _verified_source,
)

AnnotationSource = Literal["course_gold", "team_annotated", "synthetic_fixture"]


@dataclass(frozen=True, slots=True)
class GoldRecord:
    sample_id: str
    query_id: str
    gold_record_key: str
    original_qa_index: int
    answer: str
    evidence_json: str  # Original QA evidence JSON, never a Memory ID.
    category: int
    annotation_source: AnnotationSource


@dataclass(frozen=True, slots=True)
class GoldLoadResult:
    records: tuple[GoldRecord, ...]
    missing_keys: tuple[str, ...]


def load_gold_after_predictions(snapshot_path: str | Path, split_path: str | Path,
                                prediction_path: str | Path, manifest: QuestionManifest,
                                receipt: PredictionReceipt, *,
                                annotation_source: AnnotationSource) -> GoldLoadResult:
    """Verify the saved prediction before opening the source snapshot containing Gold."""
    if annotation_source not in ("course_gold", "team_annotated", "synthetic_fixture"):
        raise EvaluationInputError("ANNOTATION_SOURCE_INVALID")
    verify_predictions(prediction_path, manifest, receipt)
    info, document, split_by_id = _verified_source(snapshot_path, split_path)
    current = _manifest_from_verified(info, document, split_by_id,
                                      manifest.selected_splits, manifest.official_id_field)
    if current != manifest:
        raise EvaluationInputError("MANIFEST_MISMATCH")
    positions = {item["sample_id"]: item for item in document}
    records = []
    missing = []
    for question in manifest.records:
        qa = positions[question.sample_id]["qa"][question.original_qa_index]
        if "answer" not in qa or qa["answer"] is None:
            missing.append(question.gold_record_key)
            continue
        answer = qa["answer"]
        category = qa.get("category")
        if (not isinstance(answer, str) or not answer.strip() or
                type(category) is not int or category not in (1, 2, 3, 4, 5) or
                "evidence" not in qa):
            raise EvaluationInputError("GOLD_FORMAT_INVALID")
        evidence_json = json.dumps(qa["evidence"], ensure_ascii=False,
                                   sort_keys=True, separators=(",", ":"))
        records.append(GoldRecord(question.sample_id, question.query_id,
                                  question.gold_record_key, question.original_qa_index,
                                  answer, evidence_json, category, annotation_source))
    return GoldLoadResult(tuple(records), tuple(missing))
