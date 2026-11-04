"""Resume a private DeepSeek V4.1 Flash semantic review; no extraction calls.

Uses high thinking and deliberately omits max_tokens. Each attempted request is
journaled before sending. A failed or uncertain attempt stops without retry.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from dynamic_memory.memory.review_workflow import ReviewQueue
from dynamic_memory.memory.semantic_review import run_semantic_review


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--max-new-calls", type=int, default=0)
    parser.add_argument("--run-kind", choices=["real", "offline_test"], default="real")
    args = parser.parse_args()
    try:
        queue = ReviewQueue.model_validate_json(args.queue.read_bytes())
        if (args.output_dir.resolve().is_relative_to(args.queue.parent.parent.resolve()) or
                args.output_dir.resolve().is_relative_to(Path(__file__).resolve().parents[1])):
            raise ValueError("review output must be outside extraction state")
        result = run_semantic_review(queue, args.output_dir,
            max_new_calls=args.max_new_calls,
            api_key=os.environ.get("DEEPSEEK_API_KEY"), run_kind=args.run_kind)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "SEMANTIC_REVIEW_STOPPED",
            "error_type": type(exc).__name__}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
