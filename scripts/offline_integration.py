"""Explicit offline synthetic prepare/check CLI; never calls a real API."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from dynamic_memory.contracts import QueryRequest
from dynamic_memory.integration.offline import (
    create_system, prepare_artifacts, prepare_indexes, prepare_source, state_root,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "check", "evaluate"))
    parser.add_argument("--state", required=True, help="Absolute directory outside the repository")
    parser.add_argument("--results", help="For evaluate: fresh absolute directory under the OS temp directory")
    args = parser.parse_args()
    root = state_root(args.state)
    if args.action == "prepare":
        prepare_source(root)
        builds = prepare_artifacts(root)
        indexes = prepare_indexes(root)
        print(json.dumps({"profile": "offline_synthetic", "builds": builds,
                          "indexes": indexes}, sort_keys=True))
        return 0
    service, orchestrator = create_system(root)
    if args.action == "evaluate":
        from evaluation.question_manifest import load_question_manifest
        from evaluation.runners.non_d_offline import run_non_d_offline
        if not args.results:
            parser.error("evaluate requires --results")
        manifest = load_question_manifest(root / "synthetic_locomo.json", root / "split.yaml",
                                          selected_splits=("main_dev",))
        receipts = run_non_d_offline(manifest, service, orchestrator,
                                     Path(args.results))
        print(json.dumps({k: v.file_sha256 for k, v in receipts.items()}, sort_keys=True))
        return 0
    for sample in ("synthetic-0", "synthetic-1"):
        status = service.get_build_status(sample)
        if status.memory_state != "complete":
            raise RuntimeError("offline build is not complete")
        for method in ("full_text", "vector_rag"):
            result = service.run_query(QueryRequest(query_id=f"check_{sample}_{method}",
                sample_id=sample, question="What does Ada keep?"), method)
            if result.answer_result.status != "answered":
                raise RuntimeError(f"{sample} {method} failed: {result.answer_result.status}")
        print(f"{sample}: complete; full_text=answered; vector_rag=answered; synthetic")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
