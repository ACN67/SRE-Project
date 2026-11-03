"""Diagnostic Full Text / Vector RAG run; never a formal three-method score."""

from __future__ import annotations

import json
from pathlib import Path

from dynamic_memory.contracts import QueryResponse
from dynamic_memory.pipeline.query_runner import QueryOrchestrator
from dynamic_memory.services.dynamic_memory_service import DynamicMemoryService
from evaluation.predictions import _target, save_predictions, verify_predictions
from evaluation.question_manifest import QuestionManifest


METHODS = ("full_text", "vector_rag", "pure_vector_topk")
ANSWER_STATUSES = ("answered", "no_evidence", "time_uncertain", "unresolved_conflict", "error")


def run_non_d_offline(manifest: QuestionManifest, service: DynamicMemoryService,
                      orchestrator: QueryOrchestrator, directory: Path):
    """Persist and verify synthetic predictions before any Gold may be opened.

    The raw Top-K path uses A's existing C raw-retrieval port. It is kept
    separate from the expanded Vector RAG method and the D-dependent runner.
    """
    if type(manifest) is not QuestionManifest or not isinstance(service, DynamicMemoryService):
        raise ValueError("INVALID_OFFLINE_RUN")
    if type(orchestrator) is not QueryOrchestrator or not manifest.records:
        raise ValueError("INVALID_OFFLINE_RUN")
    directory = Path(directory).resolve()
    if directory.exists() and any(directory.iterdir()):
        raise ValueError("OFFLINE_RUN_EXISTS")
    for method in METHODS:
        _target(directory / f"{method}.predictions.json")
    directory.mkdir(parents=True, exist_ok=True)
    receipts = {}
    status_counts = {}
    error_code_counts = {}
    checked_by_method = {}
    for method in METHODS:
        responses: list[QueryResponse] = []
        for record in manifest.records:
            request = record.to_query_request()
            response = service.run_query(request, method)
            responses.append(response)
        path = directory / f"{method}.predictions.json"
        receipt = save_predictions(path, manifest, responses, method)
        checked = verify_predictions(path, manifest, receipt)
        checked_by_method[method] = checked
        receipts[method] = receipt
        # Count the independently reopened, SHA-verified predictions, not
        # unchecked in-memory responses. All statuses, including zeroes,
        # remain explicit so a saved file is not mistaken for answered QA.
        status_counts[method] = {
            status: sum(row.answer_result.status == status for row in checked)
            for status in ANSWER_STATUSES
        }
        codes = sorted({
            row.answer_result.error.code for row in checked
            if row.answer_result.error is not None
        })
        error_code_counts[method] = {
            code: sum(row.answer_result.error is not None and
                      row.answer_result.error.code == code for row in checked)
            for code in codes
        }
    comparisons = []
    for index, record in enumerate(manifest.records):
        vector = checked_by_method["vector_rag"][index]
        pure = checked_by_method["pure_vector_topk"][index]
        vector_items = vector.retrieved.items if vector.retrieved is not None else []
        pure_items = pure.retrieved.items if pure.retrieved is not None else []
        final_ids = [item.memory_id for item in vector_items]
        comparisons.append({
            "query_id": record.query_id, "sample_id": record.sample_id,
            "initial_topk_ids": [item.memory_id for item in pure_items],
            "expanded_ids": [item.memory_id for item in vector_items
                             if item.origin == "history_expansion"],
            "final_candidate_ids": final_ids,
            "candidate_origins": [item.origin for item in vector_items],
            "vector_used_memory_ids": vector.answer_result.used_memory_ids,
            "pure_used_memory_ids": pure.answer_result.used_memory_ids,
            "vector_context_excluded_ids": [mid for mid in final_ids
                                            if mid not in vector.answer_result.used_memory_ids],
            "vector_status": vector.answer_result.status,
            "pure_status": pure.answer_result.status,
        })
    (directory / "candidate_comparison.json").write_text(
        json.dumps(comparisons, sort_keys=True), encoding="utf-8")
    summary = {"execution_kind": "offline_synthetic_non_d", "snapshot_sha256": manifest.snapshot_sha256,
               "methods": list(METHODS), "question_count": len(manifest.records),
               "prediction_sha256": {method: receipt.file_sha256
               for method, receipt in receipts.items()},
               "answer_status_counts": status_counts, "error_code_counts": error_code_counts}
    (directory / "non_d_summary.json").write_text(json.dumps(summary, sort_keys=True), encoding="utf-8")
    return receipts
