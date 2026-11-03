"""Durable paid-attempt reservations; an external approval file is mandatory."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

_FP = r"^sha256:[0-9a-f]{64}$"
_OFFICIAL_SHA = "79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4"
_DEFAULT_VERIFIED_ON = date(2026, 11, 3)
# Old paid approvals are historical evidence of consumed requests. Their global
# ledgers were not visible during independent pre-batch inspection; never infer
# that missing files make these exact one-time authorizations reusable.
_RETIRED_AUTHORIZATION_IDS = frozenset({
    "user-chat-20261103-pilot01",
    "user-chat-20261103-final-pilot01",
})


class BudgetError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(f"{code}: batch budget unavailable")


def _bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _fp(value: object) -> str:
    return "sha256:" + hashlib.sha256(_bytes(value)).hexdigest()


@contextmanager
def _lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if not stream.seek(0, os.SEEK_END):
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise BudgetError("BUDGET_CONCURRENT") from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class BatchApproval(BaseModel):
    """A human-supplied, one-batch approval; this module never creates one."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)
    format_version: Literal[2] = 2
    authorization_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{7,95}$")
    approved_by: str = Field(min_length=1)
    mode: Literal["pilot", "batch", "repair4", "offline_test"]
    sample_id: str = Field(min_length=1)
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_fingerprint: str = Field(pattern=_FP)
    plan_fingerprint: str = Field(pattern=_FP)
    selected_window_id: str | None = None
    selected_window_ids: tuple[str, ...] | None = None
    model_id: Literal["deepseek-flash"] = "deepseek-flash"
    thinking: Literal["enabled"] = "enabled"
    reasoning_effort: Literal["high"] = "high"
    output_policy: Literal["provider_default_64k"]
    context_tokens: Literal[1000000] = 1000000
    provider_default_completion_tokens: Literal[65536] = 65536
    max_new_attempts: int = Field(ge=1, le=63)
    max_prompt_tokens_total: int = Field(gt=0)
    max_completion_tokens_total: int = Field(gt=0)
    max_usd: str = Field(pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,8})?$")
    input_usd_per_million: str = Field(pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,8})?$")
    output_usd_per_million: str = Field(pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,8})?$")
    price_checked_on: date
    provider_default_checked_on: date
    expires_at: datetime


def load_approval(path: Path, *, mode: str, sample_id: str,
                  dataset_sha256: str, run_fingerprint: str,
                  plan_fingerprint: str, authorization_id: str,
                  selected_window_id: str | None = None,
                  selected_window_ids: tuple[str, ...] | None = None,
                  now: datetime | None = None) -> tuple[BatchApproval, str]:
    """Validate a separately supplied approval against exact frozen run identity."""
    try:
        raw = path.read_bytes()
        approval = BatchApproval.model_validate_json(raw)
        instant = now or datetime.now(timezone.utc)
        if (approval.mode != mode or approval.sample_id != sample_id or
            approval.dataset_sha256 != dataset_sha256 or
            approval.run_fingerprint != run_fingerprint or
            approval.plan_fingerprint != plan_fingerprint or
            approval.authorization_id != authorization_id or
            approval.selected_window_id != selected_window_id or
            approval.selected_window_ids != selected_window_ids or
            approval.expires_at.tzinfo is None or instant >= approval.expires_at or
            approval.price_checked_on != instant.date() or
            approval.provider_default_checked_on != instant.date() or
            (mode != "offline_test" and
             approval.provider_default_checked_on < _DEFAULT_VERIFIED_ON) or
            approval.input_usd_per_million != "0.30" or
            approval.output_usd_per_million != "1.20"):
            raise ValueError("approval drift")
        count = 1 if mode == "pilot" else 4 if mode == "repair4" else 63
        if (approval.max_prompt_tokens_total != count * 934464 or
            approval.max_completion_tokens_total != count * 65536 or
            Decimal(approval.max_usd) <
            (Decimal(approval.max_prompt_tokens_total) * Decimal("0.30") +
             Decimal(approval.max_completion_tokens_total) * Decimal("1.20")) / 1_000_000):
            raise ValueError("default-output reserve mismatch")
        if mode == "pilot" and (approval.max_new_attempts != 1 or
            approval.selected_window_id is None):
            raise ValueError("pilot budget")
        if mode == "batch" and (sample_id != "conv-30" or
            dataset_sha256 != _OFFICIAL_SHA or approval.max_new_attempts > 63):
            raise ValueError("batch scope")
        if mode == "repair4" and (sample_id != "conv-30" or
            dataset_sha256 != _OFFICIAL_SHA or approval.max_new_attempts != 4 or
            selected_window_ids is None or len(selected_window_ids) != 4 or
            len(set(selected_window_ids)) != 4):
            raise ValueError("repair scope")
        if mode == "offline_test" and not sample_id.startswith("synthetic"):
            raise ValueError("fake scope")
        return approval, "sha256:" + hashlib.sha256(raw).hexdigest()
    except (OSError, ValidationError, ValueError, TypeError, InvalidOperation, AttributeError):
        raise BudgetError("APPROVAL_INVALID_OR_EXPIRED") from None


