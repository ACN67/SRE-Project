"""Offline safety checks: a real paid approval cannot self-initialize a vanished ledger."""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone

import pytest

from dynamic_memory.memory import batch_budget
from dynamic_memory.memory.batch_budget import (
    BatchApproval, BatchBudgetLedger, BudgetError,
)


def _real_approval(monkeypatch, *, authorization_id="test-only-fresh-pilot-id"):
    now = datetime.now(timezone.utc)
    # Provider policy verification is independent of the test's wall clock.
    monkeypatch.setattr(batch_budget, "_DEFAULT_VERIFIED_ON", now.date())
    return BatchApproval(
        authorization_id=authorization_id,
        approved_by="test-independent-reviewer",
        mode="pilot",
        sample_id="conv-30",
        dataset_sha256="79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4",
        run_fingerprint="sha256:" + "b" * 64,
        plan_fingerprint="sha256:" + "c" * 64,
        selected_window_id="window_" + "a" * 24,
        model_id="deepseek-flash", thinking="enabled", reasoning_effort="high",
        output_policy="provider_default_64k",
        max_new_attempts=1,
        max_prompt_tokens_total=934464,
        max_completion_tokens_total=65536,
        max_usd="0.3589824",
        input_usd_per_million="0.30",
        output_usd_per_million="1.20",
        price_checked_on=now.date(),
        provider_default_checked_on=now.date(),
        expires_at=now + timedelta(hours=2),
    )


def _ledger(tmp_path, approval):
    return BatchBudgetLedger(
        tmp_path / "budget",
        approval,
        "sha256:" + "d" * 64,
        tmp_path / "registry",
    )


def _attempt(ledger):
    ledger.reserve(
        window_id="window_" + "a" * 24,
        call_fingerprint="sha256:" + "e" * 64,
        prompt_token_ceiling=934464,
        completion_token_ceiling=65536,
        input_bytes=2000,
        input_tokens_estimated=2100,
    )


def test_real_approval_is_not_automatically_enrolled(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path, _real_approval(monkeypatch))
    with pytest.raises(BudgetError, match="BUDGET_ENROLLMENT_REQUIRED"):
        _attempt(ledger)
    assert not ledger.marker.exists()
    assert not (ledger.root / "attempts.json").exists()


def test_independent_enrollment_is_zero_network_and_spends_only_once(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path, _real_approval(monkeypatch))
    ledger.enroll()
    marker = json.loads(ledger.marker.read_text(encoding="utf-8"))
    assert marker["attempt_count"] == 0
    assert not (ledger.root / "attempts.json").exists()
    with pytest.raises(BudgetError, match="BUDGET_ALREADY_ENROLLED"):
        ledger.enroll()
    _attempt(ledger)
    assert ledger.status()["attempts_reserved"] == 1
    assert json.loads(ledger.marker.read_text(encoding="utf-8"))["attempt_count"] == 1
    with pytest.raises(BudgetError, match="BUDGET_ATTEMPT_SPENT"):
        _attempt(ledger)


def test_missing_paid_ledger_is_never_refilled_by_runner(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path, _real_approval(monkeypatch))
    ledger.enroll()
    _attempt(ledger)
    shutil.rmtree(ledger.root)
    with pytest.raises(BudgetError, match="BUDGET_CORRUPT_OR_STALE"):
        _attempt(ledger)
    shutil.rmtree(ledger.registry_root)
    with pytest.raises(BudgetError, match="BUDGET_ENROLLMENT_REQUIRED"):
        _attempt(ledger)
    assert not (ledger.root / "attempts.json").exists()


def test_known_paid_pilots_never_reenroll_even_if_every_local_record_is_missing(
    tmp_path, monkeypatch,
):
    for old in (
        "user-chat-20261103-pilot01",
        "user-chat-20261103-final-pilot01",
    ):
        ledger = _ledger(tmp_path / old, _real_approval(monkeypatch, authorization_id=old))
        with pytest.raises(BudgetError, match="BUDGET_PREVIOUSLY_CONSUMED"):
            ledger.enroll()
        with pytest.raises(BudgetError, match="BUDGET_PREVIOUSLY_CONSUMED"):
            _attempt(ledger)


def test_offline_synthetic_mode_keeps_existing_fake_automatic_start(
    tmp_path, monkeypatch,
):
    approval = _real_approval(monkeypatch).model_copy(update={
        "mode": "offline_test",
        "sample_id": "synthetic-0",
    })
    ledger = _ledger(tmp_path, approval)
    _attempt(ledger)
    assert ledger.status()["attempts_reserved"] == 1
    assert ledger.marker.exists()
    with pytest.raises(BudgetError, match="BUDGET_ENROLLMENT_NOT_REQUIRED"):
        ledger.enroll()


def test_expired_or_naive_enrollment_fails_closed(tmp_path, monkeypatch):
    approval = _real_approval(monkeypatch)
    for new in (
        approval.model_copy(update={"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)}),
        approval.model_copy(update={"expires_at": datetime.now().replace(tzinfo=None)}),
    ):
        ledger = _ledger(tmp_path / str(new.expires_at).replace(":", "-"), new)
        with pytest.raises(BudgetError, match="BUDGET_ENROLLMENT_EXPIRED"):
            ledger.enroll()
        assert not ledger.marker.exists()
