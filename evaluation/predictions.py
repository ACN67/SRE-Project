"""Atomic prediction artifact and byte-bound receipt for later Gold access."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from pydantic import BaseModel

from dynamic_memory.contracts import QueryResponse

from .question_manifest import EvaluationInputError, QuestionManifest, _pairs

_METHODS = frozenset({"full_text", "vector_rag", "dynamic", "pure_vector_topk"})


@dataclass(frozen=True, slots=True)
class PredictionReceipt:
    path: Path
    file_sha256: str
    snapshot_sha256: str
    method: str
    query_ids: tuple[str, ...]


def _target(path: str | Path) -> Path:
    try:
        target = Path(path).resolve()
        results = (Path(__file__).resolve().parents[1] / "results").resolve()
        temp = Path(tempfile.gettempdir()).resolve()
        if (not (target.is_relative_to(results) or target.is_relative_to(temp)) or
                not target.name.endswith(".predictions.json")):
            raise EvaluationInputError("PREDICTION_PATH_INVALID")
        return target
    except (OSError, TypeError, ValueError):
        raise EvaluationInputError("PREDICTION_PATH_INVALID") from None


def _reject_extra(value: object) -> None:
    if isinstance(value, BaseModel):
        if set(value.__dict__) != set(type(value).model_fields) or value.__pydantic_extra__:
            raise EvaluationInputError("PREDICTION_INVALID")
        for child in value.__dict__.values():
            _reject_extra(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_extra(child)
    elif isinstance(value, dict):
        for child in value.values():
            _reject_extra(child)


def _check_rows(manifest: QuestionManifest, responses: Iterable[QueryResponse], method: str) -> tuple[QueryResponse, ...]:
    if not isinstance(manifest, QuestionManifest) or method not in _METHODS:
        raise EvaluationInputError("PREDICTION_INVALID")
    expected = {record.query_id: record for record in manifest.records}
    if not expected or len(expected) != len(manifest.records):
        raise EvaluationInputError("MANIFEST_INVALID")
    checked = []
    seen = set()
    try:
        for response in responses:
            if type(response) is not QueryResponse:
                raise EvaluationInputError("PREDICTION_INVALID")
            _reject_extra(response)
            response = QueryResponse.model_validate_json(response.model_dump_json())
            record = expected.get(response.query_id)
            if (response.query_id in seen or record is None or response.sample_id != record.sample_id or
                    response.question != record.question or response.method != method):
                raise EvaluationInputError("PREDICTION_ID_MISMATCH")
            seen.add(response.query_id)
            checked.append(response)
    except EvaluationInputError:
        raise
    except Exception:
        raise EvaluationInputError("PREDICTION_INVALID") from None
    if seen != set(expected):
        raise EvaluationInputError("PREDICTION_INCOMPLETE")
    return tuple(checked)


def _payload(manifest: QuestionManifest, rows: tuple[QueryResponse, ...], method: str) -> dict:
    by_id = {row.query_id: row for row in rows}
    return {
        "format_version": 1, "snapshot_sha256": manifest.snapshot_sha256,
        "method": method, "predictions": [
            {"gold_record_key": record.gold_record_key,
             "response": by_id[record.query_id].model_dump(mode="json")}
            for record in manifest.records
        ],
    }


def save_predictions(path: str | Path, manifest: QuestionManifest,
                     responses: Iterable[QueryResponse], method: str) -> PredictionReceipt:
    """Save one complete selected manifest; no Gold is loaded or inspected."""
    target = _target(path)
    rows = _check_rows(manifest, responses, method)
    if target.exists():
        raise EvaluationInputError("PREDICTION_PATH_IN_USE")
    raw = json.dumps(_payload(manifest, rows, method), ensure_ascii=False,
                     sort_keys=True, separators=(",", ":")).encode("utf-8")
    temporary = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="wb", dir=target.parent, prefix=".predictions-",
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        # Publish a fully fsynced file without replacing an existing target.
        # Hard-link creation is atomic and fails if another writer wins the race.
        os.link(temporary, target)
    except FileExistsError:
        raise EvaluationInputError("PREDICTION_PATH_IN_USE") from None
    except OSError:
        raise EvaluationInputError("PREDICTION_SAVE_FAILED") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return PredictionReceipt(target, hashlib.sha256(raw).hexdigest(),
                             manifest.snapshot_sha256, method,
                             tuple(record.query_id for record in manifest.records))


def verify_predictions(path: str | Path, manifest: QuestionManifest,
                       receipt: PredictionReceipt) -> tuple[QueryResponse, ...]:
    """Reopen persisted bytes and verify digest, schema and exact question mapping."""
    target = _target(path)
    if (type(receipt) is not PredictionReceipt or receipt.path != target or
            receipt.snapshot_sha256 != manifest.snapshot_sha256 or
            receipt.query_ids != tuple(record.query_id for record in manifest.records) or
            receipt.method not in _METHODS):
        raise EvaluationInputError("PREDICTION_RECEIPT_INVALID")
    try:
        raw = target.read_bytes()
    except OSError:
        raise EvaluationInputError("PREDICTION_NOT_FOUND") from None
    if hashlib.sha256(raw).hexdigest() != receipt.file_sha256:
        raise EvaluationInputError("PREDICTION_HASH_MISMATCH")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
        if (not isinstance(payload, dict) or set(payload) != {"format_version", "snapshot_sha256", "method", "predictions"} or
                type(payload["format_version"]) is not int or payload["format_version"] != 1 or
                payload["snapshot_sha256"] != manifest.snapshot_sha256 or payload["method"] != receipt.method or
                not isinstance(payload["predictions"], list) or len(payload["predictions"]) != len(manifest.records)):
            raise EvaluationInputError("PREDICTION_INVALID")
        rows = []
        for record, entry in zip(manifest.records, payload["predictions"]):
            if not isinstance(entry, dict) or set(entry) != {"gold_record_key", "response"} or entry["gold_record_key"] != record.gold_record_key:
                raise EvaluationInputError("PREDICTION_ID_MISMATCH")
            rows.append(QueryResponse.model_validate(entry["response"], strict=True))
        checked = _check_rows(manifest, rows, receipt.method)
    except EvaluationInputError:
        raise
    except Exception:
        raise EvaluationInputError("PREDICTION_INVALID") from None
    if json.dumps(_payload(manifest, checked, receipt.method), ensure_ascii=False,
                  sort_keys=True, separators=(",", ":")).encode("utf-8") != raw:
        raise EvaluationInputError("PREDICTION_INVALID")
    return checked
