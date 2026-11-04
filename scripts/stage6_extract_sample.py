"""Plan or execute one approved Stage 6 main_dev extraction batch."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import asdict
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from dynamic_memory.data.loader import load_conversations
from dynamic_memory.data.repository import SourceRepository
from dynamic_memory.memory.checkpoints import (
    WindowCheckpointStore, call_identity_fingerprint,
)
from dynamic_memory.memory.extraction import (
    MEMORY_DRAFT_PROMPT, WindowExtractionError, build_extraction_request,
    extract_window,
)
from dynamic_memory.memory.review_workflow import (
    ReviewQueue, _fp, export_review_queue, make_review_queue,
)
from dynamic_memory.memory.windows import plan_extraction_windows
from dynamic_memory.pipeline.runtime import create_runtime
from dynamic_memory.pipeline.settings import AppSettings, load_settings

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/raw/locomo10.json"
SPLIT = ROOT / "configs/data_split.yaml"
PROFILE = ROOT / "configs/models_high_pilot.yaml"
ALLOWED_SAMPLES = ("conv-26", "conv-41", "conv-42")
PEAK_INPUT_PER_MILLION = Decimal("0.30")
PEAK_OUTPUT_PER_MILLION = Decimal("1.20")
PROVIDER_OUTPUT_RESERVE = 65536


class Stage6Error(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ExtractionApproval(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    format_version: Literal[1] = 1
    authorization_id: str = Field(pattern=r"^user-chat-[A-Za-z0-9_.:-]{1,80}$")
    approved_by: Literal["ACN67"]
    approved_at: datetime
    expires_at: datetime
    run_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    sample_id: Literal["conv-26", "conv-41", "conv-42"]
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


class _NoNetworkClient:
    def __init__(self) -> None:
        from types import SimpleNamespace

        self.chat = SimpleNamespace(completions=self)

    def create(self, **_kwargs):
        raise Stage6Error("PLAN_MODE_CANNOT_SEND")


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.",
            suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write((_canonical(value) + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        raise Stage6Error("OUTPUT_WRITE_FAILED") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise Stage6Error("INPUT_READ_FAILED") from None


def _estimate(messages) -> int:
    return sum(len(message.content.encode("utf-8")) + 16 for message in messages)


def _read_api_key() -> str | None:
    value = os.environ.get("DEEPSEEK_API_KEY")
    if value and value.strip():
        return value
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
            value, _ = winreg.QueryValueEx(handle, "DEEPSEEK_API_KEY")
    except OSError:
        return None
    return value if isinstance(value, str) and value.strip() else None


def _git_sha() -> str:
    try:
        value = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        raise Stage6Error("GIT_IDENTITY_UNAVAILABLE") from None
    if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
        raise Stage6Error("GIT_IDENTITY_UNAVAILABLE")
    return value


def _code_fingerprint() -> str:
    rows = []
    for base in (ROOT / "src", ROOT / "evaluation", ROOT / "scripts", ROOT / "configs"):
        for path in sorted(item for item in base.rglob("*") if item.is_file()):
            if path.suffix not in {".py", ".yaml"}:
                continue
            rows.append({
                "path": path.relative_to(ROOT).as_posix(),
                "sha256": _sha_bytes(path.read_bytes()),
            })
    return _fp(rows)


def _runtime(settings: AppSettings, client):
    return create_runtime(
        settings,
        backend="deepseek",
        api_enabled=True,
        deepseek_client=client,
        model_id="deepseek-flash",
        token_estimator=_estimate,
        estimator_version="utf8-bytes-plus16-v1",
        strict_budget=True,
        cache_enabled=False,
    )


def _plan(sample_id: str) -> dict:
    if sample_id not in ALLOWED_SAMPLES:
        raise Stage6Error("SAMPLE_NOT_ALLOWED")
    settings = load_settings(models_profile=PROFILE)
    models = settings.models
    if (
        models.default_backend != "mock"
        or models.api_enabled_by_default
        or models.model_id != "deepseek-flash"
        or models.thinking != "enabled"
        or models.reasoning_effort != "high"
        or models.max_retries != 0
        or models.max_concurrent_api_requests != 1
        or not models.extraction_omit_max_tokens
        or models.extraction_max_output_tokens != PROVIDER_OUTPUT_RESERVE
    ):
        raise Stage6Error("EXTRACTION_PROFILE_UNSAFE")
    bundle = load_conversations(str(DATA), str(SPLIT))
    if bundle.snapshot_sha256 != (
        "79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4"
    ):
        raise Stage6Error("DATA_SNAPSHOT_MISMATCH")
    if bundle.split_by_sample_id.get(sample_id) != "main_dev":
        raise Stage6Error("SPLIT_MISMATCH")
    repository = SourceRepository(bundle)
    sample = repository.get_sample(sample_id)
    windows = tuple(plan_extraction_windows(
        sample, settings.extraction, reserved_prompt_tokens=1800
    ))
    runtime = _runtime(settings, _NoNetworkClient())
    git_sha = _git_sha()
    code_fingerprint = _code_fingerprint()
    window_rows = []
    identities = []
    total_input_bytes = 0
    for window in windows:
        request = build_extraction_request(window, models)
        request_bytes = sum(len(message.content.encode("utf-8")) for message in request.messages)
        if not request.settings.omit_max_tokens:
            raise Stage6Error("EXTRACTION_PROFILE_UNSAFE")
        prepared = runtime.make_request(
            MEMORY_DRAFT_PROMPT, {"conversation": window.input_text}
        )
        if prepared.request != request:
            raise Stage6Error("REQUEST_MISMATCH")
        identity = runtime.make_identity(
            prepared,
            sample_id=sample_id,
            scope_id=window.extraction_window_id,
            run_kind="development",
            method=None,
            data_fingerprint="sha256:" + _sha_bytes(window.input_text.encode("utf-8")),
            schema_id="atomic_fact_draft",
            schema_version="1",
            retriever_fingerprint=None,
            query_analysis_version=None,
            git_sha=git_sha,
        )
        identities.append(identity)
        total_input_bytes += request_bytes
        window_rows.append({
            "window_id": window.extraction_window_id,
            "source_refs": [ref.model_dump(mode="json") for ref in window.source_refs],
            "input_sha256": _sha_bytes(window.input_text.encode("utf-8")),
            "request_input_bytes": request_bytes,
            "max_input_tokens": request.settings.max_input_tokens,
        })
    total_upper = (
        Decimal(total_input_bytes) * PEAK_INPUT_PER_MILLION
        + Decimal(len(windows) * PROVIDER_OUTPUT_RESERVE) * PEAK_OUTPUT_PER_MILLION
    ) / Decimal(1_000_000)
    plan_body = {
        "sample_id": sample_id,
        "dataset_sha256": bundle.snapshot_sha256,
        "windows": window_rows,
        "runtime_config_fingerprint": runtime.config_fingerprint,
        "execution_policy_fingerprint": runtime.execution_policy_fingerprint,
        "prompt_fingerprint": MEMORY_DRAFT_PROMPT.template_fingerprint,
        "git_sha": git_sha,
        "code_fingerprint": code_fingerprint,
    }
    run_fingerprint = _fp(plan_body)
    return {
        "format_version": 1,
        "status": "STAGE6_EXTRACTION_PLAN_READY",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "run_fingerprint": run_fingerprint,
        "sample_id": sample_id,
        "dataset_sha256": bundle.snapshot_sha256,
        "windows": window_rows,
        "window_count": len(windows),
        "runtime_config_fingerprint": runtime.config_fingerprint,
        "execution_policy_fingerprint": runtime.execution_policy_fingerprint,
        "prompt_fingerprint": MEMORY_DRAFT_PROMPT.template_fingerprint,
        "git_sha": git_sha,
        "code_fingerprint": code_fingerprint,
        "provider": {
            "model_id": "deepseek-flash",
            "thinking": "enabled",
            "reasoning_effort": "high",
            "request_max_tokens_omitted": True,
            "provider_default_output_tokens": PROVIDER_OUTPUT_RESERVE,
            "retries": 0,
            "max_concurrency": 1,
        },
        "budget": {
            "input_bytes_upper": total_input_bytes,
            "max_http_attempts": len(windows),
            "peak_input_usd_per_million": str(PEAK_INPUT_PER_MILLION),
            "peak_output_usd_per_million": str(PEAK_OUTPUT_PER_MILLION),
            "worst_case_usd": format(total_upper, ".7f"),
        },
        "network_calls": 0,
        "requires_user_approval": True,
    }


def _load_approval(path: Path) -> ExtractionApproval:
    try:
        return ExtractionApproval.model_validate_json(path.read_bytes())
    except (OSError, UnicodeError, ValidationError):
        raise Stage6Error("APPROVAL_INVALID") from None


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
        raise Stage6Error("APPROVAL_MISMATCH")


def _execute(plan_path: Path, approval_path: Path, output_dir: Path) -> dict:
    plan = _read_json(plan_path)
    if not isinstance(plan, dict) or plan.get("status") != "STAGE6_EXTRACTION_PLAN_READY":
        raise Stage6Error("PLAN_INVALID")
    if _plan(plan["sample_id"])["run_fingerprint"] != plan["run_fingerprint"]:
        raise Stage6Error("RUN_FINGERPRINT_CHANGED")
    approval = _load_approval(approval_path)
    _validate_approval(approval, plan, approval_path, output_dir)
    key = _read_api_key()
    if not key:
        raise Stage6Error("CREDENTIAL_REQUIRED")

    settings = load_settings(models_profile=PROFILE)
    bundle = load_conversations(str(DATA), str(SPLIT))
    repository = SourceRepository(bundle)
    sample = repository.get_sample(plan["sample_id"])
    windows = tuple(plan_extraction_windows(
        sample, settings.extraction, reserved_prompt_tokens=1800
    ))
    if len(windows) != plan["window_count"]:
        raise Stage6Error("WINDOW_PLAN_DRIFT")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise Stage6Error("OUTPUT_DIRECTORY_IN_USE")
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "plan.json", plan)
    checkpoints = WindowCheckpointStore(
        output_dir / "checkpoints",
        run_fingerprint=plan["run_fingerprint"],
        max_new_attempts=approval.max_http_attempts,
    )
    runtime = create_runtime(
        settings, backend="deepseek", api_enabled=True, api_key=key,
        model_id="deepseek-flash", token_estimator=_estimate,
        estimator_version="utf8-bytes-plus16-v1", strict_budget=True,
        cache_enabled=False,
    )
    git_sha = _git_sha()
    results = {}
    call_fingerprints = {}
    progress = []
    new_calls = 0
    for window in windows:
        prepared = runtime.make_request(
            MEMORY_DRAFT_PROMPT, {"conversation": window.input_text}
        )
        identity = runtime.make_identity(
            prepared,
            sample_id=sample.sample_id,
            scope_id=window.extraction_window_id,
            run_kind="development",
            method=None,
            data_fingerprint="sha256:" + _sha_bytes(window.input_text.encode("utf-8")),
            schema_id="atomic_fact_draft",
            schema_version="1",
            retriever_fingerprint=None,
            query_analysis_version=None,
            git_sha=git_sha,
        )
        call_fp = call_identity_fingerprint(identity)
        result = checkpoints.load(window, call_fp)
        if result is None:
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
                failure = {
                    "status": "STAGE6_EXTRACTION_STOPPED",
                    "sample_id": sample.sample_id,
                    "failed_window_id": window.extraction_window_id,
                    "error_code": exc.code,
                    "completed_windows": len(results),
                    "network_calls": new_calls,
                }
                _write_json(output_dir / "failure.json", failure)
                raise Stage6Error(exc.code) from None
            checkpoints.save(window, result, call_fp)
            events = runtime.recorder.store.events()
            event = events[-1] if events else None
            progress.append({
                "window_id": window.extraction_window_id,
                "new_call": True,
                "fact_count": len(result.facts),
                "event": event,
            })
        else:
            progress.append({
                "window_id": window.extraction_window_id,
                "new_call": False,
                "fact_count": len(result.facts),
            })
        results[window.extraction_window_id] = result
        call_fingerprints[window.extraction_window_id] = call_fp
        _write_json(output_dir / "progress.json", {
            "sample_id": sample.sample_id,
            "completed_windows": len(results),
            "total_windows": len(windows),
            "new_http_calls": new_calls,
            "facts": sum(len(result.facts) for result in results.values()),
            "windows": progress,
        })

    manifest_body = {
        "format_version": 1,
        "sample_id": sample.sample_id,
        "dataset_sha256": bundle.snapshot_sha256,
        "run_fingerprint": plan["run_fingerprint"],
        "window_count": len(windows),
        "windows": plan["windows"],
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
        "status": "STAGE6_EXTRACTION_COMPLETE",
        "sample_id": sample.sample_id,
        "run_fingerprint": plan["run_fingerprint"],
        "windows_completed": len(results),
        "facts": sum(len(result.facts) for result in results.values()),
        "new_http_calls": new_calls,
        "queue_path": str(queue_path),
        "queue_fingerprint": queue.fingerprint,
        "manifest_path": str(output_dir / "manifest.json"),
    }
    _write_json(output_dir / "extraction_summary.json", result)
    return result


def _safe_output(path: Path) -> Path:
    target = path.resolve()
    if not target.is_relative_to((ROOT / "results").resolve()):
        raise Stage6Error("OUTPUT_DIRECTORY_UNSAFE")
    if target.exists() and any(target.iterdir()):
        raise Stage6Error("OUTPUT_DIRECTORY_IN_USE")
    target.mkdir(parents=True, exist_ok=True)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("plan", "live"), required=True)
    parser.add_argument("--sample", required=True, choices=ALLOWED_SAMPLES)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--approval", type=Path)
    args = parser.parse_args()
    try:
        if args.mode == "plan":
            plan = _plan(args.sample)
            output_dir = _safe_output(args.output_dir)
            _write_json(output_dir / "plan.json", plan)
            print(json.dumps({
                "status": plan["status"],
                "sample_id": plan["sample_id"],
                "run_fingerprint": plan["run_fingerprint"],
                "plan": str(output_dir / "plan.json"),
                **plan["budget"],
            }))
            return 0
        if args.plan is None or args.approval is None:
            raise Stage6Error("PLAN_AND_APPROVAL_REQUIRED")
        output_dir = _safe_output(args.output_dir)
        result = _execute(args.plan, args.approval, output_dir)
        _write_json(output_dir / "plan.json", _read_json(args.plan))
        print(json.dumps(result))
        return 0
    except Exception as exc:
        code = getattr(exc, "code", None)
        print(json.dumps({
            "status": code if isinstance(code, str) else "STAGE6_STOPPED",
            "error_type": type(exc).__name__,
        }))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
