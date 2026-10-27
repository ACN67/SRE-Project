"""Evaluation-only comparison of explicit state annotations with public DTOs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from dynamic_memory.contracts import (MemoryRecord, QueryResponse, SourceRef,
                                      validate_resolution_against_candidates)
from dynamic_memory.data.repository import SourceRepository
from evaluation.question_manifest import EvaluationInputError

AnnotationSource = Literal["course_gold", "team_annotated", "synthetic_fixture"]
RelationLabel = Literal["UPDATE", "SUPERSEDE", "CONFLICT", "NO_RELATION"]
DecisionLabel = Literal["RETAIN", "EXCLUDE", "UNRESOLVED"]


@dataclass(frozen=True, slots=True)
class MemoryCatalog:
    sample_id: str
    fingerprint: str
    data_fingerprint: str
    memories: tuple[MemoryRecord, ...]
    complete: bool  # Only a verified complete build can justify extraction_missing.


@dataclass(frozen=True, slots=True)
class GoldItemMapping:
    gold_item_id: str
    sample_id: str
    source_refs: tuple[SourceRef, ...]
    entity: str
    attribute: str
    value: str
    matched_memory_ids: tuple[str, ...]  # 0=unmapped; >1=ambiguous, never auto-select.
    match_basis: str
    annotation_source: AnnotationSource
    data_fingerprint: str
    memory_fingerprint: str
    match_status: Literal["matched", "ambiguous", "unmapped", "extraction_missing"] = "matched"
    annotator: str = ""
    reviewer: str = ""
    review_note: str = ""


@dataclass(frozen=True, slots=True)
class RelationGold:
    old_item_id: str
    new_item_id: str
    scope_attribute: str
    label: RelationLabel


@dataclass(frozen=True, slots=True)
class DecisionGold:
    item_id: str
    label: DecisionLabel


@dataclass(frozen=True, slots=True)
class StateGold:
    sample_id: str
    query_id: str
    annotation_source: AnnotationSource
    relations: tuple[RelationGold, ...]
    decisions: tuple[DecisionGold, ...]
    effective_item_ids: tuple[str, ...] | None
    relation_universe_closed: bool = False
    # Empty relations are scoreable only when the pair/attribute universe is closed.


@dataclass(frozen=True, slots=True)
class UnitResult:
    unit: str
    status: str
    gold_label: str | None
    predicted_label: str | None
    correct: bool | None


@dataclass(frozen=True, slots=True)
class ClassificationMetrics:
    name: str
    status: str
    total_gold_units: int
    mapped_units: int
    candidate_covered_units: int
    scored_units: int
    model_error_units: int
    extraction_missing_units: int
    candidate_missing_units: int
    unmapped_units: int
    end_to_end_accuracy: float | None
    candidate_conditioned_accuracy: float | None
    confusion: tuple[tuple[str, str, int], ...]
    units: tuple[UnitResult, ...]
    unexpected_predictions: int = 0


@dataclass(frozen=True, slots=True)
class EffectiveSetMetrics:
    status: str
    total_gold_units: int
    mapped_units: int
    candidate_covered_units: int
    scored_units: int
    model_error_units: int
    gold_size: int | None
    predicted_size: int | None
    precision: float | None
    recall: float | None
    f1: float | None
    exact_match: bool | None
    conditional_precision: float | None = None
    conditional_recall: float | None = None
    conditional_f1: float | None = None
    conditional_exact_match: bool | None = None


@dataclass(frozen=True, slots=True)
class StateMetricReport:
    annotation_source: str | None
    relation_label_metrics: ClassificationMetrics
    decision_metrics: ClassificationMetrics
    effective_set_metrics: EffectiveSetMetrics


def validate_gold_memory_mapping(catalog: MemoryCatalog, mappings: tuple[GoldItemMapping, ...],
                                 sources: SourceRepository) -> dict[str, str | None]:
    """Reject mismatched provenance; never infer ID from matching text or value."""
    if (type(catalog) is not MemoryCatalog or type(sources) is not SourceRepository or not catalog.sample_id or not catalog.fingerprint or
            not catalog.data_fingerprint or type(catalog.complete) is not bool or type(mappings) is not tuple):
        raise EvaluationInputError("MEMORY_CATALOG_INVALID")
    ids = {}
    for memory in catalog.memories:
        if type(memory) is not MemoryRecord or memory.sample_id != catalog.sample_id or memory.memory_id in ids:
            raise EvaluationInputError("MEMORY_CATALOG_INVALID")
        for ref in memory.source_refs:
            try:
                turn = sources.get_turn(catalog.sample_id, ref)
                if (turn.sample_id, turn.session_id, turn.dia_id) != (catalog.sample_id, ref.session_id, ref.dia_id):
                    raise ValueError
            except Exception:
                raise EvaluationInputError("MEMORY_CATALOG_SOURCE_INVALID") from None
        ids[memory.memory_id] = memory
    output: dict[str, str | None] = {}
    claimed: set[str] = set()
    for row in mappings:
        if (type(row) is not GoldItemMapping or not row.gold_item_id or row.gold_item_id in output or
                row.sample_id != catalog.sample_id or row.data_fingerprint != catalog.data_fingerprint or
                row.memory_fingerprint != catalog.fingerprint or not row.source_refs or
                not row.entity or not row.attribute or not row.value or not row.match_basis or
                row.annotation_source not in ("course_gold", "team_annotated", "synthetic_fixture") or
                (row.annotation_source == "team_annotated" and (not row.annotator or not row.reviewer)) or
                len(row.matched_memory_ids) != len(set(row.matched_memory_ids)) or
                (row.match_status == "matched" and len(row.matched_memory_ids) != 1) or
                (row.match_status == "ambiguous" and len(row.matched_memory_ids) < 2) or
                (row.match_status in ("unmapped", "extraction_missing") and row.matched_memory_ids) or
                (row.match_status == "extraction_missing" and not catalog.complete) or
                row.match_status not in ("matched", "ambiguous", "unmapped", "extraction_missing")):
            raise EvaluationInputError("GOLD_MAPPING_INVALID")
        for ref in row.source_refs:
            try:
                turn = sources.get_turn(row.sample_id, ref)
                if (turn.sample_id, turn.session_id, turn.dia_id) != (row.sample_id, ref.session_id, ref.dia_id):
                    raise ValueError
            except Exception:
                raise EvaluationInputError("GOLD_MAPPING_SOURCE_INVALID") from None
        if any(mid not in ids for mid in row.matched_memory_ids):
            raise EvaluationInputError("GOLD_MAPPING_ID_INVALID")
        if len(row.matched_memory_ids) == 1:
            mid = row.matched_memory_ids[0]
            if mid in claimed or not {(r.session_id, r.dia_id) for r in row.source_refs} & {
                (r.session_id, r.dia_id) for r in ids[mid].source_refs}:
                raise EvaluationInputError("GOLD_MAPPING_CONFLICT")
            claimed.add(mid)
        output[row.gold_item_id] = row.matched_memory_ids[0] if len(row.matched_memory_ids) == 1 else None
    return output


def _classification(name: str, rows: tuple[UnitResult, ...], unexpected: int = 0) -> ClassificationMetrics:
    total = len(rows)
    mapped = sum(r.status not in ("unmapped", "extraction_missing") for r in rows)
    covered = sum(r.status in ("scored", "model_error") for r in rows)
    scored = sum(r.status == "scored" for r in rows)
    errors = sum(r.status == "model_error" for r in rows)
    correct = sum(r.correct is True for r in rows)
    confusion = {}
    for row in rows:
        if row.status == "scored":
            key = (row.gold_label, row.predicted_label)
            confusion[key] = confusion.get(key, 0) + 1
    return ClassificationMetrics(name, "scored" if total else "gold_empty", total, mapped, covered, scored,
        errors, sum(r.status == "extraction_missing" for r in rows),
        sum(r.status == "candidate_missing" for r in rows), sum(r.status == "unmapped" for r in rows),
        correct / total if total else None, correct / covered if covered else None,
        tuple((a, b, n) for (a, b), n in sorted(confusion.items())), rows, unexpected)


def _relation_key(a: str, b: str, attribute: str, label: str) -> tuple[str, str, str]:
    if label == "CONFLICT" or label == "NO_RELATION":
        a, b = sorted((a, b))
    return a, b, attribute


def _check_gold_decisions_effective(gold: StateGold) -> None:
    """Explicit decision labels must agree with an available effective-set Gold.

    Decision annotations may be partial: effective IDs without a corresponding
    decision do not imply missing or invented RETAIN labels.
    """
    effective_items = gold.effective_item_ids
    if effective_items is None:
        return
    if type(effective_items) is not tuple or len(effective_items) != len(set(effective_items)):
        raise EvaluationInputError("EFFECTIVE_GOLD_DUPLICATE")
    if any(type(item_id) is not str or not item_id for item_id in effective_items):
        raise EvaluationInputError("EFFECTIVE_GOLD_INVALID")
    effective = set(effective_items)
    for decision in gold.decisions:
        if (type(decision) is not DecisionGold or not decision.item_id or
                decision.label not in ("RETAIN", "EXCLUDE", "UNRESOLVED")):
            raise EvaluationInputError("DECISION_GOLD_INVALID")
        if (decision.label == "RETAIN") != (decision.item_id in effective):
            raise EvaluationInputError("STATE_GOLD_INCONSISTENT")


def score_state(response: QueryResponse, gold: StateGold | None, catalog: MemoryCatalog,
                mappings: tuple[GoldItemMapping, ...], sources: SourceRepository) -> StateMetricReport:
    if type(response) is not QueryResponse or response.method != "dynamic":
        raise EvaluationInputError("STATE_RESPONSE_INVALID")
    if gold is None:
        blank = ClassificationMetrics("relation_label_metrics", "gold_unavailable", 0, 0, 0, 0, 0, 0, 0, 0,
                                      None, None, (), ())
        decision = ClassificationMetrics("decision_metrics", "gold_unavailable", 0, 0, 0, 0, 0, 0, 0, 0,
                                         None, None, (), ())
        return StateMetricReport(None, blank, decision,
                                 EffectiveSetMetrics("gold_unavailable", 0, 0, 0, 0, 0, None, None,
                                                     None, None, None, None))
    if (type(gold) is not StateGold or gold.sample_id != response.sample_id or gold.query_id != response.query_id or
            gold.annotation_source not in ("course_gold", "team_annotated", "synthetic_fixture") or
            catalog.sample_id != response.sample_id or any(m.annotation_source != gold.annotation_source for m in mappings)):
        raise EvaluationInputError("STATE_GOLD_INVALID")
    _check_gold_decisions_effective(gold)
    mapped = validate_gold_memory_mapping(catalog, mappings, sources)
    mapping_by_item = {m.gold_item_id: m for m in mappings}
    candidate_ids = {item.memory_id for item in response.retrieved.items} if response.retrieved else set()
    catalog_ids = {m.memory_id for m in catalog.memories}
    if not candidate_ids <= catalog_ids:
        raise EvaluationInputError("STATE_CANDIDATE_ID_INVALID")
    if response.resolution and response.retrieved:
        try:
            validate_resolution_against_candidates(response.resolution, response.retrieved)
        except ValueError:
            raise EvaluationInputError("STATE_RESOLUTION_INVALID") from None
    if response.resolution and response.retrieved is None:
        raise EvaluationInputError("STATE_RESOLUTION_INVALID")
    prediction_failed = response.resolution is None

    def identity_status(items: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
        if any(item not in mapped for item in items):
            raise EvaluationInputError("STATE_GOLD_ITEM_UNKNOWN")
        ids = tuple(mapped[item] for item in items)
        if any(mid is None for mid in ids):
            return ("extraction_missing" if all(
                mapping_by_item[item].match_status == "extraction_missing"
                for item in items if mapped[item] is None) else "unmapped"), ()
        if any(mid not in candidate_ids for mid in ids):
            return "candidate_missing", ids
        return ("model_error" if prediction_failed else "scored"), ids

    relation_rows = []
    relation_seen = set()
    for item in gold.relations:
        if (type(item) is not RelationGold or item.label not in ("UPDATE", "SUPERSEDE", "CONFLICT", "NO_RELATION") or
                not item.scope_attribute or item.old_item_id == item.new_item_id or
                (item.label == "NO_RELATION" and not gold.relation_universe_closed)):
            raise EvaluationInputError("RELATION_GOLD_INVALID")
        status, ids = identity_status((item.old_item_id, item.new_item_id))
        key = _relation_key(*ids, item.scope_attribute, item.label) if ids else (item.old_item_id, item.new_item_id, item.scope_attribute)
        identity = (tuple(sorted(ids)) if ids else tuple(sorted((item.old_item_id, item.new_item_id))), item.scope_attribute)
        if identity in relation_seen:
            raise EvaluationInputError("RELATION_GOLD_CONFLICT")
        relation_seen.add(identity)
        predicted = None
        if status == "scored":
            labels = [edge.relation for edge in response.resolution.relations
                      if (edge.scope_attribute == item.scope_attribute and
                          (({edge.from_memory_id, edge.to_memory_id} == set(ids)) if item.label in ("CONFLICT", "NO_RELATION")
                           else (edge.from_memory_id, edge.to_memory_id) == ids))]
            if len(labels) > 1:
                raise EvaluationInputError("RELATION_PREDICTION_CONFLICT")
            predicted = labels[0] if labels else "NO_RELATION"
        relation_rows.append(UnitResult(str(key), status, item.label, predicted,
                                        predicted == item.label if status == "scored" else None))
    unexpected = 0
    if response.resolution:
        # Wrong direction or attribute becomes a false positive and the expected
        # edge stays a false negative; closed negative units supply explicit TNs.
        allowed = {_relation_key(*identity_status((r.old_item_id, r.new_item_id))[1], r.scope_attribute, r.label)
                   for r in gold.relations if identity_status((r.old_item_id, r.new_item_id))[1]}
        for edge in response.resolution.relations:
            key = _relation_key(edge.from_memory_id, edge.to_memory_id, edge.scope_attribute, edge.relation)
            if key not in allowed:
                unexpected += 1
    decision_rows = []
    decision_seen = set()
    for item in gold.decisions:
        if type(item) is not DecisionGold or item.label not in ("RETAIN", "EXCLUDE", "UNRESOLVED") or item.item_id in decision_seen:
            raise EvaluationInputError("DECISION_GOLD_INVALID")
        decision_seen.add(item.item_id)
        status, ids = identity_status((item.item_id,))
        predicted = next((d.action for d in response.resolution.decisions if d.memory_id == ids[0]), None) if status == "scored" else None
        decision_rows.append(UnitResult(item.item_id, status, item.label, predicted,
                                        predicted == item.label if status == "scored" else None))
    relations = _classification("relation_label_metrics", tuple(relation_rows), unexpected)
    decisions = _classification("decision_metrics", tuple(decision_rows))
    effective = _effective(response, gold, mapped, identity_status)
    return StateMetricReport(gold.annotation_source, relations, decisions, effective)


def _effective(response, gold, mapped, identity_status) -> EffectiveSetMetrics:
    if gold.effective_item_ids is None:
        return EffectiveSetMetrics("gold_unavailable", 0, 0, 0, 0, 0, None, None, None, None, None, None)
    if len(gold.effective_item_ids) != len(set(gold.effective_item_ids)):
        raise EvaluationInputError("EFFECTIVE_GOLD_DUPLICATE")
    status, ids = identity_status(gold.effective_item_ids)
    if status in ("unmapped", "extraction_missing"):
        return EffectiveSetMetrics(status, 1, 0, 0, 0, 0, None, None, None, None, None, None)
    if status == "model_error":
        return EffectiveSetMetrics(status, 1, 1, 1, 0, 1, len(ids), None, 0.0, 0.0, 0.0, False)
    if response.resolution is None:
        return EffectiveSetMetrics("model_error", 1, 1, 0, 0, 1, len(ids), None, 0.0, 0.0, 0.0, False)
    expected = set(ids)
    actual = set(response.resolution.effective_memory_ids)
    hit = len(expected & actual)
    # Both empty is a correct exact set, with P=R=F1=1 by frozen convention.
    p = hit / len(actual) if actual else (1.0 if not expected else 0.0)
    r = hit / len(expected) if expected else (1.0 if not actual else 0.0)
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    return EffectiveSetMetrics(status, 1, 1, int(status == "scored"), 1, 0,
                               len(expected), len(actual), p, r, f1, actual == expected,
                               p if status == "scored" else None, r if status == "scored" else None,
                               f1 if status == "scored" else None,
                               actual == expected if status == "scored" else None)
