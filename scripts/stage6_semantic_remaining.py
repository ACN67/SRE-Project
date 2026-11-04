"""Create a queue containing only never-attempted semantic review windows."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from dynamic_memory.memory.review_workflow import ReviewQueue, export_review_queue
from dynamic_memory.memory.semantic_review import run_semantic_review
from scripts.stage6_extract_sample import _read_api_key


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", required=True, type=Path)
    parser.add_argument("--review-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    queue = ReviewQueue.model_validate_json(args.queue.read_bytes())
    checkpoint_root = args.review_dir.resolve() / "checkpoints"
    attempted = {
        path.name.removesuffix(".attempt.json")
        for path in checkpoint_root.glob("*.attempt.json")
    }
    remaining = tuple(
        window for window in queue.windows
        if window.window_id not in attempted
    )
    output_dir = args.output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise SystemExit("OUTPUT_DIRECTORY_IN_USE")
    output_dir.mkdir(parents=True, exist_ok=True)
    subset = ReviewQueue(
        run_fingerprint=queue.run_fingerprint,
        sample_id=queue.sample_id,
        windows=remaining,
    )
    queue_path = output_dir / "remaining_queue.json"
    export_review_queue(subset, queue_path)
    result = run_semantic_review(
        subset,
        output_dir,
        max_new_calls=len(remaining),
        api_key=_read_api_key(),
        run_kind="real",
    )
    print(json.dumps({
        "status": result["status"],
        "original_queue_fingerprint": queue.fingerprint,
        "remaining_queue_fingerprint": subset.fingerprint,
        "remaining_windows": len(remaining),
        **result,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
