"""Synthetic Fake SDK lifecycle; never a real LoCoMo/API run."""

import json
import shutil
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from dynamic_memory.memory.batch_budget import BatchApproval, BudgetError, load_approval, _fp
from dynamic_memory.memory.batch_budget import BatchBudgetLedger
from dynamic_memory.memory.checkpoints import CheckpointError, WindowCheckpointStore
from dynamic_memory.memory.review_workflow import ReviewError, ReviewQueue
from dynamic_memory.memory.auto_review import write_auto_review_files
from dynamic_memory.retrieval import EncoderIdentity
from scripts.stage_b_repair_runner import _budget_root, prepare_plan, run_plan
from tests.integration.test_review_workflow import files
from tests.integration.test_reviewed_production_store import FakeSDK, fixture_63

REAL_DATA = Path(__file__).resolve().parents[2] / "data/raw/locomo10.json"
requires_real_data = pytest.mark.skipif(
    not REAL_DATA.is_file(),
    reason="official LoCoMo raw data is intentionally Git-ignored",
)


class FixedEncoder:
    identity = EncoderIdentity(model_name="synthetic-fixed", model_revision="v1",
        model_fingerprint="sha256:" + "1" * 64,
        encoding_fingerprint="sha256:" + "2" * 64)

    def embed_documents(self, texts):
        return [[1.0, 0.0] for _ in texts]

    def embed_query(self, _question):
        return [1.0, 0.0]


def approved_plan(tmp_path, *, limit=63, usd="22.6158912"):
    bundle, settings, _, _ = fixture_63()
    sdk = FakeSDK()
    plan = prepare_plan(mode="offline_test", bundle=bundle, settings=settings,
                        sample_id="synthetic-0", fake_client=sdk)
    approval = BatchApproval(authorization_id="offline-test-20261103",
        approved_by="synthetic-test-authority", output_policy="provider_default_64k",
        mode="offline_test", sample_id="synthetic-0",
        dataset_sha256=bundle.snapshot_sha256,
        run_fingerprint=plan.run_fingerprint, plan_fingerprint=plan.plan_fingerprint,
        max_new_attempts=limit, max_prompt_tokens_total=63 * 934464,
        max_completion_tokens_total=63 * 65536, max_usd=usd,
        input_usd_per_million="0.30", output_usd_per_million="1.20",
        price_checked_on=datetime.now(timezone.utc).date(), provider_default_checked_on=datetime.now(timezone.utc).date(),
        expires_at=datetime.now(timezone.utc) + timedelta(days=1))
    path = tmp_path / "independent-approval" / "budget.json"
    path.parent.mkdir(parents=True)
    path.write_text(approval.model_dump_json(), encoding="utf-8")
    loaded, digest = load_approval(path, mode="offline_test", sample_id="synthetic-0",
        dataset_sha256=bundle.snapshot_sha256, run_fingerprint=plan.run_fingerprint,
        plan_fingerprint=plan.plan_fingerprint,
        authorization_id="offline-test-20261103")
    return plan, sdk, loaded, digest


def test_63_fake_pause_resume_manual_review_publish_and_index(tmp_path):
    plan, sdk, approval, digest = approved_plan(tmp_path)
    state = tmp_path / "state"
    first = run_plan(plan, state_dir=state, approval=approval,
                     approval_fingerprint=digest, allow_new_calls=True, stop_after=5)
    assert first["status"] == "PAUSED" and first["completed_windows"] == 5
    assert first["network_calls"] == 5 and sdk.calls == 5
    waiting = run_plan(plan, state_dir=state, approval=approval,
                       approval_fingerprint=digest, allow_new_calls=False)
    assert waiting["status"] == "PENDING_AUTHORIZATION" and sdk.calls == 5
    second = run_plan(plan, state_dir=state, approval=approval,
                      approval_fingerprint=digest, allow_new_calls=True)
    assert second["status"] == "PENDING_REVIEW" and second["completed_windows"] == 63
    assert second["network_calls"] == 58 and sdk.calls == 63
    queue = ReviewQueue.model_validate_json((state / "private_review/queue.json").read_bytes())
    review_file, authority_file = files(tmp_path, queue)
    published = run_plan(plan, state_dir=state, approval=approval,
        approval_fingerprint=digest, allow_new_calls=False, review_file=review_file,
        authority_file=authority_file, index_encoder=FixedEncoder())
    assert published["status"] == "PUBLISHED" and published["memory_count"] == 63
    assert published["network_calls"] == 0 and sdk.calls == 63
    assert (state / "indexes/synthetic-0.json").exists()


