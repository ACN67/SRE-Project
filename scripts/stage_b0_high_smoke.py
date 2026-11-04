"""At most two opt-in synthetic DeepSeek-Flash High Chat Completions calls."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

from dynamic_memory.llm.deepseek import DeepSeekLLMAdapter
from dynamic_memory.llm.execution import GuardedLLMAdapter, LLMExecutionError
from dynamic_memory.llm.protocol import GenerationSettings, LLMMessage, LLMRequest
from dynamic_memory.pipeline.settings import load_settings
from scripts.stage_a_deepseek_smoke import lock_file, read_key, unlock_file

EVIDENCE = Path(tempfile.gettempdir()) / "SRE_HIGH_BC_PREP_20261103_241dccfee3b3"
LEDGER = EVIDENCE / "high_api_attempts.json"
LOCK = EVIDENCE / "high_api.lock"
LIMIT = 2
MODEL = "deepseek-flash"
MAX_TOKENS = 2048


def used() -> int:
    try:
        count = json.loads(LEDGER.read_text(encoding="utf-8"))["attempts"]
        if type(count) is not int or not 0 <= count <= LIMIT:
            raise ValueError
        return count
    except FileNotFoundError:
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        raise RuntimeError("LEDGER_INVALID") from None


def claim() -> int:
    count = used()
    if count >= LIMIT:
        raise RuntimeError("BUDGET_EXHAUSTED")
    with LEDGER.open("x", encoding="utf-8") if count == 0 else LEDGER.open("w", encoding="utf-8") as stream:
        json.dump({"attempts": count + 1}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    return count + 1


def request(kind: str) -> LLMRequest:
    if kind == "text":
        prompt = "Reply with OK only."
        task, mode = "qa", "text"
    else:
        prompt = 'Return only this json object: {"ok": true, "label": "synthetic"}.'
        task, mode = "memory_extraction", "json"
    return LLMRequest(messages=(LLMMessage(role="user", content=prompt),),
                      task=task, output_mode=mode, model_id=MODEL,
                      settings=GenerationSettings(max_input_tokens=200,
                                                  max_output_tokens=MAX_TOKENS,
                                                  temperature=0, thinking="enabled",
                                                  reasoning_effort="high"))


def probe(guard: GuardedLLMAdapter, kind: str) -> dict:
    number = claim()  # Failed requests consume their slot too.
    start = time.monotonic()
    try:
        result = guard.execute(request(kind))
        response = result.response
        valid = bool(response.raw_output.strip()) if kind == "text" else (
            response.parsed_json == {"ok": True, "label": "synthetic"})
        return {"attempt": number, "kind": kind, "status": "OK" if valid else "CONTENT_MISMATCH",
                "model_id": response.model_id, "thinking": "enabled", "reasoning_effort": "high",
                "finish_reason": response.finish_reason,
                "usage": response.usage.model_dump() if response.usage else None,
                "elapsed_seconds": round(time.monotonic() - start, 3),
                "output_sha256": hashlib.sha256(response.raw_output.encode()).hexdigest(),
                "valid": valid, "guard_attempts": result.summary.attempt_count}
    except LLMExecutionError as exc:
        return {"attempt": number, "kind": kind, "status": exc.code,
                "elapsed_seconds": round(time.monotonic() - start, 3),
                "guard_attempts": exc.summary.attempt_count}
    except Exception:
        return {"attempt": number, "kind": kind, "status": "UNEXPECTED_SANITIZED",
                "elapsed_seconds": round(time.monotonic() - start, 3)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--confirm-low-cost", action="store_true")
    args = parser.parse_args(argv)
    if not (args.live and args.confirm_low_cost):
        print(json.dumps({"mode": "dry-run", "key_present": bool(read_key()),
                          "attempts_used": used(), "network_calls": 0}))
        return 0
    if used() >= LIMIT:
        print(json.dumps({"status": "BUDGET_EXHAUSTED", "network_calls": 0}))
        return 2
    key = read_key()
    if not key:
        print(json.dumps({"status": "KEY_MISSING", "network_calls": 0}))
        return 2
    try:
        import openai  # noqa: F401
    except ImportError:
        print(json.dumps({"status": "SDK_MISSING", "network_calls": 0}))
        return 2
    root = Path(__file__).resolve().parents[1]
    settings = load_settings(models_profile=root / "configs" / "models_high_pilot.yaml")
    policy = settings.models
    if (policy.default_backend != "mock" or policy.api_enabled_by_default or
        policy.model_id != MODEL or policy.thinking != "enabled" or
        policy.reasoning_effort != "high" or policy.max_retries != 0 or
        policy.max_concurrent_api_requests != 1 or MAX_TOKENS > 4096):
        print(json.dumps({"status": "UNSAFE_PROFILE", "network_calls": 0}))
        return 2
    # Even two separate process launches cannot send probes concurrently.
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
            adapter = DeepSeekLLMAdapter(api_enabled=True, api_key=key, model_id=MODEL,
                                         request_timeout_seconds=policy.request_timeout_seconds)
            guard = GuardedLLMAdapter(adapter, policy)
            results = []
            for kind in ("text", "json"):
                if used() >= LIMIT:
                    break
                row = probe(guard, kind)
                results.append(row)
                if row["status"] != "OK":
                    break
            (EVIDENCE / "high_api_results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
            print(json.dumps({"key_present": True, "attempts_used": used(), "results": results}))
            return 0 if len(results) == 2 and all(row["valid"] for row in results) else 2
        finally:
            lock.seek(0)
            unlock_file(lock)


if __name__ == "__main__":
    raise SystemExit(main())