class BatchBudgetLedger:
    """No refund of unknown or failed attempts. Run lock prevents parallel callers."""

    def __init__(self, root: Path, approval: BatchApproval, approval_fingerprint: str,
                 registry_root: Path | None = None):
        if (not isinstance(root, Path) or not isinstance(approval, BatchApproval) or
                not re.fullmatch(_FP, approval_fingerprint)):
            raise BudgetError("BUDGET_IDENTITY_INVALID")
        self.root = root
        self.approval = approval
        self.approval_fingerprint = approval_fingerprint
        self.registry_root = registry_root or root.parent / "authorization-registry"
        self.marker = self.registry_root / (approval.authorization_id + ".json")
        root.mkdir(parents=True, exist_ok=True)

    def enroll(self) -> None:
        """Explicit, zero-network one-time enrollment AFTER separate user approval.

        The production runner NEVER invokes this method. In particular, a lost
        ledger + registry must not silently become a new empty authorization.
        """
        if self.approval.mode == "offline_test":
            raise BudgetError("BUDGET_ENROLLMENT_NOT_REQUIRED")
        if self.approval.authorization_id in _RETIRED_AUTHORIZATION_IDS:
            raise BudgetError("BUDGET_PREVIOUSLY_CONSUMED")
        instant = datetime.now(timezone.utc)
        if (self.approval.expires_at.tzinfo is None or
            instant >= self.approval.expires_at or
            self.approval.price_checked_on != instant.date() or
            self.approval.provider_default_checked_on != instant.date() or
            self.approval.provider_default_checked_on < _DEFAULT_VERIFIED_ON):
            raise BudgetError("BUDGET_ENROLLMENT_EXPIRED")
        self.registry_root.mkdir(parents=True, exist_ok=True)
        with _lock(self.registry_root / "enrollment.lock"):
            if self.marker.exists() or (self.root / "attempts.json").exists():
                raise BudgetError("BUDGET_ALREADY_ENROLLED")
            payload = _bytes({"authorization_id": self.approval.authorization_id,
                "approval_fingerprint": self.approval_fingerprint,
                "run_fingerprint": self.approval.run_fingerprint,
                "attempt_count": 0})
            try:
                with self.marker.open("xb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError:
                raise BudgetError("BUDGET_ENROLLMENT_FAILED") from None

    def run_lock(self):
        return _lock(self.root / "run.lock")

    def _load(self):
        path = self.root / "attempts.json"
        if self.approval.mode != "offline_test" and (
            self.approval.authorization_id in _RETIRED_AUTHORIZATION_IDS):
            raise BudgetError("BUDGET_PREVIOUSLY_CONSUMED")
        if not path.exists() and not self.marker.exists():
            if self.approval.mode == "offline_test":
                return []
            raise BudgetError("BUDGET_ENROLLMENT_REQUIRED")
        try:
            marker = json.loads(self.marker.read_bytes())
            if not path.exists():
                # The sole legitimate empty paid ledger is an independently
                # pre-enrolled (count=0) authority. A missing spent ledger
                # cannot be mistaken for a fresh authorization.
                if self.approval.mode != "offline_test" and marker == {
                    "authorization_id": self.approval.authorization_id,
                    "approval_fingerprint": self.approval_fingerprint,
                    "run_fingerprint": self.approval.run_fingerprint,
                    "attempt_count": 0,
                }:
                    return []
                raise ValueError("missing spent ledger or stale marker")
            record = json.loads(path.read_bytes())
            attempts = record["attempts"]
            if (marker != {"authorization_id": self.approval.authorization_id,
                           "approval_fingerprint": self.approval_fingerprint,
                           "run_fingerprint": self.approval.run_fingerprint,
                           "attempt_count": len(attempts)} or
                record["format_version"] != 2 or
                record["approval_fingerprint"] != self.approval_fingerprint or
                record["run_fingerprint"] != self.approval.run_fingerprint or
                record["digest"] != _fp(attempts) or not isinstance(attempts, list) or
                len({item["call_fingerprint"] for item in attempts}) != len(attempts)):
                raise ValueError("ledger drift")
            return attempts
        except (OSError, ValueError, TypeError, KeyError):
            raise BudgetError("BUDGET_CORRUPT_OR_STALE") from None

    def _mark_count(self, count: int) -> None:
        self.registry_root.mkdir(parents=True, exist_ok=True)
        payload = _bytes({"authorization_id": self.approval.authorization_id,
                          "approval_fingerprint": self.approval_fingerprint,
                          "run_fingerprint": self.approval.run_fingerprint,
                          "attempt_count": count})
        if count == 1 and self.approval.mode == "offline_test":
            try:
                with self.marker.open("xb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError:
                raise BudgetError("BUDGET_CORRUPT_OR_STALE") from None
        else:
            # Paid authorization was already independently enrolled as count=0.
            # The first attempt upgrades that marker to 1 *before* writing
            # attempts.json; interrupted writes remain fail-closed.
            fd, temporary = tempfile.mkstemp(prefix=".registry-", dir=self.registry_root)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.marker)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    def _write(self, attempts):
        payload = _bytes({"format_version": 2,
            "approval_fingerprint": self.approval_fingerprint,
            "run_fingerprint": self.approval.run_fingerprint,
            "attempts": attempts, "digest": _fp(attempts)})
        fd, temporary = tempfile.mkstemp(prefix=".attempts-", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.root / "attempts.json")
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def reserve(self, *, window_id: str, call_fingerprint: str,
                prompt_token_ceiling: int, completion_token_ceiling: int,
                input_bytes: int | None = None,
                input_tokens_estimated: int | None = None) -> None:
        instant = datetime.now(timezone.utc)
        if (self.approval.expires_at.tzinfo is None or
                instant >= self.approval.expires_at or
                self.approval.price_checked_on != instant.date()):
            raise BudgetError("BUDGET_EXPIRED")
        if (self.approval.provider_default_checked_on != instant.date() or
                (self.approval.mode != "offline_test" and
                 self.approval.provider_default_checked_on < _DEFAULT_VERIFIED_ON)):
            raise BudgetError("BUDGET_EXPIRED")
        if (not re.fullmatch(r"window_[0-9a-f]{24}", window_id) or
            not re.fullmatch(_FP, call_fingerprint) or
            type(prompt_token_ceiling) is not int or type(completion_token_ceiling) is not int or
            prompt_token_ceiling != 934464 or completion_token_ceiling != 65536):
            raise BudgetError("BUDGET_REQUEST_INVALID")
        if any(type(value) is not int or value < 0 for value in
               (input_bytes, input_tokens_estimated)):
            raise BudgetError("BUDGET_REQUEST_INVALID")
        with _lock(self.root / "budget.lock"):
            attempts = self._load()
            if any(a["call_fingerprint"] == call_fingerprint or a["window_id"] == window_id for a in attempts):
                raise BudgetError("BUDGET_ATTEMPT_SPENT")
            if len(attempts) >= self.approval.max_new_attempts:
                raise BudgetError("BUDGET_EXHAUSTED")
            prompt_total = sum(a["prompt_token_ceiling"] for a in attempts) + prompt_token_ceiling
            completion_total = sum(a["completion_token_ceiling"] for a in attempts) + completion_token_ceiling
            cost = (Decimal(prompt_total) * Decimal(self.approval.input_usd_per_million) +
                    Decimal(completion_total) * Decimal(self.approval.output_usd_per_million)) / 1_000_000
            if (prompt_total > self.approval.max_prompt_tokens_total or
                completion_total > self.approval.max_completion_tokens_total or
                cost > Decimal(self.approval.max_usd)):
                raise BudgetError("BUDGET_EXHAUSTED")
            attempts.append({"window_id": window_id, "call_fingerprint": call_fingerprint,
                "prompt_token_ceiling": prompt_token_ceiling,
                "completion_token_ceiling": completion_token_ceiling,
                "state": "spent_unknown", "error_code": None,
                "safe_status_code": None, "safe_reason": "UNKNOWN",
                "prompt_tokens": None,
                "completion_tokens": None, "reasoning_tokens": None,
                "prompt_cache_hit_tokens": None, "prompt_cache_miss_tokens": None,
                "input_bytes": input_bytes,
                "input_tokens_estimated": input_tokens_estimated,
                "finish_reason": None, "response_model_id": None,
                "http_response_received": False, "elapsed_ms": None,
                "review_state": "not_ready"})
            self._mark_count(len(attempts))
            self._write(attempts)

    def record(self, call_fingerprint: str, *, state: Literal["failed", "extracted"],
               error_code: str | None = None,
               safe_status_code: int | None = None, safe_reason: str = "UNKNOWN",
               prompt_tokens: int | None = None, completion_tokens: int | None = None,
               reasoning_tokens: int | None = None, finish_reason: str | None = None,
               prompt_cache_hit_tokens: int | None = None,
               prompt_cache_miss_tokens: int | None = None,
               response_model_id: str | None = None,
               http_response_received: bool = False,
               elapsed_ms: float | None = None) -> None:
        with _lock(self.root / "budget.lock"):
            attempts = self._load()
            matches = [a for a in attempts if a["call_fingerprint"] == call_fingerprint]
            if len(matches) != 1 or matches[0]["state"] != "spent_unknown":
                raise BudgetError("BUDGET_ATTEMPT_SPENT")
            if (safe_status_code is not None and (type(safe_status_code) is not int or
                not 100 <= safe_status_code <= 599)):
                raise BudgetError("BUDGET_REQUEST_INVALID")
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", safe_reason):
                raise BudgetError("BUDGET_REQUEST_INVALID")
            if error_code is not None and (not isinstance(error_code, str) or
                    not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", error_code)):
                raise BudgetError("BUDGET_REQUEST_INVALID")
            item = matches[0]
            for value, ceiling in ((prompt_tokens, item["prompt_token_ceiling"]),
                                   (completion_tokens, item["completion_token_ceiling"]),
                                   (reasoning_tokens, item["completion_token_ceiling"])):
                if value is not None and (type(value) is not int or not 0 <= value <= ceiling):
                    raise BudgetError("BUDGET_REQUEST_INVALID")
            if (reasoning_tokens is not None and completion_tokens is not None and
                    reasoning_tokens > completion_tokens):
                raise BudgetError("BUDGET_REQUEST_INVALID")
            if any(value is not None and (type(value) is not int or value < 0)
                   for value in (prompt_cache_hit_tokens, prompt_cache_miss_tokens)):
                raise BudgetError("BUDGET_REQUEST_INVALID")
            if (prompt_cache_hit_tokens is not None and prompt_cache_miss_tokens is not None and
                prompt_cache_hit_tokens + prompt_cache_miss_tokens != prompt_tokens):
                raise BudgetError("BUDGET_REQUEST_INVALID")
            if (finish_reason is not None and finish_reason not in
                {"stop", "length", "content_filter", "insufficient_system_resource", "aborted"}):
                raise BudgetError("BUDGET_REQUEST_INVALID")
            if response_model_id is not None and response_model_id != "deepseek-flash":
                raise BudgetError("BUDGET_REQUEST_INVALID")
            if (type(http_response_received) is not bool or
                (elapsed_ms is not None and (type(elapsed_ms) not in (int, float) or
                                             not 0 <= elapsed_ms < 3600000))):
                raise BudgetError("BUDGET_REQUEST_INVALID")
            item.update(state=state, error_code=error_code,
                safe_status_code=safe_status_code, safe_reason=safe_reason,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens, reasoning_tokens=reasoning_tokens,
                prompt_cache_hit_tokens=prompt_cache_hit_tokens,
                prompt_cache_miss_tokens=prompt_cache_miss_tokens,
                finish_reason=finish_reason, response_model_id=response_model_id,
                http_response_received=http_response_received, elapsed_ms=elapsed_ms,
                review_state="pending_review" if state == "extracted" else "not_ready")
            self._write(attempts)

    def mark_published(self) -> None:
        with _lock(self.root / "budget.lock"):
            attempts = self._load()
            if not attempts or any(a["state"] != "extracted" for a in attempts):
                raise BudgetError("BUDGET_NOT_EXTRACTED")
            for item in attempts:
                item["review_state"] = "published"
            self._write(attempts)

    def window_rows(self) -> list[dict]:
        with _lock(self.root / "budget.lock"):
            rows = []
            for item in self._load():
                known = item["prompt_tokens"] is not None and item["completion_tokens"] is not None
                hit = item["prompt_cache_hit_tokens"]
                miss = item["prompt_cache_miss_tokens"]
                input_cost = (Decimal(hit) * Decimal("0.006") + Decimal(miss) * Decimal("0.30")
                              if hit is not None and miss is not None else
                              Decimal(item["prompt_tokens"]) * Decimal("0.30") if known else None)
                usage_cost = ((input_cost + Decimal(item["completion_tokens"]) * Decimal("1.20")) / 1_000_000
                              if known else None)
                rows.append(dict(item,
                    final_tokens=(item["completion_tokens"] - item["reasoning_tokens"]
                                  if known and item["reasoning_tokens"] is not None else None),
                    usage_usd_estimate=str(usage_cost) if usage_cost is not None else None,
                    worst_usd_ceiling=str((Decimal(item["prompt_token_ceiling"]) * Decimal("0.30") +
                        Decimal(item["completion_token_ceiling"]) * Decimal("1.20")) / 1_000_000)))
            return rows

    def status(self) -> dict:
        with _lock(self.root / "budget.lock"):
            attempts = self._load()
            prompt_total = sum(a["prompt_token_ceiling"] for a in attempts)
            completion_total = sum(a["completion_token_ceiling"] for a in attempts)
            worst = (Decimal(prompt_total) * Decimal(self.approval.input_usd_per_million) +
                     Decimal(completion_total) * Decimal(self.approval.output_usd_per_million)) / 1_000_000
            known_prompt = sum(a["prompt_tokens"] for a in attempts if type(a["prompt_tokens"]) is int)
            known_completion = sum(a["completion_tokens"] for a in attempts if type(a["completion_tokens"]) is int)
            known_reasoning = sum(a["reasoning_tokens"] for a in attempts if type(a["reasoning_tokens"]) is int)
            known_cost = sum((
                Decimal(a["prompt_cache_hit_tokens"]) * Decimal("0.006") +
                Decimal(a["prompt_cache_miss_tokens"]) * Decimal(self.approval.input_usd_per_million) +
                Decimal(a["completion_tokens"]) * Decimal(self.approval.output_usd_per_million)
                if type(a["prompt_cache_hit_tokens"]) is int and
                   type(a["prompt_cache_miss_tokens"]) is int and
                   type(a["completion_tokens"]) is int else
                Decimal(a["prompt_tokens"]) * Decimal(self.approval.input_usd_per_million) +
                Decimal(a["completion_tokens"]) * Decimal(self.approval.output_usd_per_million)
                ) / 1_000_000 for a in attempts if type(a["prompt_tokens"]) is int and
                type(a["completion_tokens"]) is int)
            return {"authorization_id": self.approval.authorization_id,
                "attempts_reserved": len(attempts), "max_new_attempts": self.approval.max_new_attempts,
                "spent_unknown": sum(a["state"] == "spent_unknown" for a in attempts),
                "failed": sum(a["state"] == "failed" for a in attempts),
                "extracted": sum(a["state"] == "extracted" for a in attempts),
                "prompt_token_ceiling_reserved": prompt_total,
                "completion_token_ceiling_reserved": completion_total,
                "worst_usd_reserved": str(worst),
                "known_prompt_tokens": known_prompt,
                "known_completion_tokens": known_completion,
                "known_reasoning_tokens": known_reasoning,
                "cache_aware_usage_attempts": sum(type(a["prompt_cache_hit_tokens"]) is int and
                    type(a["prompt_cache_miss_tokens"]) is int for a in attempts),
                "known_usage_attempts": sum(type(a["prompt_tokens"]) is int and
                                            type(a["completion_tokens"]) is int for a in attempts),
                "usage_unknown_attempts": sum(a["prompt_tokens"] is None or
                                              a["completion_tokens"] is None for a in attempts),
                "known_usage_usd_estimate": str(known_cost)}