def test_automatic_review_replays_and_publishes_without_new_calls(tmp_path):
    plan, sdk, approval, digest = approved_plan(tmp_path)
    state = tmp_path / "state"
    pending = run_plan(plan, state_dir=state, approval=approval,
        approval_fingerprint=digest, allow_new_calls=True)
    assert pending["status"] == "PENDING_REVIEW" and sdk.calls == 63
    queue = ReviewQueue.model_validate_json((state / "private_review/queue.json").read_bytes())
    review_file, authority_file = write_auto_review_files(queue, tmp_path / "machine-review",
        run_kind="offline_test")
    published = run_plan(plan, state_dir=state, approval=approval,
        approval_fingerprint=digest, allow_new_calls=False,
        review_file=review_file, authority_file=authority_file,
        index_encoder=FixedEncoder())
    assert published["status"] == "PUBLISHED" and published["memory_count"] == 63
    assert published["network_calls"] == 0 and sdk.calls == 63
    assert (state / "indexes/synthetic-0.json").exists()


def test_budget_exhaustion_does_not_send_extra_call(tmp_path):
    plan, sdk, approval, digest = approved_plan(tmp_path, limit=3)
    result = run_plan(plan, state_dir=tmp_path / "state", approval=approval,
                      approval_fingerprint=digest, allow_new_calls=True)
    assert result["status"] == "BUDGET_EXHAUSTED"
    assert result["completed_windows"] == 3 and sdk.calls == 3
    again = run_plan(plan, state_dir=tmp_path / "state", approval=approval,
                     approval_fingerprint=digest, allow_new_calls=True)
    assert again["status"] == "BUDGET_EXHAUSTED" and sdk.calls == 3


def test_expired_or_wrong_approval_fails_before_http(tmp_path):
    plan, sdk, approval, digest = approved_plan(tmp_path)
    with pytest.raises(BudgetError, match="APPROVAL_INVALID_OR_EXPIRED"):
        run_plan(plan, state_dir=tmp_path / "state", approval=approval.model_copy(update={
            "run_fingerprint": "sha256:" + "0" * 64}),
            approval_fingerprint=digest, allow_new_calls=True)
    assert sdk.calls == 0


class FailingFourthSDK(FakeSDK):
    def create(self, **kwargs):
        if self.calls == 3:
            self.calls += 1
            raise type("APIStatusError", (Exception,), {"status_code": 402})("private provider text")
        return super().create(**kwargs)


def test_provider_failure_is_sanitized_and_spent_window_never_retried(tmp_path):
    bundle, settings, _, _ = fixture_63()
    sdk = FailingFourthSDK()
    plan = prepare_plan(mode="offline_test", bundle=bundle, settings=settings,
                        sample_id="synthetic-0", fake_client=sdk)
    _, _, approval, digest = approved_plan(tmp_path)
    state = tmp_path / "state"
    result = run_plan(plan, state_dir=state, approval=approval,
                      approval_fingerprint=digest, allow_new_calls=True)
    assert result["status"] == "FAILED" and result["error_code"] == "API_STATUS"
    assert result["safe_status_code"] == 402 and result["safe_reason"] == "INSUFFICIENT_BALANCE"
    assert result["completed_windows"] == 3 and sdk.calls == 4
    assert "private provider text" not in str(result)
    resumed = run_plan(plan, state_dir=state, approval=approval,
                       approval_fingerprint=digest, allow_new_calls=True)
    assert resumed["status"] == "BUDGET_ATTEMPT_SPENT" and sdk.calls == 4


class TruncatedFirstSDK(FakeSDK):
    def create(self, **kwargs):
        self.calls += 1
        assert "max_tokens" not in kwargs
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="length",
            message=SimpleNamespace(content='{"facts":[', reasoning_content="private"))],
            model="deepseek-flash", usage=SimpleNamespace(prompt_tokens=713,
                completion_tokens=65536,
                prompt_cache_hit_tokens=100, prompt_cache_miss_tokens=613,
                completion_tokens_details=SimpleNamespace(reasoning_tokens=65520)))


