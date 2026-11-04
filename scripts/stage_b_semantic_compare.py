"""Compare recorded semantic decisions with a prior review without using Gold."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from dynamic_memory.memory.review_workflow import ReviewApproval, ReviewQueue
from dynamic_memory.memory.semantic_review import _SYSTEM, _encoded, _prompt, _sha, _window_decision


def compare(queue: ReviewQueue, reference: ReviewApproval, checkpoint_dir: Path,
            *, guarded: bool = False) -> dict:
    if (reference.queue_fingerprint != queue.fingerprint or
            reference.run_fingerprint != queue.run_fingerprint or
            len(reference.windows) != len(queue.windows)):
        raise ValueError("reference review does not match queue")
    matrix = Counter()
    usage = Counter()
    mismatches = []
    completed = 0
    for window, reference_window in zip(queue.windows, reference.windows):
        path = checkpoint_dir / (window.window_id + ".json")
        if not path.exists():
            break
        row = json.loads(path.read_bytes())
        if (row.get("window_id") != window.window_id or row.get("model") != "deepseek-flash"
                or row.get("finish_reason") != "stop" or
                row.get("request_fingerprint") != _sha(_encoded({
                    "system": _SYSTEM, "user": _prompt(window)}))):
            raise ValueError("semantic checkpoint identity")
        decision_window = _window_decision(window, row["response"], queue=queue if guarded else None)
        if (reference_window.window_id != window.window_id or
                len(reference_window.facts) != len(window.facts)):
            raise ValueError("reference window identity")
        completed += 1
        for key in ("prompt_tokens", "completion_tokens", "prompt_cache_hit_tokens",
                    "prompt_cache_miss_tokens"):
            usage[key] += (row.get("usage") or {}).get(key) or 0
        for index, (fact, expected, actual) in enumerate(zip(
                window.facts, reference_window.facts, decision_window.facts)):
            if expected.draft_fingerprint != fact.draft_fingerprint:
                raise ValueError("reference fact identity")
            matrix[(expected.decision, actual.decision)] += 1
            if expected.decision != actual.decision:
                mismatches.append({"window_id": window.window_id, "fact_index": index,
                    "draft_fingerprint": fact.draft_fingerprint,
                    "reference_decision": expected.decision,
                    "semantic_decision": actual.decision,
                    "reference_reason": expected.reason_code,
                    "semantic_reason": actual.reason_code})
    return {"windows_complete": completed, "windows_total": len(queue.windows),
        "facts_compared": sum(matrix.values()),
        "decision_matrix": {f"{a}_to_{b}": n for (a, b), n in sorted(matrix.items())},
        "token_usage": dict(usage), "mismatches": mismatches,
        "reference_is_independent_gold": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", required=True, type=Path)
    parser.add_argument("--reference-approval", required=True, type=Path)
    parser.add_argument("--semantic-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--guarded", action="store_true")
    args = parser.parse_args()
    queue = ReviewQueue.model_validate_json(args.queue.read_bytes())
    reference = ReviewApproval.model_validate_json(args.reference_approval.read_bytes())
    report = compare(queue, reference, args.semantic_dir / "checkpoints",
                     guarded=args.guarded)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps({key: report[key] for key in (
        "windows_complete", "windows_total", "facts_compared", "decision_matrix",
        "token_usage")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
