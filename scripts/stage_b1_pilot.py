"""One frozen LoCoMo window through the real B extraction contract; dry-run by default."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from dynamic_memory.data.loader import load_conversations
from dynamic_memory.data.repository import SourceRepository
from dynamic_memory.llm.recording import call_cache_key
from dynamic_memory.memory.extraction import MEMORY_DRAFT_PROMPT, WindowExtractionError, build_extraction_request, extract_window
from dynamic_memory.memory.normalization import MemoryNormalizationError, normalize_memories
from dynamic_memory.memory.strict_evidence import strict_evidence_checker
from dynamic_memory.memory.windows import plan_extraction_windows
from dynamic_memory.pipeline.runtime import create_runtime
from dynamic_memory.pipeline.settings import load_settings
from scripts.stage_a_deepseek_smoke import lock_file, read_key, unlock_file

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = Path(tempfile.gettempdir()) / "SRE_STAGE_B_20261103_d3978804c53c"
LEDGER = EVIDENCE / "b1_api_attempts.json"
LOCK = EVIDENCE / "b1_run.lock"
DATA_SHA = "79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4"
EXTRACTOR_VERSION = "b1-deepseek-flash-high-pilot-v1"


def used() -> int:
    try:
        value = json.loads(LEDGER.read_text(encoding="utf-8"))["attempts"]
        if type(value) is not int or value not in (0, 1):
            raise ValueError
        return value
    except FileNotFoundError:
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        raise RuntimeError("LEDGER_INVALID") from None


def claim() -> None:
    if used() >= 1:
        raise RuntimeError("BUDGET_EXHAUSTED")
    with LEDGER.open("x", encoding="utf-8") as stream:
        json.dump({"attempts": 1}, stream)
        stream.flush()
        os.fsync(stream.fileno())


def prepare():
    raw = ROOT / "data" / "raw" / "locomo10.json"
    if hashlib.sha256(raw.read_bytes()).hexdigest() != DATA_SHA:
        raise RuntimeError("DATA_SNAPSHOT_MISMATCH")
    settings = load_settings(models_profile=ROOT / "configs" / "models_high_pilot.yaml")
    if (settings.models.model_id != "deepseek-flash" or settings.models.thinking != "enabled" or
        settings.models.reasoning_effort != "high" or settings.models.max_retries != 0 or
        settings.models.max_concurrent_api_requests != 1 or
        settings.models.extraction_max_output_tokens != 2048 or
        settings.models.default_backend != "mock" or settings.models.api_enabled_by_default):
        raise RuntimeError("UNSAFE_PROFILE")
    bundle = load_conversations(str(raw), str(ROOT / "configs" / "data_split.yaml"))
    if bundle.snapshot_sha256 != DATA_SHA:
        raise RuntimeError("DATA_SNAPSHOT_MISMATCH")
    repository = SourceRepository(bundle)
    sample = repository.get_sample("conv-30")
    windows = plan_extraction_windows(sample, settings.extraction, reserved_prompt_tokens=1800)
    # Selection depends only on source order/shape, never on Gold or model output.
    eligible = [window for window in windows if 1 <= len(window.turns) <= 3 and
                all(turn.visible_text.strip() and "[image_caption:" not in turn.visible_text
                    for turn in window.turns)]
    if not eligible:
        raise RuntimeError("NO_ELIGIBLE_WINDOW")
    window = min(eligible, key=lambda item: (len(item.input_text.encode("utf-8")),
                                             windows.index(item)))
    request = build_extraction_request(window, settings.models)
    estimate = sum(len(message.content.encode("utf-8")) + 16 for message in request.messages)
    if estimate + 128 > request.settings.max_input_tokens:
        raise RuntimeError("INPUT_BUDGET_EXCEEDED")
    for ref in window.source_refs:
        repository.get_turn(sample.sample_id, ref)
    return settings, repository, window, len(windows), request, estimate


def metadata(window, count, request, estimate) -> dict:
    refs = [ref.model_dump() for ref in window.source_refs]
    digest = hashlib.sha256(json.dumps(refs, sort_keys=True).encode()).hexdigest()
    return {"sample_id": window.sample_id, "planned_full_sample_windows": count,
            "selection_rule": "minimum UTF-8 input bytes among 1-3-turn caption-free windows; source-order tie",
            "window_id": window.extraction_window_id, "source_ref_count": len(refs),
            "source_refs_sha256": digest, "request_input_estimate": estimate,
            "max_input_tokens": request.settings.max_input_tokens,
            "max_output_tokens": request.settings.max_output_tokens,
            "model_id": request.model_id, "thinking": request.settings.thinking,
            "reasoning_effort": request.settings.reasoning_effort,
            "prompt_fingerprint": MEMORY_DRAFT_PROMPT.template_fingerprint,
            "window_input_sha256": hashlib.sha256(window.input_text.encode()).hexdigest(),
            "gold_fields_present": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--confirm-low-cost", action="store_true")
    args = parser.parse_args(argv)
    settings, repository, window, count, request, estimate = prepare()
    meta = metadata(window, count, request, estimate)
    if not (args.live and args.confirm_low_cost):
        print(json.dumps({"mode": "dry-run", "key_present": bool(read_key()),
                          "network_calls": 0, "attempts_used": used(), **meta}))
        return 0
    if used() >= 1:
        print(json.dumps({"status": "BUDGET_EXHAUSTED", "network_calls": 0, **meta}))
        return 2
    key = read_key()
    if not key:
        print(json.dumps({"status": "KEY_MISSING", "network_calls": 0, **meta}))
        return 2
    # At worst 4,096 prompt + 2,048 completion tokens at peak cache-miss rates.
    worst_usd = 4096 * 0.30 / 1_000_000 + 2048 * 1.20 / 1_000_000
    if worst_usd > 0.02:
        print(json.dumps({"status": "COST_LIMIT", "network_calls": 0, **meta}))
        return 2
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+b") as lock:
        lock.seek(0)
        if lock.read(1) == b"":
            lock.seek(0)
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            lock_file(lock)
        except OSError:
            print(json.dumps({"status": "CONCURRENT_RUN_REJECTED", "network_calls": 0}))
            return 2
        try:
            if used() >= 1:
                return 2
            # Construct and validate the exact A Runtime identity before spending the one slot.
            estimator = lambda messages: sum(len(message.content.encode("utf-8")) + 16 for message in messages)
            runtime = create_runtime(settings, backend="deepseek", api_enabled=True,
                                     api_key=key, token_estimator=estimator,
                                     estimator_version="utf8-bytes-plus16-v1", strict_budget=True,
                                     cache_enabled=False)
            prepared = runtime.make_request(MEMORY_DRAFT_PROMPT, {"conversation": window.input_text})
            if prepared.request != request:
                raise RuntimeError("REQUEST_MISMATCH")
            git_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
            data_fp = "sha256:" + meta["window_input_sha256"]
            identity = runtime.make_identity(prepared, sample_id=window.sample_id,
                scope_id=window.extraction_window_id, run_kind="development", method=None,
                data_fingerprint=data_fp, schema_id="atomic_fact_draft", schema_version="1",
                retriever_fingerprint=None, query_analysis_version=None, git_sha=git_sha)
            meta["config_fingerprint"] = runtime.config_fingerprint
            meta["execution_policy_fingerprint"] = runtime.execution_policy_fingerprint
            meta["request_cache_key"] = call_cache_key(request, identity)
            meta["extractor_version"] = EXTRACTOR_VERSION
            claim()
            started = time.monotonic()
            status = "UNKNOWN"
            review_code = None
            fact_count = None
            memory_count = None
            try:
                result = extract_window(window, runtime.recorder, identity, settings.models,
                                        token_estimator=estimator)
                fact_count = len(result.facts)
                try:
                    memories = normalize_memories(window.sample_id, [result], repository,
                        extractor_version=EXTRACTOR_VERSION,
                        evidence_checker=strict_evidence_checker)
                    memory_count = len(memories)
                    status = "PILOT_REVIEWED" if memories else "PILOT_ZERO_FACTS"
                except MemoryNormalizationError as exc:
                    status = "PILOT_REVIEW_PENDING"
                    review_code = exc.code
                    # This owner-local review file is never put in Git or the public report.
                    queue = {"sample_id": window.sample_id, "window_id": window.extraction_window_id,
                             "drafts": [item.model_dump(mode="json") for item in result.facts],
                             "cited_turns": [repository.get_turn(window.sample_id, ref).model_dump(mode="json")
                                             for item in result.facts for ref in item.source_refs]}
                    private = EVIDENCE / "private_review"
                    private.mkdir(exist_ok=True)
                    (private / "pilot_review.json").write_text(json.dumps(queue, ensure_ascii=False), encoding="utf-8")
            except WindowExtractionError as exc:
                status = "EXTRACTION_FAILED"
                review_code = exc.code
            events = runtime.recorder.store.events()
            summary = events[-1]["summary"] if events else None
            record = {**meta, "status": status, "review_code": review_code,
                      "fact_count": fact_count, "reviewed_memory_count": memory_count,
                      "elapsed_seconds": round(time.monotonic() - started, 3),
                      "usage_summary": summary, "attempts_used": used(),
                      "full_artifact_published": False}
            (EVIDENCE / "b1_pilot_result.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
            print(json.dumps(record))
            return 0 if status in ("PILOT_REVIEWED", "PILOT_ZERO_FACTS") else 2
        finally:
            lock.seek(0)
            unlock_file(lock)


if __name__ == "__main__":
    raise SystemExit(main())