def test_truncated_response_retains_usage_and_cannot_retry(tmp_path):
    bundle, settings, _, _ = fixture_63()
    sdk = TruncatedFirstSDK()
    plan = prepare_plan(mode="offline_test", bundle=bundle, settings=settings,
                        sample_id="synthetic-0", fake_client=sdk)
    _, _, approval, digest = approved_plan(tmp_path)
    state = tmp_path / "state"
    result = run_plan(plan, state_dir=state, approval=approval,
                      approval_fingerprint=digest, allow_new_calls=True)
    assert result["status"] == "FAILED" and result["error_code"] == "TRUNCATED"
    assert result["finish_reason"] == "length" and result["http_response_received"]
    assert result["known_prompt_tokens"] == 713
    assert result["known_completion_tokens"] == 65536
    assert result["known_reasoning_tokens"] == 65520
    assert result["usage_unknown_attempts"] == 0 and sdk.calls == 1
    assert result["known_usage_usd_estimate"] == "0.0788277"
    assert result["cache_aware_usage_attempts"] == 1
    ledger = BatchBudgetLedger(state / "budget", approval, digest)
    assert ledger.window_rows()[0]["final_tokens"] == 16
    assert ledger.window_rows()[0]["usage_usd_estimate"] == "0.0788277"
    again = run_plan(plan, state_dir=state, approval=approval,
                     approval_fingerprint=digest, allow_new_calls=True)
    assert again["status"] == "BUDGET_ATTEMPT_SPENT" and sdk.calls == 1


def test_deleted_ledger_cannot_restore_spent_authorization(tmp_path):
    plan, sdk, approval, digest = approved_plan(tmp_path)
    state = tmp_path / "state"
    run_plan(plan, state_dir=state, approval=approval,
             approval_fingerprint=digest, allow_new_calls=True, stop_after=1)
    assert sdk.calls == 1
    shutil.rmtree(state / "budget")
    with pytest.raises(BudgetError, match="BUDGET_CORRUPT_OR_STALE"):
        run_plan(plan, state_dir=state, approval=approval,
                 approval_fingerprint=digest, allow_new_calls=True)
    assert sdk.calls == 1


def test_rolled_back_ledger_count_is_rejected_by_registry(tmp_path):
    plan, sdk, approval, digest = approved_plan(tmp_path)
    state = tmp_path / "state"
    run_plan(plan, state_dir=state, approval=approval,
             approval_fingerprint=digest, allow_new_calls=True, stop_after=2)
    ledger_path = state / "budget" / "attempts.json"
    record = json.loads(ledger_path.read_text(encoding="utf-8"))
    record["attempts"] = record["attempts"][:1]
    record["digest"] = _fp(record["attempts"])
    ledger_path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(BudgetError, match="BUDGET_CORRUPT_OR_STALE"):
        run_plan(plan, state_dir=state, approval=approval,
                 approval_fingerprint=digest, allow_new_calls=True)
    assert sdk.calls == 2


def test_crash_after_spend_cannot_repeat_and_bad_state_fails_closed(tmp_path, monkeypatch):
    plan, sdk, approval, digest = approved_plan(tmp_path)
    state = tmp_path / "state"
    original = WindowCheckpointStore.save
    def crash_once(self, *args, **kwargs):
        raise CheckpointError("SYNTHETIC_CRASH")
    monkeypatch.setattr(WindowCheckpointStore, "save", crash_once)
    with pytest.raises(CheckpointError, match="SYNTHETIC_CRASH"):
        run_plan(plan, state_dir=state, approval=approval,
                 approval_fingerprint=digest, allow_new_calls=True)
    assert sdk.calls == 1
    monkeypatch.setattr(WindowCheckpointStore, "save", original)
    again = run_plan(plan, state_dir=state, approval=approval,
                     approval_fingerprint=digest, allow_new_calls=True)
    assert again["status"] == "BUDGET_ATTEMPT_SPENT" and sdk.calls == 1
    (state / "budget/attempts.json").write_text("{}", encoding="utf-8")
    with pytest.raises(BudgetError, match="BUDGET_CORRUPT_OR_STALE"):
        run_plan(plan, state_dir=state, approval=approval,
                 approval_fingerprint=digest, allow_new_calls=True)


