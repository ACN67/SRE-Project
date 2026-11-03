"""Two-attempt, opt-in synthetic DeepSeek probe. Never prints credentials or content."""

from __future__ import annotations

import argparse
import hashlib
import json
import msvcrt
import os
import tempfile
import time
import winreg
from pathlib import Path

from dynamic_memory.llm.deepseek import DeepSeekLLMAdapter
from dynamic_memory.llm.execution import GuardedLLMAdapter, LLMExecutionError
from dynamic_memory.llm.protocol import GenerationSettings, LLMMessage, LLMRequest
from dynamic_memory.pipeline.settings import load_settings

MODEL = "deepseek-flash"
EVIDENCE = Path(tempfile.gettempdir()) / "SRE_STAGE_A_20261103_d52fdb512b4641959e65e783d1c8d04d"
LEDGER = EVIDENCE / "api_attempts.json"
LOCK = EVIDENCE / "api_attempts.lock"
LIMIT = 2


def read_key() -> str | None:
    value = os.environ.get("DEEPSEEK_API_KEY")
    if value and value.strip():
        return value
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
            value, _ = winreg.QueryValueEx(handle, "DEEPSEEK_API_KEY")
    except OSError:
        return None
    return value if isinstance(value, str) and value.strip() else None


def attempts_used() -> int:
    try:
        state = json.loads(LEDGER.read_text(encoding="utf-8"))
        count = state["attempts"]
        if type(count) is not int or count < 0 or count > LIMIT:
            raise ValueError
        return count
    except FileNotFoundError:
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        raise RuntimeError("LEDGER_INVALID") from None


def claim_attempt() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+b") as lock:
        lock.seek(0)
        if lock.read(1) == b"":
            lock.seek(0)
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError("BUDGET_BUSY") from None
        try:
            used = attempts_used()
            if used >= LIMIT:
                raise RuntimeError("BUDGET_EXHAUSTED")
            # Persist the claim before entering SDK code; a failed request still consumes it.
            with LEDGER.open("x", encoding="utf-8") if used == 0 else LEDGER.open("w", encoding="utf-8") as stream:
                json.dump({"attempts": used + 1}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            return used + 1
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def make_request(kind: str) -> LLMRequest:
    if kind == "text":
        content = "Reply with OK only."
        task, mode, limit = "qa", "text", 128
    else:
        content = 'Return only a json object with this exact shape: {"ok": true, "label": "synthetic"}.'
        task, mode, limit = "memory_extraction", "json", 256
    return LLMRequest(
        messages=(LLMMessage(role="user", content=content),), task=task,
        output_mode=mode, model_id=MODEL,
        settings=GenerationSettings(max_input_tokens=200, max_output_tokens=limit,
                                    temperature=0, thinking="disabled"),
    )


def run_probe(guard: GuardedLLMAdapter, kind: str) -> dict:
    number = claim_attempt()
    start = time.monotonic()
    try:
        result = guard.execute(make_request(kind))
        response = result.response
        valid = response.raw_output.strip() == "OK" if kind == "text" else (
            response.parsed_json == {"ok": True, "label": "synthetic"})
        return {"attempt": number, "kind": kind, "status": "OK" if valid else "CONTENT_MISMATCH",
                "elapsed_seconds": round(time.monotonic() - start, 3),
                "model_id": response.model_id, "finish_reason": response.finish_reason,
                "usage": response.usage.model_dump() if response.usage else None,
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
    key = read_key()
    if not (args.live and args.confirm_low_cost):
        print(json.dumps({"mode": "dry-run", "key_present": bool(key),
                          "attempts_used": attempts_used(), "network_calls": 0}))
        return 0
    if not key:
        print(json.dumps({"status": "KEY_MISSING", "key_present": False, "network_calls": 0}))
        return 2
    if attempts_used() >= LIMIT:
        print(json.dumps({"status": "BUDGET_EXHAUSTED", "network_calls": 0}))
        return 2
    try:
        import openai  # noqa: F401
    except ImportError:
        print(json.dumps({"status": "SDK_MISSING", "network_calls": 0}))
        return 2
    settings = load_settings()
    if settings.models.default_backend != "mock" or settings.models.api_enabled_by_default:
        print(json.dumps({"status": "UNSAFE_DEFAULTS", "network_calls": 0}))
        return 2
    policy = settings.models.model_copy(update={"max_retries": 0,
                                                "max_concurrent_api_requests": 1,
                                                "request_timeout_seconds": 20})
    adapter = DeepSeekLLMAdapter(api_enabled=True, api_key=key, model_id=MODEL,
                                 request_timeout_seconds=20)
    guard = GuardedLLMAdapter(adapter, policy)
    results = []
    for kind in ("text", "json"):
        if attempts_used() >= LIMIT:
            break
        result = run_probe(guard, kind)
        results.append(result)
        if result["status"] != "OK":
            break
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "api_results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps({"key_present": True, "attempts_used": attempts_used(), "results": results}))
    return 0 if len(results) == 2 and all(row["valid"] for row in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
