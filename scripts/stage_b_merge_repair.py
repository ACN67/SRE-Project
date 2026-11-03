"""Zero-network, read-only lineage merge of the old 59 and four real repairs."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from dynamic_memory.memory.batch_budget import BatchApproval, BatchBudgetLedger, BudgetError
from dynamic_memory.memory.checkpoints import WindowCheckpointStore
from dynamic_memory.memory.repair_lineage import (
    FAILED_WINDOW_IDS, LineageError, merge_result_sets, verify_original_batch,
)
from dynamic_memory.memory.review_workflow import ReviewQueue
from scripts.stage_b_repair_runner import (
    _budget_root, _ensure_private_state, _registry_root, prepare_plan,
)


def _write_or_compare(path: Path, value: object) -> None:
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise LineageError("MERGED_OUTPUT_CONFLICT")
    else:
        with path.open("xb") as stream:
            stream.write(payload)
            stream.flush()


def merge_real(state_dir: Path, approval_file: Path) -> dict:
    root = _ensure_private_state(state_dir)
    if approval_file.resolve().is_relative_to(root):
        raise LineageError("APPROVAL_NOT_INDEPENDENT")
    plan = prepare_plan(mode="repair4")
    old_results, old_rows, baseline = verify_original_batch(plan)
    raw = approval_file.read_bytes()
    approval = BatchApproval.model_validate_json(raw)
    if (approval.mode != "repair4" or approval.sample_id != plan.sample_id or
        approval.dataset_sha256 != plan.bundle.snapshot_sha256 or
        approval.plan_fingerprint != plan.plan_fingerprint or
        approval.run_fingerprint != plan.run_fingerprint or
        approval.selected_window_ids != tuple(w.extraction_window_id for w in plan.selected) or
        approval.max_new_attempts != 4):
        raise LineageError("APPROVAL_LINEAGE_MISMATCH")
    ledger = BatchBudgetLedger(_budget_root(plan, root, approval), approval,
        "sha256:" + hashlib.sha256(raw).hexdigest(), _registry_root(plan, root))
    with ledger.run_lock():
        rows = ledger.window_rows()
        if (len(rows) != 4 or {r["window_id"] for r in rows} != FAILED_WINDOW_IDS or
            any(r["state"] != "extracted" for r in rows)):
            raise LineageError("REPAIR_INCOMPLETE")
        new_rows = {r["window_id"]: r for r in rows}
        new_store = WindowCheckpointStore(root / "checkpoints",
            run_fingerprint=plan.run_fingerprint)
        repaired = {}
        for w in plan.selected:
            wid = w.extraction_window_id
            if new_rows[wid]["call_fingerprint"] != plan.call_fingerprints[wid]:
                raise LineageError("REPAIR_CALL_MISMATCH")
            result = new_store.load(w, plan.call_fingerprints[wid])
            if result is None:
                raise LineageError("REPAIR_INCOMPLETE")
            repaired[wid] = result
        manifest, queue = merge_result_sets(plan, old_results, repaired, old_rows,
            new_rows, baseline, approval.authorization_id,
            hashlib.sha256(raw).hexdigest(), root / "checkpoints")
        _write_or_compare(root / "merged" / "manifest.json", manifest)
        _write_or_compare(root / "merged" / "queue.json", queue.model_dump(mode="json"))
        # Two independent disk reads, each compared with a fresh in-memory build.
        for _ in range(2):
            check_manifest, check_queue = merge_result_sets(plan, old_results, repaired,
                old_rows, new_rows, baseline, approval.authorization_id,
                hashlib.sha256(raw).hexdigest(), root / "checkpoints")
            if (json.loads((root / "merged" / "manifest.json").read_bytes()) != check_manifest or
                ReviewQueue.model_validate_json((root / "merged" / "queue.json").read_bytes()) != check_queue):
                raise LineageError("MERGED_READBACK_FAILED")
        return {"status": "READY_FOR_HUMAN_REVIEW", "network_calls": 0,
            "valid_windows": 63, "original_windows": 59, "repaired_windows": 4,
            "manifest_fingerprint": manifest["digest"],
            "queue_fingerprint": queue.fingerprint,
            "manifest_path": str(root / "merged" / "manifest.json"),
            "queue_path": str(root / "merged" / "queue.json"), "published": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--approval-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(merge_real(args.state_dir, args.approval_file)))
        return 0
    except (LineageError, BudgetError) as exc:
        print(json.dumps({"status": exc.code, "network_calls": 0}))
        return 2
    except Exception:
        print(json.dumps({"status": "MERGED_UNEXPECTED_SANITIZED", "network_calls": 0}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
