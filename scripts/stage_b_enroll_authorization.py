"""Explicit, zero-network registration of an independently user-approved paid batch.

NOT called automatically by the extraction runner. Use ONLY after the user
separately authorizes the exact approval file and authorization ID.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from dynamic_memory.memory.batch_budget import (
    BatchBudgetLedger, BudgetError, load_approval,
)
from scripts.stage_b_repair_runner import (
    OLD_PILOT_WINDOW, _budget_root, _ensure_private_state,
    _registry_root, prepare_plan,
)
from dynamic_memory.memory.repair_lineage import LineageError, verify_original_batch


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("pilot", "batch", "repair4"), required=True)
    parser.add_argument("--approval-file", type=Path, required=True)
    parser.add_argument("--authorization-id", required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--confirm-separate-user-approval", action="store_true")
    args = parser.parse_args()

    if not args.confirm_separate_user_approval:
        print(json.dumps({"status": "SEPARATE_USER_APPROVAL_REQUIRED",
                          "network_calls": 0}))
        return 2
    try:
        root = _ensure_private_state(args.state_dir)
        plan = prepare_plan(mode=args.mode)
        if args.mode == "repair4":
            verify_original_batch(plan)
        approval, fingerprint = load_approval(
            args.approval_file,
            mode=args.mode,
            sample_id=plan.sample_id,
            dataset_sha256=plan.bundle.snapshot_sha256,
            run_fingerprint=plan.run_fingerprint,
            plan_fingerprint=plan.plan_fingerprint,
            authorization_id=args.authorization_id,
            selected_window_id=OLD_PILOT_WINDOW if args.mode == "pilot" else None,
            selected_window_ids=tuple(w.extraction_window_id for w in plan.selected)
                if args.mode == "repair4" else None,
        )
        if args.approval_file.resolve().is_relative_to(root):
            raise BudgetError("APPROVAL_NOT_INDEPENDENT")
        ledger = BatchBudgetLedger(
            _budget_root(plan, root, approval),
            approval, fingerprint, _registry_root(plan, root),
        )
        ledger.enroll()
        status = ledger.status()
        assert status["attempts_reserved"] == 0
        print(json.dumps({
            "status": "ENROLLED_ZERO_ATTEMPTS",
            "authorization_id": args.authorization_id,
            "mode": args.mode,
            "max_new_attempts": approval.max_new_attempts,
            "worst_case_authorized_usd": approval.max_usd,
            "network_calls": 0,
        }))
        return 0
    except (BudgetError, LineageError) as exc:
        print(json.dumps({"status": exc.code, "network_calls": 0}))
        return 2
    except Exception:
        print(json.dumps({"status": "ENROLLMENT_UNEXPECTED_ERROR",
                          "network_calls": 0}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
