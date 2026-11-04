"""Plan or execute a fail-closed repair of one approved Stage 6 extraction."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from dynamic_memory.data.loader import load_conversations
from dynamic_memory.data.repository import SourceRepository
from dynamic_memory.memory.checkpoints import (
    WindowCheckpointStore, call_identity_fingerprint,
)
from dynamic_memory.memory.extraction import (
    MEMORY_DRAFT_PROMPT, WindowExtractionError, extract_window,
)
from dynamic_memory.memory.review_workflow import (
    _fp, export_review_queue, make_review_queue,
)
from dynamic_memory.memory.windows import plan_extraction_windows
from dynamic_memory.pipeline.runtime import create_runtime
from dynamic_memory.pipeline.settings import load_settings
from scripts.stage6_extract_sample import (
    ExtractionApproval, PROFILE, PROVIDER_OUTPUT_RESERVE, PEAK_INPUT_PER_MILLION,
    PEAK_OUTPUT_PER_MILLION, _code_fingerprint, _estimate, _git_sha,
    _read_api_key, _read_json, _write_json,
)

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/raw/locomo10.json"
SPLIT = ROOT / "configs/data_split.yaml"


class RepairError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _sha_path(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        raise RepairError("CHECKPOINT_READ_FAILED") from None


def _build_plan(plan_path: Path, state_dir: Path) -> dict:
    plan = _read_json(plan_path)
    failure = _read_json(state_dir / "failure.json")
    progress = _read_json(state_dir / "progress.json")
    if (
        not isinstance(plan, dict)
        or plan.get("status") != "STAGE6_EXTRACTION_PLAN_READY"
        or plan.get("sample_id") != "conv-26"
        or not isinstance(failure, dict)
        or failure.get("status") != "STAGE6_EXTRACTION_STOPPED"
        or failure.get("error_code") != "SCHEMA_INVALID"
        or not isinstance(progress, dict)
    ):
        raise RepairError("REPAIR_INPUT_INVALID")
    windows = plan.get("windows")
    failed_id = failure.get("failed_window_id")
    if not isinstance(windows, list) or not isinstance(failed_id, str):
        raise RepairError("REPAIR_INPUT_INVALID")
    failed_index = next(
        (index for index, row in enumerate(windows) if row.get("window_id") == failed_id),
        None,
    )
    if failed_index is None or failure.get("completed_windows") != failed_index:
        raise RepairError("REPAIR_BOUNDARY_MISMATCH")
    completed_ids = [row["window_id"] for row in windows[:failed_index]]
    checkpoint_root = state_dir / "checkpoints"
    checkpoint_files = sorted(checkpoint_root.glob("window_*.json"))
    if (
        len(checkpoint_files) != failed_index
        or {path.stem for path in checkpoint_files} != set(completed_ids)
        or (checkpoint_root / f"{failed_id}.json").exists()
    ):
        raise RepairError("REPAIR_CHECKPOINT_MISMATCH")
    attempts = _read_json(checkpoint_root / "attempts.json")
    if (
        not isinstance(attempts, dict)
        or not isinstance(attempts.get("attempts"), list)
        or len(attempts["attempts"]) != failed_index + 1
    ):
        raise RepairError("REPAIR_ATTEMPT_MISMATCH")
    remaining = windows[failed_index:]
    input_bytes = sum(row["request_input_bytes"] for row in remaining)
    worst = (
        Decimal(input_bytes) * PEAK_INPUT_PER_MILLION
        + Decimal(len(remaining) * PROVIDER_OUTPUT_RESERVE) * PEAK_OUTPUT_PER_MILLION
    ) / Decimal(1_000_000)
    by_window_id = {path.stem: path for path in checkpoint_files}
    prefix = [
        {
            "window_id": row["window_id"],
            "checkpoint_sha256": _sha_path(by_window_id[row["window_id"]]),
        }
        for row in windows[:failed_index]
    ]
    body = {
        "format_version": 1,
        "repair_of": plan["run_fingerprint"],
        "sample_id": plan["sample_id"],
        "dataset_sha256": plan["dataset_sha256"],
        "failed_window_id": failed_id,
        "failed_window_index": failed_index,
        "prefix": prefix,
        "remaining_windows": remaining,
        "runtime_config_fingerprint": plan["runtime_config_fingerprint"],
        "execution_policy_fingerprint": plan["execution_policy_fingerprint"],
        "prompt_fingerprint": plan["prompt_fingerprint"],
        "git_sha": _git_sha(),
        "code_fingerprint": _code_fingerprint(),
    }
    return {
        "format_version": 1,
        "status": "STAGE6_REPAIR_PLAN_READY",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "run_fingerprint": _fp(body),
        "original_plan_path": str(plan_path.resolve()),
        "original_state_path": str(state_dir.resolve()),
        "original_run_fingerprint": plan["run_fingerprint"],
        "sample_id": plan["sample_id"],
        "failed_window_id": failed_id,
        "failed_window_index": failed_index,
        "completed_prefix_windows": failed_index,
        "prefix": prefix,
        "remaining_windows": remaining,
        "window_count": len(windows),
        "budget": {
            "max_http_attempts": len(remaining),
            "input_bytes_upper": input_bytes,
            "peak_input_usd_per_million": str(PEAK_INPUT_PER_MILLION),
            "peak_output_usd_per_million": str(PEAK_OUTPUT_PER_MILLION),
            "worst_case_usd": format(worst, ".7f"),
        },
        "provider": plan["provider"],
        "network_calls": 0,
        "requires_user_approval": True,
    }


def _validate_approval(approval: ExtractionApproval, plan: dict,
                       approval_path: Path, output_dir: Path) -> None:
    now = datetime.now(timezone.utc)
    if (
        approval.run_fingerprint != plan["run_fingerprint"]
        or approval.sample_id != plan["sample_id"]
        or approval.max_http_attempts != plan["budget"]["max_http_attempts"]
        or approval.total_input_bytes_upper != plan["budget"]["input_bytes_upper"]
        or approval.max_total_usd != Decimal(plan["budget"]["worst_case_usd"])
        or approval.approved_at.tzinfo is None
        or approval.expires_at.tzinfo is None
        or approval.approved_at > now
        or approval.expires_at <= now
        or approval.price_checked_on != now.date()
        or approval_path.resolve().is_relative_to(output_dir.resolve())
    ):
        raise RepairError("APPROVAL_MISMATCH")


def _load_approval(path: Path) -> ExtractionApproval:
    try:
        return ExtractionApproval.model_validate_json(path.read_bytes())
    except Exception:
        raise RepairError("APPROVAL_INVALID") from None


def _execute(plan: dict, approval_path: Path, output_dir: Path) -> dict:
    if not isinstance(plan, dict) or plan.get("status") != "STAGE6_REPAIR_PLAN_READY":
        raise RepairError("REPAIR_PLAN_INVALID")
    rebuilt = _build_plan(
        Path(plan["original_plan_path"]), Path(plan["original_state_path"])
    )
    if rebuilt["run_fingerprint"] != plan["run_fingerprint"]:
        raise RepairError("REPAIR_FINGERPRINT_CHANGED")
    approval = _load_approval(approval_path)
    _validate_approval(approval, plan, approval_path, output_dir)
    key = _read_api_key()
    if not key:
        raise RepairError("CREDENTIAL_REQUIRED")

    original_plan = _read_json(Path(plan["original_plan_path"]))
    original_state = Path(plan["original_state_path"])
    settings = load_settings(models_profile=PROFILE)
    bundle = load_conversations(str(DATA), str(SPLIT))
    repository = SourceRepository(bundle)
    sample = repository.get_sample(plan["sample_id"])
    windows = tuple(plan_extraction_windows(
        sample, settings.extraction, reserved_prompt_tokens=1800
    ))
    if len(windows) != plan["window_count"]:
        raise RepairError("WINDOW_PLAN_DRIFT")

    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "repair_plan.json", plan)
    checkpoint_root = output_dir / "checkpoints"
    checkpoint_root.mkdir()
    source_checkpoints = original_state / "checkpoints"
    for row in original_plan["windows"][:plan["completed_prefix_windows"]]:
        source = source_checkpoints / f"{row['window_id']}.json"
        if _sha_path(source) != next(
            item["checkpoint_sha256"] for item in plan["prefix"]
            if item["window_id"] == row["window_id"]
        ):
            raise RepairError("REPAIR_CHECKPOINT_DRIFT")
        shutil.copyfile(source, checkpoint_root / source.name)

    runtime = create_runtime(
        settings,
        backend="deepseek",
        api_enabled=True,
        api_key=key,
        model_id="deepseek-flash",
        token_estimator=_estimate,
        estimator_version="utf8-bytes-plus16-v1",
        strict_budget=True,
        cache_enabled=False,
    )
    checkpoints = WindowCheckpointStore(
        checkpoint_root,
        run_fingerprint=plan["original_run_fingerprint"],
        max_new_attempts=approval.max_http_attempts,
    )
    git_sha = _git_sha()
    results = {}
    call_fingerprints = {}
    progress = []
    new_calls = 0
    for index, window in enumerate(windows):
        prepared = runtime.make_request(
            MEMORY_DRAFT_PROMPT, {"conversation": window.input_text}
        )
        identity = runtime.make_identity(
            prepared,
            sample_id=sample.sample_id,
            scope_id=window.extraction_window_id,
            run_kind="development",
            method=None,
            data_fingerprint="sha256:" + hashlib.sha256(window.input_text.encode()).hexdigest(),
            schema_id="atomic_fact_draft",
            schema_version="1",
            retriever_fingerprint=None,
            query_analysis_version=None,
            git_sha=git_sha,
        )
        call_fp = call_identity_fingerprint(identity)
        result = checkpoints.load(window, call_fp)
        if result is None:
            if index < plan["failed_window_index"]:
                raise RepairError("PREFIX_CHECKPOINT_INVALID")
            checkpoints.claim_new_attempt(window, call_fp)
            new_calls += 1
            try:
                result = extract_window(
                    window,
                    runtime.recorder,
                    identity,
                    settings.models,
                    token_estimator=_estimate,
                )
            except WindowExtractionError as exc:
                _write_json(output_dir / "failure.json", {
                    "status": "STAGE6_REPAIR_STOPPED",
                    "sample_id": sample.sample_id,
                    "failed_window_id": window.extraction_window_id,
                    "error_code": exc.code,
                    "completed_windows": len(results),
                    "network_calls": new_calls,
                })
                raise RepairError(exc.code) from None
            checkpoints.save(window, result, call_fp)
            events = runtime.recorder.store.events()
            event = events[-1] if events else None
        else:
            event = None
        results[window.extraction_window_id] = result
        call_fingerprints[window.extraction_window_id] = call_fp
        progress.append({
            "window_id": window.extraction_window_id,
            "new_call": index >= plan["failed_window_index"],
            "fact_count": len(result.facts),
            "event": event,
        })
        _write_json(output_dir / "progress.json", {
            "sample_id": sample.sample_id,
            "completed_windows": len(results),
            "total_windows": len(windows),
            "new_http_calls": new_calls,
            "facts": sum(len(item.facts) for item in results.values()),
            "windows": progress,
        })

    manifest_body = {
        "format_version": 1,
        "sample_id": sample.sample_id,
        "dataset_sha256": bundle.snapshot_sha256,
        "run_fingerprint": plan["original_run_fingerprint"],
        "window_count": len(windows),
        "windows": original_plan["windows"],
    }
    manifest = {"body": manifest_body, "digest": _fp(manifest_body)}
    _write_json(output_dir / "manifest.json", manifest)
    queue = make_review_queue(
        run_fingerprint=_fp(manifest),
        sample_id=sample.sample_id,
        windows=windows,
        call_fingerprints=call_fingerprints,
        results=results,
        repository=repository,
    )
    queue_path = output_dir / "private_review" / "queue.json"
    export_review_queue(queue, queue_path)
    result = {
        "status": "STAGE6_REPAIR_COMPLETE",
        "sample_id": sample.sample_id,
        "repair_fingerprint": plan["run_fingerprint"],
        "original_run_fingerprint": plan["original_run_fingerprint"],
        "windows_completed": len(results),
        "facts": sum(len(result.facts) for result in results.values()),
        "new_http_calls": new_calls,
        "queue_path": str(queue_path),
        "queue_fingerprint": queue.fingerprint,
        "manifest_path": str(output_dir / "manifest.json"),
    }
    _write_json(output_dir / "repair_summary.json", result)
    return result


def _safe_output(path: Path) -> Path:
    target = path.resolve()
    if not target.is_relative_to((ROOT / "results").resolve()):
        raise RepairError("OUTPUT_DIRECTORY_UNSAFE")
    target.mkdir(parents=True, exist_ok=True)
    if any(target.iterdir()):
        raise RepairError("OUTPUT_DIRECTORY_IN_USE")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("plan", "live"), required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--approval", type=Path)
    args = parser.parse_args()
    try:
        plan = _build_plan(args.plan, args.state_dir)
        if args.mode == "plan":
            output_dir = _safe_output(args.output_dir)
            _write_json(output_dir / "repair_plan.json", plan)
            print(json.dumps({
                "status": plan["status"],
                "sample_id": plan["sample_id"],
                "run_fingerprint": plan["run_fingerprint"],
                "failed_window_id": plan["failed_window_id"],
                "plan": str(output_dir / "repair_plan.json"),
                **plan["budget"],
            }))
        else:
            if args.approval is None:
                raise RepairError("APPROVAL_REQUIRED")
            output_dir = _safe_output(args.output_dir)
            result = _execute(plan, args.approval, output_dir)
            print(json.dumps(result))
        return 0
    except Exception as exc:
        code = getattr(exc, "code", None)
        print(json.dumps({
            "status": code if isinstance(code, str) else "STAGE6_REPAIR_STOPPED",
            "error_type": type(exc).__name__,
        }))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