def test_cross_process_lock_rejects_second_runner(tmp_path):
    plan, sdk, approval, digest = approved_plan(tmp_path)
    state = tmp_path / "state"
    ledger = BatchBudgetLedger(state / "budget", approval, digest)
    with ledger.run_lock():
        with pytest.raises(BudgetError, match="BUDGET_CONCURRENT"):
            run_plan(plan, state_dir=state, approval=approval,
                     approval_fingerprint=digest, allow_new_calls=True)
    assert sdk.calls == 0


def test_actual_second_process_cannot_enter_same_run_lock(tmp_path):
    _, _, approval, digest = approved_plan(tmp_path)
    root = tmp_path / "state" / "budget"
    ready = tmp_path / "ready"
    code = ("from pathlib import Path\nfrom dynamic_memory.memory.batch_budget import _lock\n"
            "import sys,time\nwith _lock(Path(sys.argv[1])):\n"
            "    Path(sys.argv[2]).write_text('ready')\n    time.sleep(5)\n")
    child = subprocess.Popen([sys.executable, "-B", "-c", code,
                              str(root / "run.lock"), str(ready)])
    try:
        for _ in range(50):
            if ready.exists() or child.poll() is not None:
                break
            time.sleep(0.1)
        assert ready.exists() and child.poll() is None
        ledger = BatchBudgetLedger(root, approval, digest)
        with pytest.raises(BudgetError, match="BUDGET_CONCURRENT"):
            with ledger.run_lock():
                pass
    finally:
        child.wait(timeout=10)


class ZeroFirstSDK(FakeSDK):
    def create(self, **kwargs):
        if self.calls == 0:
            self.calls += 1
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
                message=SimpleNamespace(content='{"facts":[]}', reasoning_content="fake private"))],
                model="deepseek-flash", usage=SimpleNamespace(prompt_tokens=100,
                    completion_tokens=20, completion_tokens_details=SimpleNamespace(reasoning_tokens=5)))
        return super().create(**kwargs)


def test_zero_fact_window_requires_explicit_window_approval(tmp_path):
    bundle, settings, _, _ = fixture_63()
    sdk = ZeroFirstSDK()
    plan = prepare_plan(mode="offline_test", bundle=bundle, settings=settings,
                        sample_id="synthetic-0", fake_client=sdk)
    _, _, approval, digest = approved_plan(tmp_path)
    state = tmp_path / "state"
    waiting = run_plan(plan, state_dir=state, approval=approval,
                       approval_fingerprint=digest, allow_new_calls=True)
    assert waiting["status"] == "PENDING_REVIEW" and sdk.calls == 63
    queue = ReviewQueue.model_validate_json((state / "private_review/queue.json").read_bytes())
    assert len(queue.windows[0].facts) == 0
    review_file, authority_file = files(tmp_path, queue)
    published = run_plan(plan, state_dir=state, approval=approval,
        approval_fingerprint=digest, allow_new_calls=False, review_file=review_file,
        authority_file=authority_file)
    assert published["status"] == "PUBLISHED" and published["memory_count"] == 62
    again = run_plan(plan, state_dir=state, approval=approval,
        approval_fingerprint=digest, allow_new_calls=False, review_file=review_file,
        authority_file=authority_file)
    assert again["status"] == "ALREADY_PUBLISHED" and again["build_id"] == published["build_id"]
    assert sdk.calls == 63


