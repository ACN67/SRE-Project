"""Combine valid semantic checkpoints and prepare one-window retry material."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from dynamic_memory.memory.review_workflow import ReviewQueue
from dynamic_memory.memory.semantic_review import (
    SEMANTIC_API_MODEL, SEMANTIC_REVIEWER_ID, _SYSTEM, _encoded, _prompt,
    _sha as semantic_sha, _window_decision,
)
from scripts.stage6_extract_sample import (
    PEAK_INPUT_PER_MILLION, PEAK_OUTPUT_PER_MILLION, PROVIDER_OUTPUT_RESERVE,
    _code_fingerprint, _git_sha, _read_json, _write_json,
)
from dynamic_memory.memory.review_workflow import _fp


class CombineError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", required=True, type=Path)
    parser.add_argument("--review-a", required=True, type=Path)
    parser.add_argument("--review-b", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    results_root = (Path(__file__).resolve().parents[1] / "results").resolve()
    if not output.is_relative_to(results_root):
        raise CombineError("OUTPUT_DIRECTORY_UNSAFE")
    if output.exists() and any(output.iterdir()):
        raise CombineError("OUTPUT_DIRECTORY_IN_USE")
    output.mkdir(parents=True, exist_ok=True)
    checkpoints = output / "checkpoints"
    checkpoints.mkdir()
    queue = ReviewQueue.model_validate_json(args.queue.read_bytes())
    by_id = {window.window_id: window for window in queue.windows}
    accepted = {}
    invalid = []
    duplicate = []
    for review_dir in (args.review_a.resolve(), args.review_b.resolve()):
        for path in sorted((review_dir / "checkpoints").glob("window_*.json")):
            if path.name.endswith(".attempt.json"):
                continue
            window_id = path.name[:-5]
            row = json.loads(path.read_text(encoding="utf-8"))
            try:
                _window_decision(by_id[window_id], row["response"], queue=queue)
            except Exception:
                invalid.append(window_id)
                continue
            if window_id in accepted:
                duplicate.append(window_id)
                continue
            accepted[window_id] = path
    if duplicate:
        raise CombineError("DUPLICATE_CHECKPOINT")
    if len(accepted) != 82 or len(invalid) != 1:
        raise CombineError("COMBINED_COUNT_MISMATCH")
    for window in queue.windows:
        source = accepted.get(window.window_id)
        if source is not None:
            shutil.copyfile(source, checkpoints / source.name)
    retry_id = invalid[0]
    retry_window = by_id[retry_id]
    retry_queue = ReviewQueue(
        run_fingerprint=queue.run_fingerprint,
        sample_id=queue.sample_id,
        windows=(retry_window,),
    )
    retry_queue_path = output / "retry_queue_1.json"
    with retry_queue_path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(_canonical(retry_queue.model_dump(mode="json")) + "\n")
    retry_input_bytes = len(_SYSTEM.encode("utf-8")) + len(_prompt(retry_window).encode("utf-8")) + 32
    worst = (
        Decimal(retry_input_bytes) * PEAK_INPUT_PER_MILLION
        + Decimal(PROVIDER_OUTPUT_RESERVE) * PEAK_OUTPUT_PER_MILLION
    ) / Decimal(1_000_000)
    body = {
        "source_queue_fingerprint": queue.fingerprint,
        "combined_checkpoint_fingerprints": {
            window_id: "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
            for window_id, path in sorted(accepted.items())
        },
        "retry_window_id": retry_id,
        "retry_request_fingerprint": semantic_sha(
            _encoded({"system": _SYSTEM, "user": _prompt(retry_window)})
        ),
        "model_id": SEMANTIC_API_MODEL,
        "reviewer_id": SEMANTIC_REVIEWER_ID,
        "max_http_attempts": 1,
        "provider_default_output_tokens": PROVIDER_OUTPUT_RESERVE,
        "git_sha": _git_sha(),
        "code_fingerprint": _code_fingerprint(),
    }
    plan = {
        "format_version": 1,
        "status": "STAGE6_SEMANTIC_RETRY_PLAN_READY",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "run_fingerprint": _fp(body),
        "source_queue_path": str(args.queue.resolve()),
        "combined_dir": str(output),
        "combined_valid_windows": len(accepted),
        "retry_window_id": retry_id,
        "retry_queue_path": str(retry_queue_path),
        "retry_queue_fingerprint": retry_queue.fingerprint,
        "retry_request_fingerprint": body["retry_request_fingerprint"],
        "provider": {
            "model_id": SEMANTIC_API_MODEL,
            "thinking": "enabled",
            "reasoning_effort": "high",
            "request_max_tokens_omitted": True,
            "provider_default_output_tokens": PROVIDER_OUTPUT_RESERVE,
            "retries": 0,
            "max_concurrency": 1,
        },
        "budget": {
            "input_bytes_upper": retry_input_bytes,
            "max_http_attempts": 1,
            "peak_input_usd_per_million": str(PEAK_INPUT_PER_MILLION),
            "peak_output_usd_per_million": str(PEAK_OUTPUT_PER_MILLION),
            "worst_case_usd": format(worst, ".7f"),
        },
    }
    _write_json(output / "retry_plan.json", plan)
    print(json.dumps({
        "status": plan["status"],
        "combined_valid_windows": len(accepted),
        "retry_window_id": retry_id,
        "run_fingerprint": plan["run_fingerprint"],
        "retry_queue_fingerprint": retry_queue.fingerprint,
        **plan["budget"],
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
