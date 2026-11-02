"""Per-question QA scores and auditable denominators, independent of Gold values."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from dynamic_memory.contracts import QueryResponse

from evaluation.gold_loader import GoldLoadResult, GoldRecord, load_gold_after_predictions
from evaluation.predictions import PredictionReceipt, _check_rows, verify_predictions
from evaluation.question_manifest import EvaluationInputError, QuestionManifest

from .answer_adapter import adapt_answer
from .official_qa import official_token_f1, score_official_qa

REASONS = ("execution_failure", "missing_gold", "no_evidence", "time_uncertain", "other")


def _reason(row: "QuestionScore") -> str:
    if row.execution_failed:
        return "execution_failure"
    if row.gold_missing:
        return "missing_gold"
    if row.original_status in ("no_evidence", "time_uncertain"):
        return row.original_status
    return "other"


def _sample(value: object) -> str:
    return value if isinstance(value, str) and value.strip() else "unknown"


@dataclass(frozen=True, slots=True)
class QuestionScore:
    sample_id: str
    query_id: str
    gold_record_key: str
    method: str
    category: int | None
    original_status: str
    original_prediction: str | None
    adapted_text: str | None
    execution_failed: bool
    gold_missing: bool
    score: float | None
    annotation_source: str | None
    text_token_f1: float | None = None


@dataclass(frozen=True, slots=True)
class GroupScore:
    key: str
    total_predictions: int
    gold_scored: int
    gold_missing: int
    execution_failed: int
    conditional_scored: int
    main_mean: float | None
    conditional_mean: float | None
    reason_counts: tuple[tuple[str, int], ...] = ()
    other_status_counts: tuple[tuple[str, int], ...] = ()
    text_f1_scored: int = 0
    text_f1_conditional_scored: int = 0
    text_f1_main_mean: float | None = None
    text_f1_conditional_mean: float | None = None


@dataclass(frozen=True, slots=True)
class QASummary:
    score_name: str
    total_predictions: int
    gold_scored: int
    gold_missing: int
    execution_failed: int
    conditional_scored: int
    main_mean: float | None
    conditional_mean: float | None
    execution_success_coverage: float | None
    scored_execution_coverage: float | None
    by_category: tuple[GroupScore, ...]
    by_sample: tuple[GroupScore, ...]
    by_method: tuple[GroupScore, ...]
    by_category_sample: tuple[GroupScore, ...] = ()
    reason_counts: tuple[tuple[str, int], ...] = ()
    other_status_counts: tuple[tuple[str, int], ...] = ()
    text_f1_scored: int = 0
    text_f1_conditional_scored: int = 0
    text_f1_main_mean: float | None = None
    text_f1_conditional_mean: float | None = None


@dataclass(frozen=True, slots=True)
class QAReport:
    questions: tuple[QuestionScore, ...]
    summary: QASummary


def _group(key: str, rows: tuple[QuestionScore, ...]) -> GroupScore:
    scored = [row for row in rows if not row.gold_missing]
    successful = [row for row in scored if not row.execution_failed]
    f1 = [row for row in scored if row.text_token_f1 is not None]
    f1_success = [row for row in f1 if not row.execution_failed]
    reasons = tuple((reason, sum(_reason(row) == reason for row in rows)) for reason in REASONS)
    other_states = tuple((status, sum(_reason(row) == "other" and row.original_status == status for row in rows))
                         for status in sorted({row.original_status for row in rows if _reason(row) == "other"}))
    return GroupScore(
        key=key, total_predictions=len(rows), gold_scored=len(scored),
        gold_missing=len(rows) - len(scored),
        execution_failed=sum(row.execution_failed for row in rows),
        conditional_scored=len(successful),
        main_mean=math.fsum(row.score for row in scored) / len(scored) if scored else None,
        conditional_mean=math.fsum(row.score for row in successful) / len(successful) if successful else None,
        reason_counts=reasons, other_status_counts=other_states,
        text_f1_scored=len(f1), text_f1_conditional_scored=len(f1_success),
        text_f1_main_mean=math.fsum(row.text_token_f1 for row in f1) / len(f1) if f1 else None,
        text_f1_conditional_mean=math.fsum(row.text_token_f1 for row in f1_success) / len(f1_success) if f1_success else None,
    )


def summarize_questions(rows: Iterable[QuestionScore]) -> QASummary:
    values = tuple(rows)
    if any(
        type(row) is not QuestionScore or
        not isinstance(row.original_status, str) or not row.original_status or
        row.execution_failed != (row.original_status == "error") or
        (row.gold_missing and (row.score is not None or row.category is not None or row.annotation_source is not None or row.text_token_f1 is not None)) or
        (not row.gold_missing and (type(row.category) is not int or row.category not in (1, 2, 3, 4, 5) or
                                   row.score is None or row.annotation_source not in
                                   ("course_gold", "team_annotated", "synthetic_fixture"))) or
        (row.execution_failed and row.score not in (None, 0.0)) or
        (row.score is not None and (not isinstance(row.score, (int, float)) or
                                    not math.isfinite(row.score) or not 0 <= row.score <= 1)) or
        (row.text_token_f1 is not None and (not isinstance(row.text_token_f1, (int, float)) or
             not math.isfinite(row.text_token_f1) or not 0 <= row.text_token_f1 <= 1)) or
        (row.execution_failed and row.text_token_f1 not in (None, 0.0))
        for row in values
    ):
        raise EvaluationInputError("QA_RESULT_INVALID")
    if len({(row.method, row.query_id) for row in values}) != len(values):
        raise EvaluationInputError("DUPLICATE_QA_RESULT")
    overall = _group("all", values)
    categories = tuple(_group(str(category), tuple(row for row in values if row.category == category))
                       for category in (1, 2, 3, 4, 5)) + ((
                           _group("unknown", tuple(row for row in values if row.category is None)),)
                           if any(row.category is None for row in values) else ())
    samples = tuple(_group(sample, tuple(row for row in values if _sample(row.sample_id) == sample))
                    for sample in dict.fromkeys(_sample(row.sample_id) for row in values))
    methods = tuple(_group(method, tuple(row for row in values if row.method == method))
                    for method in dict.fromkeys(row.method for row in values))
    cross = tuple(_group(f"{category}|{sample}", tuple(row for row in values
                    if (str(row.category) if row.category is not None else "unknown") == category
                    and _sample(row.sample_id) == sample))
                  for category in ("1", "2", "3", "4", "5", "unknown")
                  for sample in dict.fromkeys(_sample(row.sample_id) for row in values)
                  if any((str(row.category) if row.category is not None else "unknown") == category
                         and _sample(row.sample_id) == sample for row in values))
    return QASummary(
        score_name="LoCoMo 官方 QA 综合得分", total_predictions=overall.total_predictions,
        gold_scored=overall.gold_scored, gold_missing=overall.gold_missing,
        execution_failed=overall.execution_failed, conditional_scored=overall.conditional_scored,
        main_mean=overall.main_mean, conditional_mean=overall.conditional_mean,
        execution_success_coverage=(len(values) - overall.execution_failed) / len(values) if values else None,
        scored_execution_coverage=overall.conditional_scored / overall.gold_scored if overall.gold_scored else None,
        by_category=categories, by_sample=samples, by_method=methods,
        by_category_sample=cross, reason_counts=overall.reason_counts,
        other_status_counts=overall.other_status_counts,
        text_f1_scored=overall.text_f1_scored,
        text_f1_conditional_scored=overall.text_f1_conditional_scored,
        text_f1_main_mean=overall.text_f1_main_mean,
        text_f1_conditional_mean=overall.text_f1_conditional_mean,
    )


def score_qa_results(manifest: QuestionManifest, responses: Iterable[QueryResponse],
                     gold: GoldLoadResult, method: str) -> QAReport:
    """Match only frozen IDs and Gold keys; preserve one row for every prediction."""
    if type(gold) is not GoldLoadResult:
        raise EvaluationInputError("GOLD_RESULT_INVALID")
    checked = _check_rows(manifest, responses, method)
    by_gold = {}
    expected = {record.gold_record_key: record for record in manifest.records}
    for record in gold.records:
        if type(record) is not GoldRecord or record.gold_record_key in by_gold:
            raise EvaluationInputError("GOLD_ID_MISMATCH")
        question = expected.get(record.gold_record_key)
        if (question is None or record.query_id != question.query_id or
                record.sample_id != question.sample_id or record.original_qa_index != question.original_qa_index or
                type(record.category) is not int or record.category not in (1, 2, 3, 4, 5) or
                not isinstance(record.answer, str) or not record.answer.strip() or
                record.annotation_source not in ("course_gold", "team_annotated", "synthetic_fixture")):
            raise EvaluationInputError("GOLD_ID_MISMATCH")
        by_gold[record.gold_record_key] = record
    missing = gold.missing_keys
    if (not isinstance(missing, tuple) or len(set(missing)) != len(missing) or
            set(missing) & set(by_gold) or set(missing) | set(by_gold) != set(expected)):
        raise EvaluationInputError("GOLD_COVERAGE_INVALID")
    if len({record.annotation_source for record in by_gold.values()}) > 1:
        raise EvaluationInputError("ANNOTATION_SOURCE_MIXED")
    by_query = {response.query_id: response for response in checked}
    rows = []
    for question in manifest.records:
        response = by_query[question.query_id]
        adapted = adapt_answer(response.answer_result)
        gold_record = by_gold.get(question.gold_record_key)
        if gold_record is None:
            score = None
            text_f1 = None
        elif adapted.execution_failed:
            score = 0.0
            text_f1 = 0.0
        else:
            score = score_official_qa(gold_record.category, adapted.adapted_text, gold_record.answer)
            text_f1 = official_token_f1(adapted.adapted_text, gold_record.answer)
        rows.append(QuestionScore(
            sample_id=question.sample_id, query_id=question.query_id,
            gold_record_key=question.gold_record_key, method=method,
            category=gold_record.category if gold_record else None,
            original_status=adapted.original_status, original_prediction=response.answer_result.answer,
            adapted_text=adapted.adapted_text, execution_failed=adapted.execution_failed,
            gold_missing=gold_record is None, score=score,
            annotation_source=gold_record.annotation_source if gold_record else None,
            text_token_f1=text_f1,
        ))
    values = tuple(rows)
    return QAReport(values, summarize_questions(values))


def evaluate_saved_qa(prediction_path: str | Path, manifest: QuestionManifest,
                      receipt: PredictionReceipt, snapshot_path: str | Path,
                      split_path: str | Path, *, annotation_source: str) -> QAReport:
    """A-14 verifies persisted bytes before this layer may see any Gold record."""
    gold = load_gold_after_predictions(snapshot_path, split_path, prediction_path,
                                       manifest, receipt, annotation_source=annotation_source)
    responses = verify_predictions(prediction_path, manifest, receipt)
    return score_qa_results(manifest, responses, gold, receipt.method)
