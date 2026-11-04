"""Audit replayability of private semantic-review checkpoints without network."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from dynamic_memory.memory.review_workflow import ReviewQueue
from dynamic_memory.memory.semantic_review import _window_decision


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", required=True, type=Path)
    parser.add_argument("--review-dir", required=True, type=Path)
    args = parser.parse_args()
    queue = ReviewQueue.model_validate_json(args.queue.read_bytes())
    windows = {window.window_id: window for window in queue.windows}
    checkpoint_root = args.review_dir.resolve() / "checkpoints"
    attempts = sorted(checkpoint_root.glob("*.attempt.json"))
    valid = []
    invalid = []
    for path in sorted(checkpoint_root.glob("window_*.json")):
        if path.name.endswith(".attempt.json"):
            continue
        window_id = path.name[:-5]
        row = json.loads(path.read_text(encoding="utf-8"))
        try:
            _window_decision(windows[window_id], row["response"], queue=queue)
            valid.append(window_id)
        except Exception as exc:
            invalid.append({
                "window_id": window_id,
                "error_type": type(exc).__name__,
                "response_chars": len(row.get("response", "")),
                "finish_reason": row.get("finish_reason"),
            })
    print(json.dumps({
        "queue_fingerprint": queue.fingerprint,
        "attempts": len(attempts),
        "valid_checkpoints": len(valid),
        "invalid_checkpoints": invalid,
    }, sort_keys=True))
    return 0 if not invalid else 2


if __name__ == "__main__":
    raise SystemExit(main())
