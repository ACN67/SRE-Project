"""Frozen QA question identities. Public results contain no Gold fields."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from dynamic_memory.contracts import QueryRequest
from dynamic_memory.data.snapshot import inspect_snapshot, validate_split
from dynamic_memory.pipeline.settings import DataSplitSettings

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*\Z")
_GROUPS = ("main_dev", "extra_dev", "test")
_NON_ID_FIELDS = frozenset({"question", "answer", "evidence", "category",
                           "observation", "session_summary", "event_summary"})


class EvaluationInputError(ValueError):
    """Content-free, stable evaluation-side failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"{code}: evaluation input is invalid")


@dataclass(frozen=True, slots=True)
class QuestionRecord:
    sample_id: str
    query_id: str
    original_qa_index: int
    question: str
    gold_record_key: str
    split: str

    def to_query_request(self) -> QueryRequest:
        return QueryRequest(sample_id=self.sample_id, query_id=self.query_id,
                            question=self.question, as_of=None)


@dataclass(frozen=True, slots=True)
class QuestionManifest:
    snapshot_sha256: str
    records: tuple[QuestionRecord, ...]
    selected_splits: tuple[str, ...]
    official_id_field: str | None


def _pairs(values: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in values:
        if key in result:
            raise EvaluationInputError("QA_FORMAT_INVALID")
        result[key] = value
    return result


def _verified_source(snapshot_path: str | Path, split_path: str | Path):
    """Reuse B's raw-byte SHA/order and frozen split checks before QA access."""
    try:
        info = inspect_snapshot(snapshot_path)
        split_doc = yaml.safe_load(Path(split_path).read_text(encoding="utf-8"))
        split = DataSplitSettings.model_validate(split_doc)
        validate_split(info, split)
        raw = Path(snapshot_path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != info.sha256:
            raise EvaluationInputError("DATA_SNAPSHOT_MISMATCH")
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                              parse_constant=lambda _: (_ for _ in ()).throw(EvaluationInputError("QA_FORMAT_INVALID")))
        if not isinstance(document, list) or len(document) != len(info.sample_ids):
            raise EvaluationInputError("QA_FORMAT_INVALID")
        if tuple(item.get("sample_id") if isinstance(item, dict) else None for item in document) != info.sample_ids:
            raise EvaluationInputError("DATA_SNAPSHOT_MISMATCH")
    except EvaluationInputError:
        raise
    except Exception as exc:
        code = getattr(exc, "code", None)
        if code not in {"DATA_FILE_NOT_FOUND", "DATA_FORMAT_INVALID", "DATA_SNAPSHOT_MISMATCH",
                        "SPLIT_UNFROZEN", "SPLIT_MISMATCH", "DUPLICATE_SAMPLE_ID"}:
            code = "QA_FORMAT_INVALID"
        raise EvaluationInputError(code) from None
    split_by_id = {sample_id: name for name in _GROUPS
                   for sample_id in getattr(split.groups, name).sample_ids}
    return info, document, split_by_id


def _query_id(sample_id: str, index: int, qa: dict, official_id_field: str | None) -> str:
    if official_id_field is None:
        value = f"qa_{sample_id}_{index}"
    else:
        if official_id_field not in qa:
            raise EvaluationInputError("OFFICIAL_ID_MISSING")
        original = qa[official_id_field]
        if isinstance(original, bool) or not isinstance(original, (str, int)):
            raise EvaluationInputError("OFFICIAL_ID_INVALID")
        value = str(original)
    if not _ID.fullmatch(value) or value.startswith("interactive_"):
        raise EvaluationInputError("QUERY_ID_INVALID")
    return value


def _key(sha: str, sample_id: str, index: int, query_id: str) -> str:
    canonical = json.dumps([sha, sample_id, index, query_id], separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def make_question_record(*, snapshot_sha256: str, sample_id: str, original_qa_index: int,
                         question: str, split: str, query_id: str | None = None) -> QuestionRecord:
    """Create a Gold-free evaluation record from an already selected question."""
    if (not isinstance(snapshot_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", snapshot_sha256)
            or not isinstance(sample_id, str) or not _ID.fullmatch(sample_id)
            or type(original_qa_index) is not int or original_qa_index < 0
            or not isinstance(question, str) or not question.strip()
            or split not in _GROUPS):
        raise EvaluationInputError("QUESTION_RECORD_INVALID")
    identifier = query_id or f"qa_{sample_id}_{original_qa_index}"
    if not isinstance(identifier, str) or not _ID.fullmatch(identifier) or identifier.startswith("interactive_"):
        raise EvaluationInputError("QUERY_ID_INVALID")
    return QuestionRecord(
        sample_id=sample_id,
        query_id=identifier,
        original_qa_index=original_qa_index,
        question=question,
        gold_record_key=_key(snapshot_sha256, sample_id, original_qa_index, identifier),
        split=split,
    )


def _manifest_from_verified(info, document, split_by_id, selected_splits, official_id_field):
    if not selected_splits or len(set(selected_splits)) != len(selected_splits) or any(
        split not in _GROUPS for split in selected_splits
    ):
        raise EvaluationInputError("SPLIT_SELECTION_INVALID")
    if official_id_field is not None and (not isinstance(official_id_field, str) or
                                          not _ID.fullmatch(official_id_field) or
                                          official_id_field.lower() in _NON_ID_FIELDS):
        raise EvaluationInputError("OFFICIAL_ID_FIELD_INVALID")
    records = []
    seen_ids = set()
    seen_keys = set()
    for item in document:
        sample_id = item["sample_id"]
        qa_rows = item.get("qa")
        if not isinstance(qa_rows, list) or not qa_rows:
            raise EvaluationInputError("QA_EMPTY_OR_INVALID")
        for index, qa in enumerate(qa_rows):
            if not isinstance(qa, dict) or not isinstance(qa.get("question"), str) or not qa["question"].strip():
                raise EvaluationInputError("QA_FORMAT_INVALID")
            query_id = _query_id(sample_id, index, qa, official_id_field)
            key = _key(info.sha256, sample_id, index, query_id)
            if query_id in seen_ids or key in seen_keys:
                raise EvaluationInputError("DUPLICATE_QA_ID")
            seen_ids.add(query_id)
            seen_keys.add(key)
            if split_by_id[sample_id] in selected_splits:
                records.append(QuestionRecord(sample_id, query_id, index, qa["question"],
                                              key, split_by_id[sample_id]))
    return QuestionManifest(info.sha256, tuple(records), tuple(selected_splits), official_id_field)


def verify_manifest_selection(manifest: QuestionManifest, info, document,
                              split_by_id) -> QuestionManifest:
    """Verify that a manifest is an ordered subset of one frozen split."""
    if type(manifest) is not QuestionManifest:
        raise EvaluationInputError("MANIFEST_INVALID")
    full = _manifest_from_verified(
        info, document, split_by_id, manifest.selected_splits,
        manifest.official_id_field,
    )
    if (
        full.snapshot_sha256 != manifest.snapshot_sha256
        or full.selected_splits != manifest.selected_splits
        or full.official_id_field != manifest.official_id_field
        or not manifest.records
    ):
        raise EvaluationInputError("MANIFEST_MISMATCH")
    by_id = {record.query_id: record for record in full.records}
    selected = set()
    for record in manifest.records:
        expected = by_id.get(record.query_id)
        if (
            expected is None
            or record.query_id in selected
            or record != expected
        ):
            raise EvaluationInputError("MANIFEST_MISMATCH")
        selected.add(record.query_id)
    if tuple(record for record in full.records if record.query_id in selected) != manifest.records:
        raise EvaluationInputError("MANIFEST_MISMATCH")
    return manifest


def load_question_manifest(snapshot_path: str | Path, split_path: str | Path, *,
                           selected_splits: tuple[str, ...] = _GROUPS,
                           official_id_field: str | None = None) -> QuestionManifest:
    """Use an official ID field only after its stability has been verified externally."""
    info, document, split_by_id = _verified_source(snapshot_path, split_path)
    return _manifest_from_verified(info, document, split_by_id,
                                   selected_splits, official_id_field)
