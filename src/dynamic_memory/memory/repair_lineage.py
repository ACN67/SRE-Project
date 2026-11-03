"""Read-only lineage checks for four-window conv-30 repair; never rewrites the old run."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from dynamic_memory.memory.checkpoints import WindowCheckpointStore
from dynamic_memory.memory.review_workflow import _fp, make_review_queue

FAILED_WINDOW_IDS = frozenset({
    "window_27c0c70beb97227c8707abce",
    "window_34776806a0251088cea22816",
    "window_4f55ba1f22c7864a11b89e6c",
    "window_8e0e259c26578e63bd788ead",
})
OLD_RUN = "sha256:b4c848e7f21acf01fb68ebefc18c5ed069fb32abe971b736bf628556683b8cd8"
OLD_AUTH = "user-chat-20261103-batch63-promptv3-01"
OLD_ROOT = Path(r"C:\Users\Keshu\AppData\Local\Temp\SRE_STAGE_B_REAL_PROMPT_V3_20261104_0242")
BASELINE = Path(r"C:\Users\Keshu\AppData\Local\Temp\SRE_STAGE_B_4WINDOW_REPAIR_20261104\baseline.json")
OLD_LEDGER = Path(r"C:\Users\Keshu\AppData\Local\SRE-Project-BudgetLedgers") / OLD_AUTH / "attempts.json"
OLD_MARKER = Path(r"C:\Users\Keshu\AppData\Local\SRE-Project-AuthorizationRegistry") / (OLD_AUTH + ".json")
OLD_APPROVAL = Path(r"C:\Users\Keshu\Documents\SRE-Project-Approvals\batch63_user_chat_20261103_promptv3_01.json")
OLD_SHA = {
    "ledger_sha256": "8b972ba585ec5933538b697c6012b340553bcb63f1630b48de8505c6bae24e5f",
    "registry_sha256": "bb472ef209a0b0b22f413cd602f7820ff978a59ba71fd7c8d8af9a8a523c858a",
    "approval_sha256": "1c0f6b5d45adb71335868c91855406cbaf60295a265a560b296a347ace023817",
}


class LineageError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(f"{code}: repair lineage unavailable")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_original_batch(plan):
    """Return old real results only after exact baseline and ledger reconciliation."""
    try:
        baseline = json.loads(BASELINE.read_bytes())
        if (plan.sample_id != "conv-30" or len(plan.windows) != 63 or
            plan.plan_fingerprint != baseline["current_plan_fingerprint"] or
            baseline["old_run_fingerprint"] != OLD_RUN or
            baseline["data_sha256"] != plan.bundle.snapshot_sha256 or
            {key: _sha(path) for key, path in (
                ("ledger_sha256", OLD_LEDGER), ("registry_sha256", OLD_MARKER),
                ("approval_sha256", OLD_APPROVAL))} != OLD_SHA):
            raise ValueError("baseline")
        ledger = json.loads(OLD_LEDGER.read_bytes())
        rows = ledger["attempts"]
        if (ledger["run_fingerprint"] != OLD_RUN or len(rows) != 63 or
            len({r["window_id"] for r in rows}) != 63 or
            {r["window_id"] for r in rows if r["state"] == "failed"} != FAILED_WINDOW_IDS or
            sum(r["state"] == "extracted" for r in rows) != 59 or
            any(r["state"] == "spent_unknown" for r in rows) or
            len(list((OLD_ROOT / "checkpoints").glob("window_*.json"))) != 59):
            raise ValueError("ledger")
        by_row = {r["window_id"]: r for r in rows}
        by_base = {r["window_id"]: r for r in baseline["windows"]}
        if (set(by_row) != set(by_base) or
            set(by_row) != {w.extraction_window_id for w in plan.windows}):
            raise ValueError("windows")
        store = WindowCheckpointStore(OLD_ROOT / "checkpoints", run_fingerprint=OLD_RUN)
        results = {}
        for w in plan.windows:
            wid = w.extraction_window_id
            row, item = by_row[wid], by_base[wid]
            cp = OLD_ROOT / "checkpoints" / (wid + ".json")
            if (item["input_sha256"] != hashlib.sha256(w.input_text.encode()).hexdigest() or
                item["source_refs"] != [r.model_dump(mode="json") for r in w.source_refs] or
                item["old_call_fingerprint"] != row["call_fingerprint"] or
                item["old_state"] != row["state"] or
                item["old_checkpoint_sha256"] != (_sha(cp) if cp.exists() else None)):
                raise ValueError("window drift")
            result = store.load(w, row["call_fingerprint"])
            if (result is None) != (wid in FAILED_WINDOW_IDS):
                raise ValueError("checkpoint")
            if result is not None:
                if result.template_fingerprint != baseline["old_prompt_fingerprint"]:
                    raise ValueError("prompt")
                results[wid] = result
        return results, by_row, baseline
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise LineageError("ORIGINAL_BATCH_DRIFT") from None


def merge_result_sets(plan, old_results, repaired_results, old_rows, new_rows,
                      baseline, new_authorization_id: str, new_approval_sha256: str,
                      new_checkpoint_root: Path):
    """Require all 63 unique results and bind each to its own immutable origin."""
    try:
        if (len(plan.windows) != 63 or len(old_results) != 59 or
            set(old_results) != {w.extraction_window_id for w in plan.windows} - FAILED_WINDOW_IDS or
            set(repaired_results) != FAILED_WINDOW_IDS or
            len(new_rows) != 4 or set(new_rows) != FAILED_WINDOW_IDS or
            plan.mode != "repair4"):
            raise ValueError("coverage")
        all_results = dict(old_results) | dict(repaired_results)
        all_calls = {}
        origins = []
        for w in plan.windows:
            wid = w.extraction_window_id
            repaired = wid in FAILED_WINDOW_IDS
            row = new_rows[wid] if repaired else old_rows[wid]
            result = all_results[wid]
            cp = (new_checkpoint_root if repaired else OLD_ROOT / "checkpoints") / (wid + ".json")
            run = plan.run_fingerprint if repaired else OLD_RUN
            call = plan.call_fingerprints[wid] if repaired else row["call_fingerprint"]
            if (row["state"] != "extracted" or row["call_fingerprint"] != call or
                result.sample_id != plan.sample_id or result.extraction_window_id != wid or
                result.template_fingerprint != (plan.public["prompt_fingerprint"] if repaired else
                    baseline["old_prompt_fingerprint"]) or not cp.is_file()):
                raise ValueError("origin")
            all_calls[wid] = call
            origins.append({"window_id": wid, "origin_run_fingerprint": run,
                "call_fingerprint": call,
                "authorization_id": new_authorization_id if repaired else OLD_AUTH,
                "approval_sha256": new_approval_sha256 if repaired else OLD_SHA["approval_sha256"],
                "checkpoint_sha256": _sha(cp),
                "input_sha256": hashlib.sha256(w.input_text.encode()).hexdigest(),
                "source_refs_fingerprint": _fp([r.model_dump(mode="json") for r in w.source_refs]),
                "prompt_fingerprint": result.template_fingerprint,
                "code_fingerprint": plan.code_fingerprint if repaired else baseline["old_code_fingerprint"]})
        manifest_body = {"format_version": 1, "sample_id": plan.sample_id,
            "split": plan.bundle.split_by_sample_id[plan.sample_id],
            "dataset_sha256": plan.bundle.snapshot_sha256,
            "plan_fingerprint": plan.plan_fingerprint,
            "origin_runs": [OLD_RUN, plan.run_fingerprint], "windows": origins}
        manifest = {"body": manifest_body, "digest": _fp(manifest_body)}
        queue = make_review_queue(run_fingerprint=_fp(manifest), sample_id=plan.sample_id,
            windows=plan.windows, call_fingerprints=all_calls, results=all_results,
            repository=plan.repository)
        return manifest, queue
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise LineageError("MERGED_EXTRACTION_INVALID") from None
