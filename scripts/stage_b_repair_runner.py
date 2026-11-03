"""Explicitly authorized conv-30 pilot/batch; default is zero-network dry-run."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from dynamic_memory.contracts import DatasetBundle, memory_set_fingerprint
from dynamic_memory.data.loader import load_conversations
from dynamic_memory.data.repository import SourceRepository
from dynamic_memory.integration.artifacts import ArtifactPorts
from dynamic_memory.llm.recording import call_cache_key
from dynamic_memory.llm.deepseek import DeepSeekAdapterError, _new_sdk_client
from dynamic_memory.memory.artifact_store import ArtifactStoreError, ReviewAttestation, WindowReview
from dynamic_memory.memory.batch_budget import BatchApproval, BatchBudgetLedger, BudgetError, load_approval
from dynamic_memory.memory.build import MemoryBuilder
from dynamic_memory.memory.checkpoints import WindowCheckpointStore, call_identity_fingerprint
from dynamic_memory.memory.checkpoints import CheckpointError
from dynamic_memory.memory.extraction import (
    MEMORY_DRAFT_PROMPT, WindowExtractionError, build_extraction_request, extract_window,
)
from dynamic_memory.memory.review_workflow import (
    ReviewError, ReviewQueue, _fp, export_review_queue, make_review_queue, verify_review_files,
)
from dynamic_memory.memory.windows import ExtractionWindow, plan_extraction_windows
from dynamic_memory.memory.repair_lineage import FAILED_WINDOW_IDS, LineageError, verify_original_batch
from dynamic_memory.pipeline.runtime import create_runtime
from dynamic_memory.pipeline.settings import AppSettings, load_settings
from dynamic_memory.retrieval import (
    EmbeddingConfig, IndexManager, LocalIndexEncoder, VectorRetriever, analyze_query, save_index,
)
from dynamic_memory.contracts.models import QueryRequest
from scripts.stage_a_deepseek_smoke import read_key

ROOT = Path(__file__).resolve().parents[1]
DATA_SHA = "79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4"
EXTRACTOR_VERSION = "b-repair-deepseek-flash-high-v1"
PROVIDER_DEFAULT_VERIFIED_ON = "2026-11-03"
CODE_FILES = (
    "scripts/stage_b_repair_runner.py",
    "src/dynamic_memory/llm/deepseek.py",
    "src/dynamic_memory/llm/execution.py",
    "src/dynamic_memory/llm/recording.py",
    "src/dynamic_memory/llm/structured_output.py",
    "src/dynamic_memory/memory/extraction.py",
    "src/dynamic_memory/memory/normalization.py",
    "src/dynamic_memory/memory/strict_evidence.py",
    "src/dynamic_memory/memory/review_workflow.py",
    "src/dynamic_memory/memory/checkpoints.py",
    "src/dynamic_memory/memory/batch_budget.py",
    "src/dynamic_memory/memory/artifact_store.py",
    "src/dynamic_memory/memory/repair_lineage.py",
)
_SKIPPABLE_EXTRACTION_ERRORS = frozenset({
    "SCHEMA_INVALID", "JSON_INTEGRITY",
    "SOURCE_OUT_OF_WINDOW", "RECORDED_AT_MISMATCH",
})
OLD_PILOT_WINDOW = "window_b122e288395635db8dc555dc"
OLD_PILOT_INPUT_SHA = "fa3796482b59f5214e19870d0741b83164b29506ab4bf48fead00c96e605b9a4"
MODEL_ROOT = Path(r"C:\Users\Keshu\Documents\SRE-Project-RuntimeAssets\models\bge-small-en-v1.5\5c38ec7c405ec4b44b94cc5a9bb96e735b38267a")
MODEL_REVISION = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"


class _NoNetworkClient:
    def __init__(self):
        self.chat = SimpleNamespace(completions=self)

    def create(self, **_kwargs):
        raise RuntimeError("DRY_RUN_CLIENT_CANNOT_SEND")


def _estimate(messages) -> int:
    return sum(len(message.content.encode("utf-8")) + 16 for message in messages)


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _code_fingerprint() -> str:
    return _fp([(path, _sha((ROOT / path).read_bytes())) for path in CODE_FILES])


@dataclass(frozen=True)
class BatchPlan:
    mode: str
    bundle: DatasetBundle
    repository: SourceRepository
    settings: AppSettings
    runtime: object
    sample_id: str
    windows: tuple[ExtractionWindow, ...]
    selected: tuple[ExtractionWindow, ...]
    plan_fingerprint: str
    run_fingerprint: str
    identities: dict
    call_fingerprints: dict
    live_ready: bool
    code_fingerprint: str

    @property
    def public(self) -> dict:
        return {"mode": self.mode, "sample_id": self.sample_id,
            "dataset_sha256": self.bundle.snapshot_sha256,
            "planned_windows": len(self.windows), "selected_windows": len(self.selected),
            "plan_fingerprint": self.plan_fingerprint,
            "run_fingerprint": self.run_fingerprint,
            "model_id": "deepseek-flash", "thinking": "enabled",
            "reasoning_effort": "high", "extractor_version": EXTRACTOR_VERSION,
            "prompt_fingerprint": MEMORY_DRAFT_PROMPT.template_fingerprint,
            "code_fingerprint": self.code_fingerprint,
            "planner_input_tokens": self.settings.extraction.window_max_input_tokens,
            "max_input_tokens_per_window": 934464,
            "server_default_output_tokens": 65536,
            "request_max_tokens_omitted": True,
            "provider_default_verified_on": PROVIDER_DEFAULT_VERIFIED_ON}


def prepare_plan(*, mode: str, bundle: DatasetBundle | None = None,
                 settings: AppSettings | None = None, sample_id: str | None = None,
                 fake_client=None, api_key: str | None = None) -> BatchPlan:
    if mode not in ("pilot", "batch", "repair4", "offline_test"):
        raise BudgetError("RUN_MODE_INVALID")
    if settings is None:
        settings = load_settings(models_profile=ROOT / "configs/models_high_pilot.yaml")
    models = settings.models
    if (models.default_backend != "mock" or models.api_enabled_by_default or
        models.model_id != "deepseek-flash" or models.thinking != "enabled" or
        models.reasoning_effort != "high" or models.max_retries != 0 or
        models.max_concurrent_api_requests != 1 or models.extraction_max_input_tokens != 934464 or
        models.extraction_max_output_tokens != 65536 or not models.extraction_omit_max_tokens or
        settings.extraction.window_max_input_tokens != 4096):
        raise BudgetError("RUN_PROFILE_UNSAFE")
    if bundle is None:
        raw = ROOT / "data/raw/locomo10.json"
        if _sha(raw.read_bytes()) != DATA_SHA:
            raise BudgetError("DATA_SNAPSHOT_MISMATCH")
        bundle = load_conversations(str(raw), str(ROOT / "configs/data_split.yaml"))
    if mode == "offline_test":
        if fake_client is None or not (sample_id or "").startswith("synthetic"):
            raise BudgetError("OFFLINE_TEST_SCOPE_INVALID")
    elif (fake_client is not None or bundle.snapshot_sha256 != DATA_SHA or
          sample_id not in (None, "conv-30")):
        raise BudgetError("REAL_SCOPE_INVALID")
    sample_id = sample_id or "conv-30"
    if bundle.split_by_sample_id.get(sample_id) != "main_dev":
        raise BudgetError("SPLIT_MISMATCH")
    repository = SourceRepository(bundle)
    sample = repository.get_sample(sample_id)
    windows = tuple(plan_extraction_windows(sample, settings.extraction,
                                             reserved_prompt_tokens=1800))
    if len(windows) != 63:
        raise BudgetError("WINDOW_PLAN_DRIFT")
    if mode == "pilot":
        chosen = tuple(window for window in windows if window.extraction_window_id == OLD_PILOT_WINDOW)
        if len(chosen) != 1 or _sha(chosen[0].input_text.encode()) != OLD_PILOT_INPUT_SHA:
            raise BudgetError("PILOT_WINDOW_DRIFT")
        selected = chosen
    elif mode == "repair4":
        selected = tuple(window for window in windows
                         if window.extraction_window_id in FAILED_WINDOW_IDS)
        if len(selected) != 4 or {w.extraction_window_id for w in selected} != FAILED_WINDOW_IDS:
            raise BudgetError("REPAIR_WINDOW_DRIFT")
    else:
        selected = windows
    runtime = create_runtime(settings, backend="deepseek", api_enabled=True,
        api_key=api_key, deepseek_client=fake_client or (_NoNetworkClient() if api_key is None else None),
        model_id="deepseek-flash", token_estimator=_estimate,
        estimator_version="utf8-bytes-plus16-v1", strict_budget=True,
        cache_enabled=False)
    git_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    plan_fingerprint = _fp([{"id": w.extraction_window_id,
        "refs": [r.model_dump(mode="json") for r in w.source_refs],
        "input_sha256": _sha(w.input_text.encode())} for w in windows])
    code_fingerprint = _code_fingerprint()
    run_fingerprint = _fp({"mode": mode, "sample": sample_id,
        "snapshot": bundle.snapshot_sha256,
        "source": _fp(sample.model_dump(mode="json")), "plan": plan_fingerprint,
        "config": runtime.config_fingerprint, "policy": runtime.execution_policy_fingerprint,
        "prompt": MEMORY_DRAFT_PROMPT.template_fingerprint,
        "extractor": EXTRACTOR_VERSION, "git_sha": git_sha,
        "code": code_fingerprint})
    identities = {}
    call_fingerprints = {}
    for window in selected:
        request = build_extraction_request(window, settings.models)
        if _estimate(request.messages) + 128 > request.settings.max_input_tokens:
            raise BudgetError("INPUT_BUDGET_EXCEEDED")
        if not request.settings.omit_max_tokens or request.settings.max_output_tokens != 65536:
            raise BudgetError("RUN_PROFILE_UNSAFE")
        prepared = runtime.make_request(MEMORY_DRAFT_PROMPT,
                                         {"conversation": window.input_text})
        if prepared.request != request:
            raise BudgetError("REQUEST_MISMATCH")
        identity = runtime.make_identity(prepared, sample_id=sample_id,
            scope_id=window.extraction_window_id, run_kind="development", method=None,
            data_fingerprint="sha256:" + _sha(window.input_text.encode()),
            schema_id="atomic_fact_draft", schema_version="1",
            retriever_fingerprint=None, query_analysis_version=None, git_sha=git_sha)
        identities[window.extraction_window_id] = identity
        call_fingerprints[window.extraction_window_id] = call_identity_fingerprint(identity)
        for ref in window.source_refs:
            repository.get_turn(sample_id, ref)
    return BatchPlan(mode, bundle, repository, settings, runtime, sample_id,
                     windows, selected, plan_fingerprint, run_fingerprint,
                     identities, call_fingerprints, api_key is not None or mode == "offline_test",
                     code_fingerprint)


def _ensure_private_state(path: Path) -> Path:
    root = path.resolve()
    if root.is_relative_to(ROOT.resolve()) or not root.is_relative_to(Path(tempfile.gettempdir()).resolve()):
        raise BudgetError("STATE_DIRECTORY_UNSAFE")
    return root


def _budget_root(plan: BatchPlan, state_root: Path, approval: BatchApproval) -> Path:
    # Real spending cannot be reset by changing --state-dir. The authorization
    # ID and approval fingerprint are bound by BatchBudgetLedger._load.
    if plan.mode == "offline_test":
        return state_root / "budget"
    base = _local_appdata()
    return base / "SRE-Project-BudgetLedgers" / approval.authorization_id


def _local_appdata() -> Path:
    """Resolve the Windows account's known folder, not a caller-supplied env var."""
    if os.name != "nt":
        raise BudgetError("WINDOWS_BUDGET_ROOT_REQUIRED")
    buffer = ctypes.create_unicode_buffer(32768)
    if ctypes.windll.shell32.SHGetFolderPathW(None, 0x001C, None, 0, buffer) != 0 or not buffer.value:
        raise BudgetError("BUDGET_ROOT_UNAVAILABLE")
    return Path(buffer.value).resolve()


