"""Plan or execute an independently approved Stage 6 v3 semantic review."""

from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from dynamic_memory.memory.review_workflow import ReviewQueue, _fp
from dynamic_memory.memory.semantic_review import (
    SEMANTIC_API_MODEL, SEMANTIC_AUTHORITY_VERSION, SEMANTIC_REVIEWER_ID,
    _SYSTEM, _prompt, run_semantic_review,
)
from scripts.stage6_extract_sample import (
    PEAK_INPUT_PER_MILLION, PEAK_OUTPUT_PER_MILLION, PROVIDER_OUTPUT_RESERVE,
    _code_fingerprint, _git_sha, _read_api_key, _read_json, _write_json,
)

ROOT = Path(__file__).resolve().parents[1]


class SemanticReviewError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class SemanticApproval(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    format_version: Literal[1] = 1
    authorization_id: str = Field(pattern=r"^user-chat-[A-Za-z0-9_.:-]{1,80}$")
    approved_by: Literal["ACN67"]
    approved_at: datetime
    expires_at: datetime
    run_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    queue_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    sample_id: Literal["conv-26"]
    model_id: Literal["deepseek-flash"]
    thinking: Literal["enabled"]
    reasoning_effort: Literal["high"]
    request_max_tokens_omitted: Literal[True]
    max_http_attempts: int = Field(ge=1)
    retry_attempts: Literal[0]
    max_concurrency: Literal[1]
    total_input_bytes_upper: int = Field(ge=1)
    max_total_usd: Decimal
    price_checked_on: date


def _plan(queue_path: Path) -> dict:
    try:
        queue = ReviewQueue.model_validate_json(queue_path.read_bytes())
    except Exception:
        raise SemanticReviewError("QUEUE_INVALID") from None
    if (
        queue.sample_id != "conv-26"
        or len(queue.windows) != 83
        or sum(len(window.facts) for window in queue.windows) != 887
    ):
        raise SemanticReviewError("QUEUE_IDENTITY_MISMATCH")
    sizes = [
        len(_SYSTEM.encode("utf-8")) + len(_prompt(window).encode("utf-8")) + 32
        for window in queue.windows
    ]
    input_bytes = sum(sizes)
    worst = (
        Decimal(input_bytes) * PEAK_INPUT_PER_MILLION
        + Decimal(len(queue.windows) * PROVIDER_OUTPUT_RESERVE)
        * PEAK_OUTPUT_PER_MILLION
    ) / Decimal(1_000_000)
    git_sha = _git_sha()
    code_fingerprint = _code_fingerprint()
    body = {
        "queue_fingerprint": queue.fingerprint,
        "sample_id": queue.sample_id,
        "windows": len(queue.windows),
        "facts": sum(len(window.facts) for window in queue.windows),
        "input_bytes_upper": input_bytes,
        "max_http_attempts": len(queue.windows),
        "provider_default_output_tokens": PROVIDER_OUTPUT_RESERVE,
        "model_id": SEMANTIC_API_MODEL,
        "reviewer_id": SEMANTIC_REVIEWER_ID,
        "authority_version": SEMANTIC_AUTHORITY_VERSION,
        "git_sha": git_sha,
        "code_fingerprint": code_fingerprint,
    }
    return {
        "format_version": 1,
        "status": "STAGE6_SEMANTIC_PLAN_READY",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "run_fingerprint": _fp(body),
        "queue_fingerprint": queue.fingerprint,
        "sample_id": queue.sample_id,
        "windows": len(queue.windows),
        "facts": body["facts"],
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
            "input_bytes_upper": input_bytes,
            "max_input_bytes": max(sizes),
            "max_http_attempts": len(queue.windows),
            "peak_input_usd_per_million": str(PEAK_INPUT_PER_MILLION),
            "peak_output_usd_per_million": str(PEAK_OUTPUT_PER_MILLION),
            "worst_case_usd": format(worst, ".7f"),
        },
        "git_sha": git_sha,
        "code_fingerprint": code_fingerprint,
        "network_calls": 0,
        "requires_user_approval": True,
    }


def _validate_approval(approval: SemanticApproval, plan: dict,
                       approval_path: Path, output_dir: Path) -> None:
    now = datetime.now(timezone.utc)
    if (
        approval.run_fingerprint != plan["run_fingerprint"]
        or approval.queue_fingerprint != plan["queue_fingerprint"]
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
        raise SemanticReviewError("APPROVAL_MISMATCH")


def _load_approval(path: Path) -> SemanticApproval:
    try:
        return SemanticApproval.model_validate_json(path.read_bytes())
    except (OSError, UnicodeError, ValidationError):
        raise SemanticReviewError("APPROVAL_INVALID") from None


def _safe_output(path: Path) -> Path:
    target = path.resolve()
    if not target.is_relative_to((ROOT / "results").resolve()):
        raise SemanticReviewError("OUTPUT_DIRECTORY_UNSAFE")
    if target.exists() and any(target.iterdir()):
        raise SemanticReviewError("OUTPUT_DIRECTORY_IN_USE")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("plan", "live"), required=True)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--approval", type=Path)
    args = parser.parse_args()
    try:
        plan = _plan(args.queue)
        if args.mode == "plan":
            output_dir = _safe_output(args.output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            _write_json(output_dir / "plan.json", plan)
            print(json.dumps({
                "status": plan["status"],
                "run_fingerprint": plan["run_fingerprint"],
                "queue_fingerprint": plan["queue_fingerprint"],
                "plan": str(output_dir / "plan.json"),
                **plan["budget"],
            }))
            return 0
        if args.plan is None or args.approval is None:
            raise SemanticReviewError("PLAN_AND_APPROVAL_REQUIRED")
        stored = _read_json(args.plan)
        if not isinstance(stored, dict) or stored.get("run_fingerprint") != plan["run_fingerprint"]:
            raise SemanticReviewError("PLAN_MISMATCH")
        approval = _load_approval(args.approval)
        output_dir = _safe_output(args.output_dir)
        _validate_approval(approval, plan, args.approval, output_dir)
        key = _read_api_key()
        if key is None:
            raise SemanticReviewError("CREDENTIAL_REQUIRED")
        queue = ReviewQueue.model_validate_json(args.queue.read_bytes())
        output_dir.mkdir(parents=True, exist_ok=True)
        _write_json(output_dir / "plan.json", plan)
        summary = run_semantic_review(
            queue,
            output_dir,
            max_new_calls=approval.max_http_attempts,
            api_key=key,
            run_kind="real",
        )
        _write_json(output_dir / "run_summary.json", {
            "format_version": 1,
            "status": summary["status"],
            "authorization_id": approval.authorization_id,
            "run_fingerprint": plan["run_fingerprint"],
            "queue_fingerprint": plan["queue_fingerprint"],
            **summary,
        })
        print(json.dumps(summary))
        return 0
    except Exception as exc:
        code = getattr(exc, "code", None)
        print(json.dumps({
            "status": code if isinstance(code, str) else "STAGE6_SEMANTIC_STOPPED",
            "error_type": type(exc).__name__,
        }))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
