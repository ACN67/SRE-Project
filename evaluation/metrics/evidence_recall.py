"""Evidence-turn recall over ranked Memories, never Memory/fact recall."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Iterable

from dynamic_memory.contracts import QueryResponse, SourceRef
from dynamic_memory.data.repository import SourceRepository, SourceRepositoryError

from evaluation.gold_loader import GoldLoadResult, GoldRecord
from evaluation.predictions import PredictionReceipt, _check_rows, verify_predictions
from evaluation.question_manifest import EvaluationInputError, QuestionManifest
from evaluation.gold_loader import load_gold_after_predictions

KS = (5, 10)


@dataclass(frozen=True, slots=True)
class EvidenceTurnScore:
    sample_id: str
    query_id: str
    gold_record_key: str
    method: str
    k: int
    status: str
    annotation_source: str | None
    snapshot_sha256: str
    retriever_fingerprint: str | None
    gold_turn_count: int
    top_k_memory_count: int
    retrieved_turn_count: int
    matched_turn_count: int
    evidence_turn_recall: float | None


@dataclass(frozen=True, slots=True)
class EvidenceTurnSummary:
    key: str
    k: int
    total_questions: int
    scored_questions: int
    status_counts: tuple[tuple[str, int], ...]
    main_mean: float | None


@dataclass(frozen=True, slots=True)
class EvidenceTurnReport:
    questions: tuple[EvidenceTurnScore, ...]
    by_method: tuple[EvidenceTurnSummary, ...]
    by_sample: tuple[EvidenceTurnSummary, ...]


def _verified_turns(record: GoldRecord, sources: SourceRepository) -> tuple[str, set[tuple[str, str]]]:
    """Synthetic protocol: list of exact refs or dia_id strings with unique lookup.

    A string is an entire dia_id, never parsed as a session prefix. The same
    protocol may serve real Gold only after its shape has been independently
    confirmed and every ref has passed the sample's SourceRepository.
    """
    try:
        raw = json.loads(record.evidence_json)
    except (TypeError, ValueError):
        return "evidence_unrecognized", set()
    if not isinstance(raw, list):
        return "evidence_unrecognized", set()
    keys: set[tuple[str, str]] = set()
    for entry in raw:
        if isinstance(entry, str) and entry.strip():
            try:
                sample = sources.get_sample(record.sample_id)
            except Exception:
                return "source_invalid", set()
            matches = [SourceRef(session_id=s.session_id, dia_id=entry)
                       for s in sample.sessions for turn in s.turns if turn.dia_id == entry]
            if not matches:
                return "source_invalid", set()
            if len(matches) != 1:
                return "session_ambiguous", set()
            ref = matches[0]
        elif isinstance(entry, dict) and set(entry) == {"session_id", "dia_id"}:
            try:
                ref = SourceRef.model_validate(entry)
            except Exception:
                return "evidence_unrecognized", set()
        else:
            return "evidence_unrecognized", set()
        try:
            turn = sources.get_turn(record.sample_id, ref)
            if (turn.sample_id, turn.session_id, turn.dia_id) != (record.sample_id, ref.session_id, ref.dia_id):
                return "source_invalid", set()
        except Exception:
            return "source_invalid", set()
        keys.add((ref.session_id, ref.dia_id))
    return ("gold_empty" if not keys else "ready"), keys


def _check_source_snapshot(manifest: QuestionManifest, sources: SourceRepository) -> None:
    if type(manifest) is not QuestionManifest or type(sources) is not SourceRepository:
        raise EvaluationInputError("EVIDENCE_INPUT_INVALID")
    try:
        source_snapshot = sources.snapshot_sha256
    except SourceRepositoryError:
        raise EvaluationInputError("EVIDENCE_SOURCE_UNAVAILABLE") from None
    if source_snapshot != manifest.snapshot_sha256:
        raise EvaluationInputError("EVIDENCE_SNAPSHOT_MISMATCH")


def score_evidence_turns(manifest: QuestionManifest, responses: Iterable[QueryResponse],
                         gold: GoldLoadResult, sources: SourceRepository, method: str,
                         *, ks: tuple[int, ...] = KS) -> EvidenceTurnReport:
    if type(gold) is not GoldLoadResult or ks != KS:
        raise EvaluationInputError("EVIDENCE_INPUT_INVALID")
    _check_source_snapshot(manifest, sources)
    checked = _check_rows(manifest, responses, method)
    expected = {record.gold_record_key: record for record in manifest.records}
    by_gold: dict[str, GoldRecord] = {}
    for record in gold.records:
        question = expected.get(record.gold_record_key) if type(record) is GoldRecord else None
        if (question is None or record.gold_record_key in by_gold or record.sample_id != question.sample_id or
                record.query_id != question.query_id or record.original_qa_index != question.original_qa_index or
                record.annotation_source not in ("course_gold", "team_annotated", "synthetic_fixture")):
            raise EvaluationInputError("GOLD_ID_MISMATCH")
        by_gold[record.gold_record_key] = record
    if (type(gold.missing_keys) is not tuple or len(set(gold.missing_keys)) != len(gold.missing_keys) or
            set(gold.missing_keys) & set(by_gold) or set(gold.missing_keys) | set(by_gold) != set(expected) or
            len({r.annotation_source for r in by_gold.values()}) > 1):
        raise EvaluationInputError("GOLD_COVERAGE_INVALID")
    by_query = {row.query_id: row for row in checked}
    output = []
    for question in manifest.records:
        response = by_query[question.query_id]
        record = by_gold.get(question.gold_record_key)
        gold_status, gold_keys = ("gold_missing", set()) if record is None else _verified_turns(record, sources)
        for k in ks:
            status = gold_status
            top = []
            found: set[tuple[str, str]] = set()
            fingerprint = response.retrieved.retriever_fingerprint if response.retrieved else None
            if method == "full_text":
                status = "not_applicable"
            elif status == "ready":
                if response.retrieved is None:
                    status = "model_error" if response.answer_result.status == "error" else "retrieval_unavailable"
                else:
                    top = sorted(response.retrieved.items, key=lambda item: item.rank)[:k]
                    for item in top:
                        for ref in item.source_refs:
                            try:
                                turn = sources.get_turn(question.sample_id, ref)
                                if (turn.sample_id, turn.session_id, turn.dia_id) != (question.sample_id, ref.session_id, ref.dia_id):
                                    raise ValueError
                            except Exception:
                                status = "candidate_source_invalid"
                                break
                            found.add((ref.session_id, ref.dia_id))
                        if status != "ready":
                            break
            hits = len(found & gold_keys) if status == "ready" else 0
            output.append(EvidenceTurnScore(question.sample_id, question.query_id, question.gold_record_key,
                                            method, k, status, record.annotation_source if record else None,
                                            manifest.snapshot_sha256, fingerprint, len(gold_keys), len(top), len(found), hits,
                                            hits / len(gold_keys) if status == "ready" else None))
    rows = tuple(output)
    return EvidenceTurnReport(rows,
        tuple(summarize_evidence_turns(rows, method, k) for k in ks),
        tuple(summarize_evidence_turns(tuple(r for r in rows if r.sample_id == sample), sample, k)
              for sample in dict.fromkeys(r.sample_id for r in rows) for k in ks))


def summarize_evidence_turns(rows: Iterable[EvidenceTurnScore], key: str, k: int) -> EvidenceTurnSummary:
    selected = tuple(row for row in rows if row.k == k)
    scores = [row.evidence_turn_recall for row in selected if row.status == "ready"]
    counts = tuple((status, sum(row.status == status for row in selected))
                   for status in dict.fromkeys(row.status for row in selected))
    return EvidenceTurnSummary(key, k, len(selected), len(scores), counts,
                               math.fsum(scores) / len(scores) if scores else None)


def evaluate_saved_evidence(prediction_path, manifest: QuestionManifest, receipt: PredictionReceipt,
                            snapshot_path, split_path, sources: SourceRepository, *, annotation_source: str) -> EvidenceTurnReport:
    """Validate saved predictions and source snapshot before opening Gold."""
    rows = verify_predictions(prediction_path, manifest, receipt)
    _check_source_snapshot(manifest, sources)
    gold = load_gold_after_predictions(snapshot_path, split_path, prediction_path, manifest, receipt,
                                       annotation_source=annotation_source)
    return score_evidence_turns(manifest, rows, gold, sources, receipt.method)