def _registry_root(plan: BatchPlan, state_root: Path) -> Path:
    if plan.mode == "offline_test":
        return state_root / "authorization-registry"
    base = _local_appdata()
    return base / "SRE-Project-AuthorizationRegistry"


def _ensure_queue(queue: ReviewQueue, path: Path) -> None:
    if path.exists():
        try:
            saved = ReviewQueue.model_validate_json(path.read_bytes())
            if saved != queue:
                raise ValueError("queue drift")
        except Exception:
            raise ReviewError("REVIEW_QUEUE_STALE") from None
    else:
        export_review_queue(queue, path)


def _validate_plan(plan: BatchPlan) -> None:
    try:
        if _code_fingerprint() != plan.code_fingerprint or len(plan.windows) != 63:
            raise ValueError("code or count")
        current = _fp([{"id": w.extraction_window_id,
            "refs": [r.model_dump(mode="json") for r in w.source_refs],
            "input_sha256": _sha(w.input_text.encode())} for w in plan.windows])
        if current != plan.plan_fingerprint:
            raise ValueError("window plan")
        if plan.mode in ("batch", "offline_test") and plan.selected != plan.windows:
            raise ValueError("selected windows")
        if plan.mode == "repair4" and {w.extraction_window_id for w in plan.selected} != FAILED_WINDOW_IDS:
            raise ValueError("repair windows")
        if plan.mode == "pilot" and (len(plan.selected) != 1 or
            plan.selected[0].extraction_window_id != OLD_PILOT_WINDOW):
            raise ValueError("pilot window")
        for window in plan.selected:
            identity = plan.identities[window.extraction_window_id]
            if (identity.sample_id != plan.sample_id or
                identity.scope_id != window.extraction_window_id or
                identity.template_fingerprint != MEMORY_DRAFT_PROMPT.template_fingerprint or
                identity.config_fingerprint != plan.runtime.config_fingerprint or
                identity.execution_policy_fingerprint != plan.runtime.execution_policy_fingerprint or
                call_identity_fingerprint(identity) !=
                    plan.call_fingerprints[window.extraction_window_id]):
                raise ValueError("call identity")
    except (ValueError, TypeError, AttributeError, KeyError):
        raise BudgetError("RUN_PLAN_DRIFT") from None


