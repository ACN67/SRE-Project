"""Strict loader for reviewed dynamic-state annotations."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re

from dynamic_memory.contracts import SourceRef
from evaluation.question_manifest import EvaluationInputError

from .state_metrics import (
    DecisionGold,
    GoldItemMapping,
    RelationGold,
    StateGold,
)


_SHA = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")


@dataclass(frozen=True, slots=True)
class StateAnnotationBundle:
    annotation_source: str
    annotator: str
    reviewer: str
    data_fingerprint: str
    memory_fingerprints: dict[str, str]
    mappings_by_sample: dict[str, tuple[GoldItemMapping, ...]]
    gold_by_query: dict[tuple[str, str], StateGold]


def _object(value: object, keys: set[str], code: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise EvaluationInputError(code)
    return value


def _text(value: object, code: str) -> str:
    if type(value) is not str or not value.strip():
        raise EvaluationInputError(code)
    return value


def _refs(value: object) -> tuple[SourceRef, ...]:
    if type(value) is not list or not value:
        raise EvaluationInputError("STATE_ANNOTATION_REF_INVALID")
    try:
        refs = tuple(SourceRef.model_validate(item, strict=True) for item in value)
    except Exception:
        raise EvaluationInputError("STATE_ANNOTATION_REF_INVALID") from None
    if len({(ref.session_id, ref.dia_id) for ref in refs}) != len(refs):
        raise EvaluationInputError("STATE_ANNOTATION_REF_INVALID")
    return refs


def load_state_annotations(path: str | Path) -> StateAnnotationBundle:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise EvaluationInputError("STATE_ANNOTATION_READ_FAILED") from None
    document = _object(
        document,
        {
            "format_version",
            "annotation_source",
            "annotator",
            "reviewer",
            "created_at",
            "protocol",
            "data_fingerprint",
            "memory_fingerprints",
            "items",
            "queries",
        },
        "STATE_ANNOTATION_INVALID",
    )
    if document["format_version"] != 1:
        raise EvaluationInputError("STATE_ANNOTATION_INVALID")
    source = document["annotation_source"]
    if source not in ("course_gold", "team_annotated", "synthetic_fixture"):
        raise EvaluationInputError("STATE_ANNOTATION_INVALID")
    annotator = _text(document["annotator"], "STATE_ANNOTATION_INVALID")
    reviewer = _text(document["reviewer"], "STATE_ANNOTATION_INVALID")
    if not _IDENTITY.fullmatch(annotator) or not _IDENTITY.fullmatch(reviewer):
        raise EvaluationInputError("STATE_ANNOTATION_INVALID")
    _text(document["created_at"], "STATE_ANNOTATION_INVALID")
    _text(document["protocol"], "STATE_ANNOTATION_INVALID")
    data_fingerprint = _text(
        document["data_fingerprint"], "STATE_ANNOTATION_INVALID"
    )
    if not _SHA.fullmatch(data_fingerprint):
        raise EvaluationInputError("STATE_ANNOTATION_INVALID")
    if type(document["memory_fingerprints"]) is not dict:
        raise EvaluationInputError("STATE_ANNOTATION_INVALID")
    memory_fingerprints = {}
    for sample_id, fingerprint in document["memory_fingerprints"].items():
        if (
            type(sample_id) is not str
            or not _IDENTITY.fullmatch(sample_id)
            or type(fingerprint) is not str
            or not _SHA.fullmatch(fingerprint)
        ):
            raise EvaluationInputError("STATE_ANNOTATION_INVALID")
        memory_fingerprints[sample_id] = fingerprint
    if type(document["items"]) is not list or type(document["queries"]) is not list:
        raise EvaluationInputError("STATE_ANNOTATION_INVALID")

    mappings = []
    item_by_id = {}
    for value in document["items"]:
        value = _object(
            value,
            {
                "gold_item_id",
                "sample_id",
                "source_refs",
                "entity",
                "attribute",
                "value",
                "matched_memory_ids",
                "match_basis",
                "match_status",
                "review_note",
            },
            "STATE_ANNOTATION_ITEM_INVALID",
        )
        gold_item_id = _text(
            value["gold_item_id"], "STATE_ANNOTATION_ITEM_INVALID"
        )
        sample_id = _text(value["sample_id"], "STATE_ANNOTATION_ITEM_INVALID")
        if gold_item_id in item_by_id or sample_id not in memory_fingerprints:
            raise EvaluationInputError("STATE_ANNOTATION_ITEM_INVALID")
        matched = value["matched_memory_ids"]
        if type(matched) is not list or any(type(item) is not str for item in matched):
            raise EvaluationInputError("STATE_ANNOTATION_ITEM_INVALID")
        match_status = value["match_status"]
        if match_status != "matched" or len(matched) != 1:
            raise EvaluationInputError("STATE_ANNOTATION_ITEM_INVALID")
        mapping = GoldItemMapping(
            gold_item_id=gold_item_id,
            sample_id=sample_id,
            source_refs=_refs(value["source_refs"]),
            entity=_text(value["entity"], "STATE_ANNOTATION_ITEM_INVALID"),
            attribute=_text(value["attribute"], "STATE_ANNOTATION_ITEM_INVALID"),
            value=_text(value["value"], "STATE_ANNOTATION_ITEM_INVALID"),
            matched_memory_ids=tuple(matched),
            match_basis=_text(
                value["match_basis"], "STATE_ANNOTATION_ITEM_INVALID"
            ),
            annotation_source=source,
            data_fingerprint=data_fingerprint,
            memory_fingerprint=memory_fingerprints[sample_id],
            match_status="matched",
            annotator=annotator,
            reviewer=reviewer,
            review_note=_text(
                value["review_note"], "STATE_ANNOTATION_ITEM_INVALID"
            ),
        )
        item_by_id[gold_item_id] = mapping
        mappings.append(mapping)

    gold_by_query = {}
    for value in document["queries"]:
        value = _object(
            value,
            {
                "sample_id",
                "query_id",
                "relations",
                "decisions",
                "effective_item_ids",
                "relation_universe_closed",
                "review_note",
            },
            "STATE_ANNOTATION_QUERY_INVALID",
        )
        sample_id = _text(value["sample_id"], "STATE_ANNOTATION_QUERY_INVALID")
        query_id = _text(value["query_id"], "STATE_ANNOTATION_QUERY_INVALID")
        key = (sample_id, query_id)
        if key in gold_by_query or sample_id not in memory_fingerprints:
            raise EvaluationInputError("STATE_ANNOTATION_QUERY_INVALID")
        relations = []
        if type(value["relations"]) is not list:
            raise EvaluationInputError("STATE_ANNOTATION_QUERY_INVALID")
        for row in value["relations"]:
            row = _object(
                row,
                {"old_item_id", "new_item_id", "scope_attribute", "label"},
                "STATE_ANNOTATION_RELATION_INVALID",
            )
            old_item_id = _text(
                row["old_item_id"], "STATE_ANNOTATION_RELATION_INVALID"
            )
            new_item_id = _text(
                row["new_item_id"], "STATE_ANNOTATION_RELATION_INVALID"
            )
            if (
                old_item_id not in item_by_id
                or new_item_id not in item_by_id
                or item_by_id[old_item_id].sample_id != sample_id
                or item_by_id[new_item_id].sample_id != sample_id
            ):
                raise EvaluationInputError("STATE_ANNOTATION_RELATION_INVALID")
            relations.append(
                RelationGold(
                    old_item_id,
                    new_item_id,
                    _text(
                        row["scope_attribute"],
                        "STATE_ANNOTATION_RELATION_INVALID",
                    ),
                    row["label"],
                )
            )
        decisions = []
        if type(value["decisions"]) is not list:
            raise EvaluationInputError("STATE_ANNOTATION_QUERY_INVALID")
        for row in value["decisions"]:
            row = _object(
                row,
                {"item_id", "label"},
                "STATE_ANNOTATION_DECISION_INVALID",
            )
            item_id = _text(
                row["item_id"], "STATE_ANNOTATION_DECISION_INVALID"
            )
            if (
                item_id not in item_by_id
                or item_by_id[item_id].sample_id != sample_id
            ):
                raise EvaluationInputError("STATE_ANNOTATION_DECISION_INVALID")
            decisions.append(DecisionGold(item_id, row["label"]))
        effective = value["effective_item_ids"]
        if effective is not None:
            if type(effective) is not list or any(
                type(item) is not str
                or item not in item_by_id
                or item_by_id[item].sample_id != sample_id
                for item in effective
            ):
                raise EvaluationInputError("STATE_ANNOTATION_QUERY_INVALID")
            effective = tuple(effective)
        if type(value["relation_universe_closed"]) is not bool:
            raise EvaluationInputError("STATE_ANNOTATION_QUERY_INVALID")
        _text(value["review_note"], "STATE_ANNOTATION_QUERY_INVALID")
        gold_by_query[key] = StateGold(
            sample_id,
            query_id,
            source,
            tuple(relations),
            tuple(decisions),
            effective,
            value["relation_universe_closed"],
        )

    mappings_by_sample = {}
    for mapping in mappings:
        mappings_by_sample.setdefault(mapping.sample_id, []).append(mapping)
    return StateAnnotationBundle(
        source,
        annotator,
        reviewer,
        data_fingerprint,
        memory_fingerprints,
        {
            sample_id: tuple(rows)
            for sample_id, rows in mappings_by_sample.items()
        },
        gold_by_query,
    )
