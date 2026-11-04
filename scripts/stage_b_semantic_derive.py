"""Derive guarded v3 semantic decisions from recorded v2 responses, offline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from dynamic_memory.memory.review_workflow import ReviewQueue
from dynamic_memory.memory.semantic_review import derive_guarded_review


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--source-approval", type=Path, required=True)
    parser.add_argument("--source-authority", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        queue = ReviewQueue.model_validate_json(args.queue.read_bytes())
        if (args.output_dir.resolve().is_relative_to(args.queue.parent.parent.resolve()) or
                args.output_dir.resolve().is_relative_to(Path(__file__).resolve().parents[1])):
            raise ValueError("unsafe output directory")
        result = derive_guarded_review(queue, args.source_approval,
            args.source_authority, args.output_dir)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "GUARDED_SEMANTIC_DERIVATION_STOPPED",
                          "error_type": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