def run_plan(plan: BatchPlan, *, state_dir: Path, approval: BatchApproval,
             approval_fingerprint: str, allow_new_calls: bool = False,
             stop_after: int | None = None,
             continue_past_failed: bool = False,
             review_file: Path | None = None,
             authority_file: Path | None = None, artifact_dir: Path | None = None,
             index_encoder=None) -> dict:
    """One locked pass. Only a separately approved caller may set allow_new_calls."""
    root = _ensure_private_state(state_dir)
    _validate_plan(plan)
    if plan.mode == "repair4":
        verify_original_batch(plan)
    if (approval.run_fingerprint != plan.run_fingerprint or
        approval.plan_fingerprint != plan.plan_fingerprint or
        approval.mode != plan.mode or approval.sample_id != plan.sample_id or
        (plan.mode == "repair4" and
         (approval.selected_window_ids != tuple(w.extraction_window_id for w in plan.selected)
          or approval.max_new_attempts != 4))):
        raise BudgetError("APPROVAL_INVALID_OR_EXPIRED")
    if allow_new_calls and plan.mode != "offline_test" and not plan.live_ready:
        raise BudgetError("CREDENTIAL_REQUIRED")
    if stop_after is not None and (type(stop_after) is not int or stop_after < 1):
        raise BudgetError("RUN_LIMIT_INVALID")
    if plan.mode == "repair4" and allow_new_calls and stop_after != 1:
        raise BudgetError("REPAIR_ONE_CALL_PER_PASS_REQUIRED")
    if type(continue_past_failed) is not bool:
        raise BudgetError("RUN_SKIP_POLICY_INVALID")
    if continue_past_failed and plan.mode in ("pilot", "repair4"):
        raise BudgetError("RUN_SKIP_POLICY_INVALID")
    ledger = BatchBudgetLedger(_budget_root(plan, root, approval), approval,
                               approval_fingerprint, _registry_root(plan, root))
    checkpoints = WindowCheckpointStore(root / "checkpoints",
        run_fingerprint=plan.run_fingerprint,
        max_new_attempts=approval.max_new_attempts if allow_new_calls else 0)
    results = {}
    new_calls = 0
    skipped_failed: list[str] = []
    with ledger.run_lock():
        for window in plan.selected:
            window_id = window.extraction_window_id
            call_fp = plan.call_fingerprints[window_id]
            result = checkpoints.load(window, call_fp)
            # A spent failed attempt is not a valid extraction checkpoint.
            # The opt-in skip policy can process LATER windows, but never
            # promote this incomplete batch to a review queue or artifact.
            spent = [row for row in ledger.window_rows() if row["window_id"] == window_id]
            if len(spent) > 1 or (spent and spent[0]["call_fingerprint"] != call_fp):
                raise BudgetError("BUDGET_CALL_IDENTITY_MISMATCH")
            if result is not None and (len(spent) != 1 or spent[0]["state"] != "extracted"):
                raise BudgetError("CHECKPOINT_LEDGER_MISMATCH")
            if result is None and spent:
                if (spent[0]["state"] == "failed" and continue_past_failed and
                    spent[0].get("error_code") in _SKIPPABLE_EXTRACTION_ERRORS):
                    skipped_failed.append(window_id)
                    continue
                if spent[0]["state"] == "extracted":
                    raise BudgetError("CHECKPOINT_MISSING_FOR_EXTRACTED")
                return {**plan.public, **ledger.status(),
                    "status": "BUDGET_ATTEMPT_SPENT",
                    "completed_windows": len(results),
                    "new_calls_this_run": new_calls, "network_calls": new_calls,
                    "published": False}
            if result is None:
                if not allow_new_calls:
                    return {**plan.public, **ledger.status(),
                        "status": "PENDING_AUTHORIZATION", "completed_windows": len(results),
                        "skipped_failed_windows": skipped_failed,
                        "new_calls_this_run": new_calls, "network_calls": new_calls,
                        "published": False}
                if stop_after is not None and new_calls >= stop_after:
                    return {**plan.public, **ledger.status(),
                        "status": "PAUSED", "completed_windows": len(results),
                        "skipped_failed_windows": skipped_failed,
                        "new_calls_this_run": new_calls, "network_calls": new_calls,
                        "published": False}
                request = build_extraction_request(window, plan.settings.models)
                try:
                    ledger.reserve(window_id=window_id, call_fingerprint=call_fp,
                        prompt_token_ceiling=934464,
                        completion_token_ceiling=65536,
                        input_bytes=sum(len(m.content.encode("utf-8")) for m in request.messages),
                        input_tokens_estimated=_estimate(request.messages))
                    checkpoints.claim_new_attempt(window, call_fp)
                except (BudgetError, CheckpointError) as exc:
                    return {**plan.public, **ledger.status(),
                        "status": getattr(exc, "code", "BUDGET_FAILED"),
                        "completed_windows": len(results), "new_calls_this_run": new_calls,
                        "network_calls": new_calls}
                try:
                    result = extract_window(window, plan.runtime.recorder,
                        plan.identities[window_id], plan.settings.models,
                        token_estimator=_estimate)
                except WindowExtractionError as exc:
                    events = plan.runtime.recorder.store.events()
                    summary = events[-1].get("summary", {}) if events else {}
                    ledger.record(call_fp, state="failed", error_code=exc.code,
                        safe_status_code=summary.get("safe_status_code"),
                        safe_reason=summary.get("safe_reason", "UNKNOWN"),
                        prompt_tokens=summary.get("prompt_tokens_known"),
                        completion_tokens=summary.get("completion_tokens_known"),
                        reasoning_tokens=summary.get("reasoning_tokens_known"),
                        prompt_cache_hit_tokens=summary.get("prompt_cache_hit_tokens_known"),
                        prompt_cache_miss_tokens=summary.get("prompt_cache_miss_tokens_known"),
                        finish_reason=summary.get("finish_reason"),
                        response_model_id=summary.get("response_model_id"),
                        http_response_received=summary.get("http_response_received", False),
                        elapsed_ms=events[-1].get("elapsed_ms") if events else None)
                    diagnostic_path = None
                    if exc.code == "SCHEMA_INVALID":
                        # Store only allowlisted schema paths and types, NEVER raw
                        # provider JSON, Pydantic input/context, or model thinking.
                        diagnostic_dir = root / "private_diagnostics"
                        diagnostic_dir.mkdir(parents=True, exist_ok=True)
                        diagnosis = {
                            "window_id": window_id, "error_code": exc.code,
                            "validation_error_count": exc.validation_error_count,
                            "validation_errors": list(exc.validation_errors),
                            "run_fingerprint": plan.run_fingerprint,
                            "call_fingerprint": call_fp,
                        }
                        path = diagnostic_dir / f"{window_id}.json"
                        with path.open("x", encoding="utf-8") as stream:
                            json.dump(diagnosis, stream, ensure_ascii=False, sort_keys=True)
                            stream.write("\n")
                            stream.flush()
                        diagnostic_path = str(path)
                    if continue_past_failed and exc.code in _SKIPPABLE_EXTRACTION_ERRORS:
                        skipped_failed.append(window_id)
                        new_calls += 1
                        continue
                    return {**plan.public, **ledger.status(),
                        "status": "FAILED", "error_code": exc.code,
                        "validation_error_count": exc.validation_error_count,
                        "validation_errors": list(exc.validation_errors),
                        "private_diagnostic_path": diagnostic_path,
                        "safe_status_code": summary.get("safe_status_code"),
                        "safe_reason": summary.get("safe_reason", "UNKNOWN"),
                        "finish_reason": summary.get("finish_reason"),
                        "http_response_received": summary.get("http_response_received", False),
                        "completed_windows": len(results),
                        "new_calls_this_run": new_calls + 1,
                        "network_calls": new_calls + (0 if exc.code == "SDK_MISSING" else 1)}
                checkpoints.save(window, result, call_fp)
                events = plan.runtime.recorder.store.events()
                summary = events[-1].get("summary", {}) if events else {}
                ledger.record(call_fp, state="extracted",
                    prompt_tokens=summary.get("prompt_tokens_known"),
                    completion_tokens=summary.get("completion_tokens_known"),
                    reasoning_tokens=summary.get("reasoning_tokens_known"),
                    prompt_cache_hit_tokens=summary.get("prompt_cache_hit_tokens_known"),
                    prompt_cache_miss_tokens=summary.get("prompt_cache_miss_tokens_known"),
                    finish_reason=summary.get("finish_reason"),
                    response_model_id=summary.get("response_model_id"),
                    http_response_received=summary.get("http_response_received", False),
                    elapsed_ms=events[-1].get("elapsed_ms") if events else None)
                new_calls += 1
            results[window_id] = result
        if skipped_failed:
            return {**plan.public, **ledger.status(),
                "status": "INCOMPLETE_SPENT_FAILURES",
                "skipped_failed_windows": skipped_failed,
                "completed_windows": len(results),
                "missing_windows": len(plan.selected) - len(results),
                "new_calls_this_run": new_calls, "network_calls": new_calls,
                "published": False}
        if len(results) != len(plan.selected):
            raise BudgetError("RUN_WINDOWS_INCOMPLETE")
        queue = make_review_queue(run_fingerprint=plan.run_fingerprint,
            sample_id=plan.sample_id, windows=plan.selected,
            call_fingerprints=plan.call_fingerprints, results=results,
            repository=plan.repository)
        queue_path = root / "private_review" / "queue.json"
        _ensure_queue(queue, queue_path)
        base = {**plan.public, **ledger.status(), "completed_windows": len(results),
                "new_calls_this_run": new_calls,
                "network_calls": new_calls,
                "queue_fingerprint": queue.fingerprint,
                "queue_path": str(queue_path)}
        if plan.mode == "pilot":
            if review_file is None or authority_file is None:
                return {**base, "status": "PILOT_PENDING_REVIEW", "published": False}
            if review_file.resolve().is_relative_to(root) or authority_file.resolve().is_relative_to(root):
                raise ReviewError("REVIEW_AUTHORITY_NOT_INDEPENDENT")
            verified = verify_review_files(queue, review_file, authority_file,
                                           run_kind="real")
            return {**base, "status": "PILOT_REVIEWED", "published": False,
                "review_fingerprint": verified.approval_fingerprint,
                "reviewed_facts": sum(len(w.facts) for w in queue.windows)}
        if plan.mode == "repair4":
            return {**base, "status": "REPAIR_PENDING_MERGE", "published": False}
        if review_file is None or authority_file is None:
            return {**base, "status": "PENDING_REVIEW", "published": False}
        if review_file.resolve().is_relative_to(root) or authority_file.resolve().is_relative_to(root):
            raise ReviewError("REVIEW_AUTHORITY_NOT_INDEPENDENT")
        verified = verify_review_files(queue, review_file, authority_file,
            run_kind="real" if plan.mode == "batch" else "offline_test")
        replay_only = WindowCheckpointStore(root / "checkpoints",
            run_fingerprint=plan.run_fingerprint, max_new_attempts=0)
        builder = MemoryBuilder(plan.repository, plan.runtime.recorder, plan.settings.models,
            plan.settings.extraction, lambda window: plan.identities[window.extraction_window_id],
            extractor_version=EXTRACTOR_VERSION, checkpoint_store=replay_only,
            window_evidence_checker=verified.checker_for_window)
        builder.window_fact_filter = verified.filter_for_window
        report = builder.build_memories_with_report(plan.repository.get_sample(plan.sample_id))
        if report.build_status != "complete" or report.total_windows != len(plan.windows):
            return {**base, "status": "REVIEW_OR_BUILD_INCOMPLETE", "published": False}
        receipt = ReviewAttestation(run_kind="real" if plan.mode == "batch" else "offline_test",
            reviewer_id=verified.approval.reviewer_id,
            execution_policy_fingerprint=plan.runtime.execution_policy_fingerprint,
            reviewed_memory_set_fingerprint=memory_set_fingerprint(report.memories),
            review_log_fingerprint=verified.approval_fingerprint,
            review_authority_fingerprint=_fp(verified.authority.model_dump(mode="json")),
            windows=tuple(WindowReview(extraction_window_id=d.extraction_window_id,
                source_refs_fingerprint=_fp([r.model_dump(mode="json") for r in d.source_refs]),
                approved_fact_count=d.accepted_fact_count,
                excluded_fact_count=d.rejected_fact_count, decision="approved")
                for d in report.window_diagnostics))
        ports = ArtifactPorts(artifact_dir or root / "artifacts", plan.bundle,
                              plan.settings, production=True)
        store = ports.store(plan.sample_id)
        try:
            existing = store.load()
        except ArtifactStoreError as exc:
            if exc.code != "BUILD_NOT_FOUND":
                raise
            existing = None
        if existing is not None:
            provenance = existing.manifest.production_provenance
            if (provenance is None or provenance.review_fingerprint != _fp(receipt.model_dump(mode="json"))
                    or existing.manifest.memory_set_fingerprint != memory_set_fingerprint(report.memories)):
                raise ReviewError("PUBLISHED_BUILD_CONFLICT")
            return {**base, "status": "ALREADY_PUBLISHED", "published": True,
                "build_id": existing.manifest.build_id,
                "source_manifest_fingerprint": existing.source_manifest_fingerprint,
                "memory_count": len(existing.memories)}
        artifact = store.publish_reviewed(report, receipt, verified_review=verified)
        ledger.mark_published()
        output = {**base, "status": "PUBLISHED", "published": True,
            "build_id": artifact.manifest.build_id,
            "source_manifest_fingerprint": artifact.source_manifest_fingerprint,
            "memory_count": len(artifact.memories)}
        if index_encoder is not None or plan.mode == "batch":
            encoder = index_encoder or LocalIndexEncoder(EmbeddingConfig(
                str(MODEL_ROOT), MODEL_REVISION,
                query_prompt="Represent this sentence for searching relevant passages: ",
                document_prompt=""))
            retrieval = plan.settings.retrieval.model_copy(update={
                "embedding_model": encoder.identity.model_name})
            index = IndexManager(encoder).build_index(artifact, retrieval)
            index_path = root / "indexes" / f"{plan.sample_id}.json"
            index_path.parent.mkdir(parents=True, exist_ok=True)
            save_index(index, index_path)
            loaded = IndexManager(encoder).load_index(index_path, artifact, retrieval)
            if loaded.index_fingerprint != index.index_fingerprint:
                raise BudgetError("INDEX_READBACK_FAILED")
            candidate_count = 0
            if artifact.memories:
                request = QueryRequest(query_id="b-repair-provenance-smoke",
                    sample_id=plan.sample_id,
                    question="What information is recorded in this conversation?")
                snapshot = VectorRetriever(encoder, retrieval).retrieve_with_trace(
                    request, analyze_query(request), loaded)
                valid_ids = {item.memory_id for item in artifact.memories}
                if any(item.memory_id not in valid_ids for item in snapshot.candidates.items):
                    raise BudgetError("INDEX_SOURCE_MISMATCH")
                candidate_count = len(snapshot.candidates.items)
            output.update(index_fingerprint=index.index_fingerprint,
                          encoder_fingerprint=encoder.identity.encoding_fingerprint,
                          index_path=str(index_path), candidate_count=candidate_count)
        return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["pilot", "batch", "repair4"], default="batch")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--confirm-paid-run", action="store_true")
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--approval-file", type=Path)
    parser.add_argument("--authorization-id")
    parser.add_argument("--review-file", type=Path)
    parser.add_argument("--authority-file", type=Path)
    parser.add_argument("--stop-after", type=int)
    parser.add_argument("--continue-past-failed", action="store_true",
                        help="Explicit opt-in: skip spent failed windows, never publish partial runs")
    args = parser.parse_args()
    try:
        plan = prepare_plan(mode=args.mode)
        if not args.live and not args.resume and not args.status:
            print(json.dumps({**plan.public, "status": "DRY_RUN",
                "key_present": bool(read_key()), "network_calls": 0}))
            return 0
        if args.state_dir is None:
            raise BudgetError("STATE_DIRECTORY_REQUIRED")
        root = _ensure_private_state(args.state_dir)
        if args.status and not args.live:
            attempts = root / "budget" / "attempts.json"
            checkpoint_count = len(list((root / "checkpoints").glob("window_*.json")))
            status = {**plan.public, "status": "STATUS_ONLY",
                "checkpoint_files": checkpoint_count,
                "budget_ledger_present": attempts.exists() if plan.mode == "offline_test" else None,
                "network_calls": 0}
            if args.approval_file is not None and args.authorization_id is not None:
                approval, approval_fp = load_approval(args.approval_file, mode=args.mode,
                    sample_id=plan.sample_id, dataset_sha256=plan.bundle.snapshot_sha256,
                    run_fingerprint=plan.run_fingerprint, plan_fingerprint=plan.plan_fingerprint,
                    authorization_id=args.authorization_id,
                    selected_window_id=OLD_PILOT_WINDOW if args.mode == "pilot" else None,
                    selected_window_ids=tuple(w.extraction_window_id for w in plan.selected)
                        if args.mode == "repair4" else None)
                budget_root = _budget_root(plan, root, approval)
                status["budget_ledger_present"] = (budget_root / "attempts.json").exists()
                status.update(BatchBudgetLedger(budget_root, approval,
                                                approval_fp, _registry_root(plan, root)).status())
            print(json.dumps(status))
            return 0
        if args.approval_file is None or args.authorization_id is None:
            raise BudgetError("APPROVAL_REQUIRED")
        if args.live and not args.confirm_paid_run:
            raise BudgetError("LIVE_CONFIRMATION_REQUIRED")
        if args.live and args.approval_file.resolve().is_relative_to(root):
            raise BudgetError("APPROVAL_NOT_INDEPENDENT")
        approval, approval_fp = load_approval(args.approval_file, mode=args.mode,
            sample_id=plan.sample_id, dataset_sha256=plan.bundle.snapshot_sha256,
            run_fingerprint=plan.run_fingerprint, plan_fingerprint=plan.plan_fingerprint,
            authorization_id=args.authorization_id,
            selected_window_id=OLD_PILOT_WINDOW if args.mode == "pilot" else None,
            selected_window_ids=tuple(w.extraction_window_id for w in plan.selected)
                if args.mode == "repair4" else None)
        if args.live:
            key = read_key()
            if not key:
                raise BudgetError("CREDENTIAL_REQUIRED")
            # Construct the optional SDK before any paid attempt is reserved.
            # This never sends HTTP; a missing/broken local dependency must
            # fail without spending an authorization slot.
            try:
                sdk_probe = _new_sdk_client(key, plan.settings.models.request_timeout_seconds)
                sdk_probe.close()
            except DeepSeekAdapterError as exc:
                raise BudgetError("SDK_PREFLIGHT_" + exc.code) from None
            plan = prepare_plan(mode=args.mode, api_key=key)
        result = run_plan(plan, state_dir=root, approval=approval,
            approval_fingerprint=approval_fp, allow_new_calls=args.live,
            stop_after=1 if args.live and args.mode == "repair4" and
                args.stop_after is None else args.stop_after,
            continue_past_failed=args.continue_past_failed,
            review_file=args.review_file,
            authority_file=args.authority_file)
        if args.live:
            ledger = BatchBudgetLedger(_budget_root(plan, root, approval), approval,
                                       approval_fp, _registry_root(plan, root))
            rows = ledger.window_rows()
            (root / "window_usage.json").write_text(
                json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["status"] in {"DRY_RUN", "STATUS_ONLY", "PUBLISHED", "ALREADY_PUBLISHED",
                                          "PILOT_PENDING_REVIEW", "PILOT_REVIEWED", "PENDING_REVIEW",
                                          "REPAIR_PENDING_MERGE",
                                          "PENDING_AUTHORIZATION", "PAUSED"} else 2
    except (BudgetError, ReviewError, CheckpointError, ArtifactStoreError, LineageError) as exc:
        current_plan = locals().get("plan")
        events = current_plan.runtime.recorder.store.events() if current_plan is not None else []
        calls = sum(event.get("summary", {}).get("attempt_count", 0) for event in events)
        print(json.dumps({"status": exc.code, "network_calls": calls}))
        return 2
    except Exception:
        current_plan = locals().get("plan")
        events = current_plan.runtime.recorder.store.events() if current_plan is not None else []
        calls = sum(event.get("summary", {}).get("attempt_count", 0) for event in events)
        print(json.dumps({"status": "UNEXPECTED_SANITIZED", "network_calls": calls}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
