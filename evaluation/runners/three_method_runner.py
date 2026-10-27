"""Gold-free A-17 three-method batch execution over the A-12 public API.

Each method calls run_prepared independently. This module proves observed
candidate identity; it does not claim that the retrieval calls were shared.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from dynamic_memory.contracts import AnswerResult, ErrorInfo, QueryResponse
from dynamic_memory.pipeline.query_runner import QueryOrchestrator
from evaluation.predictions import PredictionReceipt, _check_rows, save_predictions, verify_predictions
from evaluation.question_manifest import EvaluationInputError, QuestionManifest

METHODS = ("full_text", "vector_rag", "dynamic")
ROOT = Path(__file__).resolve().parents[2]
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_GIT_SHA = re.compile(r"[0-9a-f]{40}\Z")
_UNVERIFIED = frozenset({"unknown", "unverified", "unknown/unverified"})
_STATUSES = ("identical", "mismatch", "not_comparable", "unverified")


@dataclass(frozen=True, slots=True)
class RunProtocol:
    run_id: str
    snapshot_sha256: str
    selected_splits: tuple[str, ...]
    prompt_fingerprint: str
    model_fingerprint: str
    model_settings_fingerprint: str
    retriever_fingerprint: str
    index_fingerprint: str
    time_policy: str
    base_top_k: int
    max_candidates: int
    execution_kind: str = "synthetic_fixture"
    provider_status: str = "provider_not_run"
    n: int = 1


@dataclass(frozen=True, slots=True)
class CandidateComparison:
    query_id: str
    status: str
    reason: str


@dataclass(frozen=True, slots=True)
class RunResult:
    directory: Path
    manifest_path: Path
    manifest_sha256: str
    status: str
    receipts: tuple[PredictionReceipt, ...]
    comparisons: tuple[CandidateComparison, ...]


def _git_sha() -> str:
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                                text=True, check=True, timeout=5)
        sha = result.stdout.strip()
        if _GIT_SHA.fullmatch(sha):
            return sha
    except (OSError, subprocess.SubprocessError):
        pass
    raise EvaluationInputError("GIT_ID_UNAVAILABLE")


def _target(results_root: Path | str, run_id: str) -> Path:
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id) or run_id in (".", ".."):
        raise EvaluationInputError("RUN_ID_INVALID")
    try:
        root = Path(results_root).resolve()
        allowed = (ROOT / "results").resolve()
        temporary = Path(tempfile.gettempdir()).resolve()
        if not (root.is_relative_to(allowed) or root.is_relative_to(temporary)):
            raise EvaluationInputError("RUN_PATH_INVALID")
        target = (root / run_id).resolve()
        if target.parent != root:
            raise EvaluationInputError("RUN_PATH_INVALID")
        return target
    except (OSError, TypeError, ValueError):
        raise EvaluationInputError("RUN_PATH_INVALID") from None


def _validate(manifest: QuestionManifest, protocol: RunProtocol) -> None:
    if (type(manifest) is not QuestionManifest or type(protocol) is not RunProtocol or
            not _SHA.fullmatch(manifest.snapshot_sha256) or
            protocol.snapshot_sha256 != manifest.snapshot_sha256 or
            protocol.selected_splits != manifest.selected_splits or
            not protocol.selected_splits or set(protocol.selected_splits) - {"main_dev", "extra_dev"} or
            not manifest.records or protocol.n != 1 or
            protocol.execution_kind != "synthetic_fixture" or protocol.provider_status != "provider_not_run" or
            type(protocol.base_top_k) is not int or type(protocol.max_candidates) is not int or
            protocol.base_top_k < 1 or protocol.max_candidates < protocol.base_top_k or
            any(not isinstance(value, str) or not value.strip() for value in (
                protocol.prompt_fingerprint, protocol.model_fingerprint,
                protocol.model_settings_fingerprint, protocol.retriever_fingerprint,
                protocol.index_fingerprint, protocol.time_policy))):
        raise EvaluationInputError("RUN_PROTOCOL_INVALID")
    seen = set()
    for record in manifest.records:
        if (record.split not in protocol.selected_splits or record.query_id in seen or
                not record.query_id or not record.sample_id or not record.question.strip() or
                not record.gold_record_key.startswith("sha256:") or
                not _SHA.fullmatch(record.gold_record_key[7:])):
            raise EvaluationInputError("RUN_MANIFEST_INVALID")
        seen.add(record.query_id)


def _error(record, method: str, code: str) -> QueryResponse:
    return QueryResponse(query_id=record.query_id, sample_id=record.sample_id,
        question=record.question, method=method, query_analysis=None, retrieved=None,
        resolution=None, answer_result=AnswerResult(status="error", error=ErrorInfo(
            code=code, message="Batch query execution failed.")))


def _candidate_document(response: QueryResponse) -> dict | None:
    return response.retrieved.model_dump(mode="json") if response.retrieved is not None else None


def _identity_verified(protocol: RunProtocol, a: QueryResponse, b: QueryResponse) -> bool:
    values = (protocol.prompt_fingerprint, protocol.model_fingerprint,
              protocol.model_settings_fingerprint, protocol.retriever_fingerprint,
              protocol.index_fingerprint, protocol.time_policy,
              a.retrieved.retriever_fingerprint, b.retrieved.retriever_fingerprint)
    return all(value.strip().lower() not in _UNVERIFIED for value in values)


def compare_candidates(a: QueryResponse, b: QueryResponse, protocol: RunProtocol) -> CandidateComparison:
    if (a.method, b.method) != ("vector_rag", "dynamic") or a.query_id != b.query_id or a.sample_id != b.sample_id:
        raise EvaluationInputError("COMPARISON_ID_INVALID")
    if a.retrieved is None or b.retrieved is None:
        return CandidateComparison(a.query_id, "not_comparable", "candidate_unavailable")
    if a.query_analysis != b.query_analysis:
        return CandidateComparison(a.query_id, "mismatch", "query_analysis_differs")
    if _candidate_document(a) != _candidate_document(b):
        return CandidateComparison(a.query_id, "mismatch", "final_candidate_differs")
    if ((a.retrieved.base_top_k, a.retrieved.max_candidates) != (protocol.base_top_k, protocol.max_candidates) or
            a.retrieved.retriever_fingerprint != protocol.retriever_fingerprint):
        return CandidateComparison(a.query_id, "mismatch", "run_protocol_differs")
    if not _identity_verified(protocol, a, b):
        return CandidateComparison(a.query_id, "unverified", "upstream_identity_unverified")
    return CandidateComparison(a.query_id, "identical", "observed_final_candidates_equal")


def _json_bytes(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _publish_new(path: Path, raw: bytes) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=".run-",
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    except FileExistsError:
        raise EvaluationInputError("RUN_FILE_EXISTS") from None
    except OSError:
        raise EvaluationInputError("RUN_SAVE_FAILED") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _manifest_payload(protocol: RunProtocol, git_sha: str, manifest: QuestionManifest,
                      receipts: tuple[PredictionReceipt, ...],
                      comparisons: tuple[CandidateComparison, ...],
                      rows_by_method: dict[str, tuple[QueryResponse, ...]],
                      started_at: str, finished_at: str) -> dict:
    counts = {status: sum(item.status == status for item in comparisons) for status in _STATUSES}
    status = "completed_verified" if counts["mismatch"] == counts["not_comparable"] == counts["unverified"] == 0 else "complete_not_comparable"
    return {
        "format_version": 1, "status": status, "git_sha": git_sha,
        "started_at": started_at, "finished_at": finished_at,
        "run_id": protocol.run_id, "snapshot_sha256": protocol.snapshot_sha256,
        "selected_splits": list(protocol.selected_splits), "methods": list(METHODS),
        "n": protocol.n, "execution_kind": protocol.execution_kind,
        "provider_status": protocol.provider_status, "identity_basis": "declared_synthetic_ports",
        "identities": {key: getattr(protocol, key) for key in (
            "prompt_fingerprint", "model_fingerprint", "model_settings_fingerprint",
            "retriever_fingerprint", "index_fingerprint", "time_policy",
            "base_top_k", "max_candidates")},
        "query_ids": [item.query_id for item in manifest.records],
        "predictions": [{"method": method, "file": f"{method}.predictions.json",
                         "sha256": receipt.file_sha256, "query_ids": list(receipt.query_ids),
                         "count": len(rows_by_method[method]),
                         "error_count": sum(row.answer_result.status == "error" for row in rows_by_method[method]),
                         "answer_status_counts": {status: sum(row.answer_result.status == status for row in rows_by_method[method])
                                                  for status in ("answered", "no_evidence", "time_uncertain", "unresolved_conflict", "error")},
                         "error_code_counts": {code: sum(row.answer_result.error is not None and
                                                          row.answer_result.error.code == code for row in rows_by_method[method])
                                               for code in sorted({row.answer_result.error.code for row in rows_by_method[method]
                                                                   if row.answer_result.error is not None})}}
                        for method, receipt in zip(METHODS, receipts)],
        "candidate_comparability": [{"query_id": row.query_id, "status": row.status,
                                      "reason": row.reason} for row in comparisons],
        "comparison_counts": counts,
    }


def run_three_method_batch(manifest: QuestionManifest, orchestrator: QueryOrchestrator,
                           protocol: RunProtocol, *, results_root: Path | str = ROOT / "results") -> RunResult:
    """Run once per method/question, save all three, then publish completion."""
    _validate(manifest, protocol)
    if type(orchestrator) is not QueryOrchestrator:
        raise EvaluationInputError("RUN_ORCHESTRATOR_INVALID")
    target = _target(results_root, protocol.run_id)
    git_sha = _git_sha()
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        target.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise EvaluationInputError("RUN_ID_EXISTS") from None
    except OSError:
        raise EvaluationInputError("RUN_PATH_INVALID") from None
    rows: dict[str, list[QueryResponse]] = {method: [] for method in METHODS}
    for record in manifest.records:
        request = record.to_query_request()
        try:
            prepared = orchestrator.prepare_query(request)
        except Exception:
            prepared = None
        for method in METHODS:
            if prepared is None:
                response = _error(record, method, "BATCH_PREPARE_FAILED")
            else:
                try:
                    response = orchestrator.run_prepared(prepared, method)
                except Exception:
                    response = _error(record, method, "BATCH_METHOD_FAILED")
            rows[method].append(response)
    checked = {method: _check_rows(manifest, rows[method], method) for method in METHODS}
    comparisons = tuple(compare_candidates(a, b, protocol) for a, b in zip(
        checked["vector_rag"], checked["dynamic"]))
    receipts = []
    for method in METHODS:
        path = target / f"{method}.predictions.json"
        receipt = save_predictions(path, manifest, checked[method], method)
        verify_predictions(path, manifest, receipt)
        receipts.append(receipt)
    receipts_tuple = tuple(receipts)
    finished_at = datetime.now(timezone.utc).isoformat()
    payload = _manifest_payload(protocol, git_sha, manifest, receipts_tuple, comparisons, checked,
                                started_at, finished_at)
    raw = _json_bytes(payload)
    digest = hashlib.sha256(raw).hexdigest()
    _publish_new(target / "run_manifest.sha256", (digest + "\n").encode("ascii"))
    manifest_path = target / "run_manifest.json"
    _publish_new(manifest_path, raw)
    return RunResult(target, manifest_path, digest, payload["status"], receipts_tuple, comparisons)


def verify_run(directory: Path | str, manifest: QuestionManifest, *,
               expected_manifest_sha256: str | None = None) -> RunResult:
    """Independently reopen metadata and all prediction bytes; never open Gold."""
    if type(manifest) is not QuestionManifest:
        raise EvaluationInputError("RUN_MANIFEST_INVALID")
    try:
        path = Path(directory).resolve()
        if _target(path.parent, path.name) != path:
            raise EvaluationInputError("RUN_PATH_INVALID")
        raw = (path / "run_manifest.json").read_bytes()
        recorded_sha = (path / "run_manifest.sha256").read_text(encoding="ascii")
    except EvaluationInputError:
        raise
    except (OSError, UnicodeError):
        raise EvaluationInputError("RUN_INCOMPLETE") from None
    digest = hashlib.sha256(raw).hexdigest()
    if recorded_sha != digest + "\n":
        raise EvaluationInputError("RUN_MANIFEST_HASH_MISMATCH")
    if expected_manifest_sha256 is not None and digest != expected_manifest_sha256:
        raise EvaluationInputError("RUN_MANIFEST_HASH_MISMATCH")
    try:
        data = json.loads(raw.decode("utf-8"))
        if (_json_bytes(data) != raw or type(data) is not dict or
                type(data.get("format_version")) is not int or data["format_version"] != 1):
            raise ValueError
        protocol = RunProtocol(run_id=data["run_id"], snapshot_sha256=data["snapshot_sha256"],
            selected_splits=tuple(data["selected_splits"]), **data["identities"],
            execution_kind=data["execution_kind"], provider_status=data["provider_status"], n=data["n"])
        _validate(manifest, protocol)
        if (data["git_sha"] is None or not _GIT_SHA.fullmatch(data["git_sha"]) or
                data["methods"] != list(METHODS) or data["query_ids"] != [r.query_id for r in manifest.records] or
                len(data["predictions"]) != 3):
            raise ValueError
        started = datetime.fromisoformat(data["started_at"])
        finished = datetime.fromisoformat(data["finished_at"])
        if (started.tzinfo is None or finished.tzinfo is None or
                started.utcoffset().total_seconds() != 0 or finished.utcoffset().total_seconds() != 0 or
                finished < started):
            raise ValueError
        receipts = []
        rows = {}
        for method, item in zip(METHODS, data["predictions"]):
            if item["method"] != method or item["file"] != f"{method}.predictions.json" or not _SHA.fullmatch(item["sha256"]):
                raise ValueError
            prediction_path = path / item["file"]
            receipt = PredictionReceipt(prediction_path, item["sha256"], manifest.snapshot_sha256,
                                        method, tuple(item["query_ids"]))
            checked = verify_predictions(prediction_path, manifest, receipt)
            if (item["count"] != len(checked) or
                    item["error_count"] != sum(r.answer_result.status == "error" for r in checked)):
                raise ValueError
            rows[method] = checked
            receipts.append(receipt)
        comparisons = tuple(compare_candidates(a, b, protocol) for a, b in zip(
            rows["vector_rag"], rows["dynamic"]))
        expected = _manifest_payload(protocol, data["git_sha"], manifest, tuple(receipts), comparisons, rows,
                                     data["started_at"], data["finished_at"])
        if data != expected:
            raise ValueError
    except EvaluationInputError:
        raise
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        raise EvaluationInputError("RUN_MANIFEST_INVALID") from None
    return RunResult(path, path / "run_manifest.json", digest, data["status"], tuple(receipts), comparisons)
