"""Run deterministic extraction, review, publication, and indexing for one sample."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from dynamic_memory.memory.auto_review import write_auto_review_files
from dynamic_memory.memory.review_workflow import ReviewQueue
from scripts.stage6_extract_sample import (
    ALLOWED_SAMPLES,
    _execute_auto,
    _plan_auto,
)
from scripts.stage6_publish_sample import _publish, _safe_new_summary_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", required=True, choices=ALLOWED_SAMPLES)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--review-dir", required=True, type=Path)
    parser.add_argument("--artifact-root", required=True, type=Path)
    parser.add_argument("--index-root", type=Path)
    parser.add_argument("--model-root", type=Path)
    parser.add_argument("--model-revision")
    parser.add_argument("--summary-dir", required=True, type=Path)
    args = parser.parse_args()
    if args.index_root is not None and (
        args.model_root is None or not args.model_revision
    ):
        raise SystemExit("MODEL_IDENTITY_REQUIRED")
    state_dir = args.state_dir.resolve()
    review_dir = args.review_dir.resolve()
    if state_dir.exists() and any(state_dir.iterdir()):
        raise SystemExit(f"state directory not empty: {state_dir}")
    if review_dir.exists() and any(review_dir.iterdir()):
        raise SystemExit(f"review directory not empty: {review_dir}")
    summary_dir = _safe_new_summary_dir(args.summary_dir)

    plan = _plan_auto(args.sample)
    extraction = _execute_auto(plan, state_dir)
    queue = ReviewQueue.model_validate_json(
        (state_dir / "private_review/queue.json").read_bytes()
    )
    approval_path, authority_path = write_auto_review_files(
        queue,
        review_dir,
        run_kind="real",
    )
    publication = _publish(
        sample_id=args.sample,
        state_dir=state_dir,
        review_dir=review_dir,
        artifact_root=args.artifact_root,
        index_root=args.index_root,
        model_root=args.model_root,
        model_revision=args.model_revision,
        allowed_splits=(plan["split"],),
    )
    output = {
        "status": "AUTO_MEMORY_BUILD_COMPLETE",
        "sample_id": args.sample,
        "network_calls": 0,
        "extraction": extraction,
        "review": {
            "approval_path": str(approval_path),
            "authority_path": str(authority_path),
            "approved_facts": publication["approved_facts"],
            "excluded_facts": publication["excluded_facts"],
        },
        "publication": publication,
        "summary_dir": str(summary_dir),
    }
    summary_dir.mkdir(parents=True, exist_ok=True)
    (summary_dir / "auto_build_summary.json").write_text(
        json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
