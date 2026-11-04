"""Inspect a private queue, verify review files, or run conservative automatic review."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from dynamic_memory.memory.review_workflow import (
    ReviewError, ReviewQueue, verify_review_files,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--pending-template", type=Path)
    parser.add_argument("--auto-output-dir", type=Path,
                        help="write machine-identified source-text decisions; never overwrites")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--authority", type=Path)
    parser.add_argument("--run-kind", choices=["real", "offline_test"], default="real")
    args = parser.parse_args()
    try:
        queue = ReviewQueue.model_validate_json(args.queue.read_bytes())
        summary = {"sample_id": queue.sample_id, "run_fingerprint": queue.run_fingerprint,
            "queue_fingerprint": queue.fingerprint,
            "windows": len(queue.windows),
            "facts": sum(len(window.facts) for window in queue.windows),
            "automatically_supported": sum(f.automatic_status == "supported"
                for w in queue.windows for f in w.facts),
            "automatically_unclear": sum(f.automatic_status == "unclear"
                for w in queue.windows for f in w.facts),
            "automatically_unsupported": sum(f.automatic_status == "unsupported"
                for w in queue.windows for f in w.facts)}
        if args.auto_output_dir is not None:
            if args.pending_template is not None or args.verify:
                raise ReviewError("REVIEW_ACTION_INVALID")
            from dynamic_memory.memory.auto_review import write_auto_review_files
            approval_path, authority_path = write_auto_review_files(queue, args.auto_output_dir,
                run_kind=args.run_kind)
            verified = verify_review_files(queue, approval_path, authority_path,
                run_kind=args.run_kind)
            summary.update({"status": "AUTO_REVIEW_VERIFIED",
                "reviewer_id": verified.approval.reviewer_id,
                "approved_facts": sum(f.decision == "approved" for w in verified.approval.windows for f in w.facts),
                "unclear_facts": sum(f.decision == "unclear" for w in verified.approval.windows for f in w.facts),
                "approval_path": str(approval_path), "authority_path": str(authority_path),
                "approval_fingerprint": verified.approval_fingerprint})
            print(json.dumps(summary, ensure_ascii=False))
            return 0
        if args.pending_template is not None:
            # This file deliberately cannot pass ReviewApproval validation until
            # an independent human supplies reviewer, time, decisions and reasons.
            template = {"format_version": 1, "run_fingerprint": queue.run_fingerprint,
                "queue_fingerprint": queue.fingerprint, "reviewer_id": None,
                "reviewed_at": None, "authority_version": None,
                "windows": [{"window_id": window.window_id,
                    "source_refs_fingerprint": window.source_refs_fingerprint,
                    "decision": "PENDING",
                    "facts": [{"draft_fingerprint": fact.draft_fingerprint,
                        "evidence_fingerprint": fact.evidence_fingerprint,
                        "decision": "PENDING", "reason_code": None,
                        "confirmed_source_fingerprints": [],
                        "relation_confirmed": False}
                        for fact in window.facts]} for window in queue.windows]}
            args.pending_template.parent.mkdir(parents=True, exist_ok=True)
            with args.pending_template.open("x", encoding="utf-8") as stream:
                json.dump(template, stream, ensure_ascii=False, indent=2)
            summary["pending_template_path"] = str(args.pending_template)
        if args.verify:
            if args.approval is None or args.authority is None:
                raise ReviewError("REVIEW_FILES_REQUIRED")
            verified = verify_review_files(queue, args.approval, args.authority,
                                           run_kind=args.run_kind)
            summary["status"] = "VERIFIED_FILE_CONSISTENCY"
            summary["approval_fingerprint"] = verified.approval_fingerprint
        elif args.inspect or args.pending_template is not None:
            summary["status"] = "PENDING_HUMAN_REVIEW"
        else:
            raise ReviewError("REVIEW_ACTION_REQUIRED")
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    except ReviewError as exc:
        print(json.dumps({"status": exc.code}))
        return 2
    except Exception:
        print(json.dumps({"status": "REVIEW_INPUT_INVALID"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