def test_corrupt_checkpoint_and_review_queue_stop_without_new_call(tmp_path):
    plan, sdk, approval, digest = approved_plan(tmp_path)
    state = tmp_path / "state"
    run_plan(plan, state_dir=state, approval=approval,
             approval_fingerprint=digest, allow_new_calls=True, stop_after=5)
    assert sdk.calls == 5
    first = next((state / "checkpoints").glob("window_*.json"))
    first.write_text("{}", encoding="utf-8")
    with pytest.raises(CheckpointError, match="CHECKPOINT_CORRUPT_OR_STALE"):
        run_plan(plan, state_dir=state, approval=approval,
                 approval_fingerprint=digest, allow_new_calls=True)
    assert sdk.calls == 5

    clean = tmp_path / "separate-state"
    result = run_plan(plan, state_dir=clean, approval=approval,
                      approval_fingerprint=digest, allow_new_calls=True)
    assert result["status"] == "PENDING_REVIEW" and sdk.calls == 68
    (clean / "private_review/queue.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ReviewError, match="REVIEW_QUEUE_STALE"):
        run_plan(plan, state_dir=clean, approval=approval,
                 approval_fingerprint=digest, allow_new_calls=False)
    assert sdk.calls == 68


@requires_real_data
def test_real_budget_location_is_independent_of_mutable_state_directory(tmp_path, monkeypatch):
    plan = prepare_plan(mode="batch")  # frozen real data, planning only; zero HTTP
    approval = BatchApproval(authorization_id="test-only-fixed-budget-id",
        approved_by="synthetic-test-authority", output_policy="provider_default_64k",
        mode="batch", sample_id="conv-30", dataset_sha256=plan.bundle.snapshot_sha256,
        run_fingerprint=plan.run_fingerprint, plan_fingerprint=plan.plan_fingerprint,
        max_new_attempts=63, max_prompt_tokens_total=63 * 934464,
        max_completion_tokens_total=63 * 65536, max_usd="22.6158912",
        input_usd_per_million="0.30", output_usd_per_million="1.20",
        price_checked_on=datetime.now(timezone.utc).date(), provider_default_checked_on=datetime.now(timezone.utc).date(),
        expires_at=datetime.now(timezone.utc) + timedelta(days=1))
    first = _budget_root(plan, tmp_path / "one", approval)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "forged-local-appdata"))
    second = _budget_root(plan, tmp_path / "two", approval)
    assert first == second and not first.is_relative_to(tmp_path)


def test_old_approval_format_cannot_authorize_new_high_policy(tmp_path):
    plan, sdk, approval, _ = approved_plan(tmp_path)
    path = tmp_path / "old.json"
    path.write_text(approval.model_copy(update={"format_version": 1}).model_dump_json(),
                    encoding="utf-8")
    with pytest.raises(BudgetError, match="APPROVAL_INVALID_OR_EXPIRED"):
        load_approval(path, mode="offline_test", sample_id="synthetic-0",
            dataset_sha256=plan.bundle.snapshot_sha256,
            run_fingerprint=plan.run_fingerprint,
            plan_fingerprint=plan.plan_fingerprint,
            authorization_id=approval.authorization_id)
    assert sdk.calls == 0


@pytest.mark.parametrize("change", ["expired", "stale_price", "stale_default", "wrong_sample"])
def test_human_approval_file_drift_fails_before_any_attempt(tmp_path, change):
    plan, sdk, approval, _ = approved_plan(tmp_path)
    if change == "expired":
        approval = approval.model_copy(update={"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)})
    elif change == "stale_price":
        approval = approval.model_copy(update={"price_checked_on": date(2020, 1, 1)})
    elif change == "stale_default":
        approval = approval.model_copy(update={"provider_default_checked_on": date(2020, 1, 1)})
    else:
        approval = approval.model_copy(update={"sample_id": "synthetic-other"})
    path = tmp_path / "human" / "approval.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(approval.model_dump_json(), encoding="utf-8")
    with pytest.raises(BudgetError, match="APPROVAL_INVALID_OR_EXPIRED"):
        load_approval(path, mode="offline_test", sample_id="synthetic-0",
            dataset_sha256=plan.bundle.snapshot_sha256,
            run_fingerprint=plan.run_fingerprint,
            plan_fingerprint=plan.plan_fingerprint,
            authorization_id="offline-test-20261103")
    assert sdk.calls == 0


def test_budget_expiry_is_rechecked_before_each_reservation(tmp_path):
    plan, sdk, approval, digest = approved_plan(tmp_path)
    expired = approval.model_copy(update={
        "expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)})
    ledger = BatchBudgetLedger(tmp_path / "budget", expired, digest)
    first = plan.selected[0]
    with pytest.raises(BudgetError, match="BUDGET_EXPIRED"):
        ledger.reserve(window_id=first.extraction_window_id,
            call_fingerprint=plan.call_fingerprints[first.extraction_window_id],
            prompt_token_ceiling=4096, completion_token_ceiling=2048)
    assert sdk.calls == 0
