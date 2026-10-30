"""Deterministic, Gold-free supplementary raw-vector Top-K run."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from dynamic_memory.contracts import AnswerResult, ErrorInfo, QueryResponse
from dynamic_memory.pipeline.query_runner import QueryOrchestrator
from evaluation.predictions import PredictionReceipt, _check_rows, save_predictions, verify_predictions
from evaluation.question_manifest import EvaluationInputError, QuestionManifest

from .three_method_runner import RunProtocol, _json_bytes, _publish_new, _target, _validate

METHOD = "pure_vector_topk"


@dataclass(frozen=True, slots=True)
class PureVectorRunResult:
    directory: Path
    manifest_path: Path
    manifest_sha256: str
    receipt: PredictionReceipt
    responses: tuple[QueryResponse, ...]


def _fallback(record, code: str) -> QueryResponse:
    return QueryResponse(query_id=record.query_id, sample_id=record.sample_id,
        question=record.question, method=METHOD, query_analysis=None, retrieved=None,
        resolution=None, answer_result=AnswerResult(status="error", error=ErrorInfo(
            code=code, message="Batch query preparation failed.")))


def _payload(manifest: QuestionManifest, protocol: RunProtocol, receipt: PredictionReceipt,
             rows: tuple[QueryResponse, ...], audit: list[dict]) -> dict:
    codes = sorted({row.answer_result.error.code for row in rows if row.answer_result.error is not None})
    return {
        "format_version": 1, "run_id": protocol.run_id, "method": METHOD,
        "ablation_type": "supplementary_ablation", "main_experiment": False,
        "retrieval": "raw_embedding_topk", "history_expansion": False, "state_resolution": False,
        "base_top_k": protocol.base_top_k, "snapshot_sha256": manifest.snapshot_sha256,
        "selected_splits": list(manifest.selected_splits),
        "query_ids": [record.query_id for record in manifest.records],
        "prompt_fingerprint": protocol.prompt_fingerprint,
        "model_fingerprint": protocol.model_fingerprint,
        "model_settings_fingerprint": protocol.model_settings_fingerprint,
        "retriever_fingerprint": protocol.retriever_fingerprint,
        "index_fingerprint": protocol.index_fingerprint,
        "time_policy": protocol.time_policy,
        "execution_kind": protocol.execution_kind, "provider_status": protocol.provider_status,
        "initial_topk_ids": audit,
        "excluded_candidates": sum(item["excluded_candidates"] for item in audit),
        "prediction_file": f"{METHOD}.predictions.json",
        "prediction_sha256": receipt.file_sha256,
        "answer_status_counts": {status: sum(row.answer_result.status == status for row in rows)
                                 for status in ("answered", "no_evidence", "time_uncertain", "unresolved_conflict", "error")},
        "error_code_counts": {code: sum(row.answer_result.error is not None and
                                         row.answer_result.error.code == code for row in rows)
                              for code in codes},
    }


def run_pure_vector_ablation(manifest: QuestionManifest, orchestrator: QueryOrchestrator,
                             protocol: RunProtocol, *, results_root: Path | str) -> PureVectorRunResult:
    _validate(manifest, protocol)
    if type(orchestrator) is not QueryOrchestrator:
        raise EvaluationInputError("RUN_ORCHESTRATOR_INVALID")
    target = _target(results_root, protocol.run_id)
    try:
        target.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise EvaluationInputError("RUN_ID_EXISTS") from None
    except OSError:
        raise EvaluationInputError("RUN_PATH_INVALID") from None
    responses = []
    audit = []
    for record in manifest.records:
        try:
            prepared = orchestrator.prepare_query(record.to_query_request())
        except Exception:
            response = _fallback(record, "BATCH_PREPARE_FAILED")
            ids, excluded = (), 0
        else:
            try:
                result = orchestrator.run_pure_vector_prepared(
                    prepared, base_top_k=protocol.base_top_k,
                    retriever_fingerprint=protocol.retriever_fingerprint)
                response, ids, excluded = result.response, result.initial_topk_ids, result.excluded_candidates
            except Exception:
                response = _fallback(record, "BATCH_METHOD_FAILED")
                ids, excluded = (), 0
        responses.append(response)
        audit.append({"query_id": record.query_id, "ids": list(ids), "excluded_candidates": excluded})
    rows = _check_rows(manifest, responses, METHOD)
    prediction_path = target / f"{METHOD}.predictions.json"
    receipt = save_predictions(prediction_path, manifest, rows, METHOD)
    verify_predictions(prediction_path, manifest, receipt)
    raw = _json_bytes(_payload(manifest, protocol, receipt, rows, audit))
    digest = hashlib.sha256(raw).hexdigest()
    _publish_new(target / "run_manifest.sha256", (digest + "\n").encode("ascii"))
    manifest_path = target / "run_manifest.json"
    _publish_new(manifest_path, raw)
    return PureVectorRunResult(target, manifest_path, digest, receipt, rows)


def verify_pure_vector_run(directory: Path | str, manifest: QuestionManifest, *,
                           expected_manifest_sha256: str | None = None) -> PureVectorRunResult:
    try:
        path = Path(directory).resolve()
        if _target(path.parent, path.name) != path:
            raise EvaluationInputError("RUN_PATH_INVALID")
        raw = (path / "run_manifest.json").read_bytes()
        recorded = (path / "run_manifest.sha256").read_text(encoding="ascii")
    except EvaluationInputError:
        raise
    except (OSError, UnicodeError):
        raise EvaluationInputError("RUN_INCOMPLETE") from None
    digest = hashlib.sha256(raw).hexdigest()
    if recorded != digest + "\n" or expected_manifest_sha256 is not None and digest != expected_manifest_sha256:
        raise EvaluationInputError("RUN_MANIFEST_HASH_MISMATCH")
    try:
        data = json.loads(raw.decode("utf-8"))
        if _json_bytes(data) != raw or data["method"] != METHOD or data["prediction_file"] != f"{METHOD}.predictions.json":
            raise ValueError
        protocol = RunProtocol(data["run_id"], data["snapshot_sha256"], tuple(data["selected_splits"]),
            data["prompt_fingerprint"], data["model_fingerprint"], data["model_settings_fingerprint"],
            data["retriever_fingerprint"], data["index_fingerprint"], data["time_policy"],
            data["base_top_k"], data["base_top_k"], data["execution_kind"], data["provider_status"])
        _validate(manifest, protocol)
        receipt = PredictionReceipt(path / data["prediction_file"], data["prediction_sha256"],
            manifest.snapshot_sha256, METHOD, tuple(data["query_ids"]))
        rows = verify_predictions(receipt.path, manifest, receipt)
        audit = data["initial_topk_ids"]
        if len(audit) != len(rows):
            raise ValueError
        for item, row in zip(audit, rows):
            if (set(item) != {"query_id", "ids", "excluded_candidates"} or
                    item["query_id"] != row.query_id or type(item["excluded_candidates"]) is not int or
                    item["excluded_candidates"] < 0 or
                    item["ids"] != ([candidate.memory_id for candidate in row.retrieved.items] if row.retrieved else [])):
                raise ValueError
        if data != _payload(manifest, protocol, receipt, rows, audit):
            raise ValueError
    except EvaluationInputError:
        raise
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        raise EvaluationInputError("RUN_MANIFEST_INVALID") from None
    return PureVectorRunResult(path, path / "run_manifest.json", digest, receipt, rows)
