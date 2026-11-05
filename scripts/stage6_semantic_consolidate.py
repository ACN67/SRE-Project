"""Consolidate semantic checkpoints from interrupted partial review batches.

The full frozen queue is authoritative. Only checkpoints that replay against
that queue are copied. The output directory also receives a queue containing
the windows that still need one new semantic call.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

from dynamic_memory.memory.review_workflow import ReviewQueue, _fp
from dynamic_memory.memory.semantic_review import _window_decision


class ConsolidationError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _write_json(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(_canonical(value) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", required=True, type=Path)
    parser.add_argument("--review-dir", required=True, action="append", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        queue = ReviewQueue.model_validate_json(args.queue.read_bytes())
        windows = {window.window_id: window for window in queue.windows}
        output = args.output_dir.resolve()
        allowed = (Path(__file__).resolve().parents[1] / "results").resolve()
        if not output.is_relative_to(allowed):
            raise ConsolidationError("OUTPUT_DIRECTORY_UNSAFE")
        if output.exists() and any(output.iterdir()):
            raise ConsolidationError("OUTPUT_DIRECTORY_IN_USE")
        checkpoints = output / "checkpoints"
        checkpoints.mkdir(parents=True)

        accepted: dict[str, Path] = {}
        invalid: list[dict] = []
        duplicates = 0
        for review_dir in sorted(args.review_dir, key=lambda path: str(path)):
            root = review_dir.resolve() / "checkpoints"
            if not root.is_dir():
                continue
            for path in sorted(root.glob("window_*.json")):
                if path.name.endswith(".attempt.json"):
                    continue
                window_id = path.stem
                if window_id not in windows:
                    invalid.append({
                        "window_id": window_id,
                        "source": str(path),
                        "reason": "UNKNOWN_WINDOW",
                    })
                    continue
                try:
                    row = json.loads(path.read_text(encoding="utf-8"))
                    _window_decision(
                        windows[window_id],
                        row["response"],
                        queue=queue,
                    )
                except Exception as exc:
                    invalid.append({
                        "window_id": window_id,
                        "source": str(path),
                        "reason": type(exc).__name__,
                    })
                    continue
                if window_id in accepted:
                    duplicates += 1
                    continue
                accepted[window_id] = path

        for window in queue.windows:
            source = accepted.get(window.window_id)
            if source is not None:
                shutil.copyfile(
                    source,
                    checkpoints / f"{window.window_id}.json",
                )

        missing = tuple(
            window for window in queue.windows
            if window.window_id not in accepted
        )
        missing_queue = None
        if missing:
            missing_queue = ReviewQueue(
                run_fingerprint=queue.run_fingerprint,
                sample_id=queue.sample_id,
                windows=missing,
            )
            _write_json(
                output / "missing_queue.json",
                missing_queue.model_dump(mode="json"),
            )
        audit = {
            "format_version": 1,
            "status": (
                "STAGE6_SEMANTIC_CONSOLIDATION_READY"
                if not missing
                else "STAGE6_SEMANTIC_MISSING_WINDOWS"
            ),
            "sample_id": queue.sample_id,
            "source_queue_fingerprint": queue.fingerprint,
            "accepted_windows": len(accepted),
            "missing_windows": len(missing),
            "duplicate_valid_checkpoints": duplicates,
            "invalid_checkpoints": invalid,
            "missing_queue_fingerprint": (
                missing_queue.fingerprint if missing_queue is not None else None
            ),
            "checkpoint_fingerprints": {
                window_id: "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
                for window_id, path in sorted(accepted.items())
            },
        }
        _write_json(output / "consolidation_audit.json", audit)
        print(json.dumps(audit, ensure_ascii=False))
        return 0
    except Exception as exc:
        code = getattr(exc, "code", None)
        print(json.dumps({
            "status": code if isinstance(code, str)
            else "STAGE6_SEMANTIC_CONSOLIDATION_FAILED",
            "error_type": type(exc).__name__,
        }))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
