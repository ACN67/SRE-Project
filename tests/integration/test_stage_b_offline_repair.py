"""B offline regression: sanitized schema details and honest partial-batch continuation.

All SDK clients below are Fake; this module never uses DeepSeek credentials.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from dynamic_memory.memory.batch_budget import BatchBudgetLedger, BudgetError, load_approval
from dynamic_memory.memory.checkpoints import CheckpointError, WindowCheckpointStore
from scripts.stage_b_repair_runner import CODE_FILES, prepare_plan, run_plan
from tests.integration.test_batch_runner import approved_plan, FailingFourthSDK
from tests.integration.test_reviewed_production_store import FakeSDK, fixture_63


def _invalid_response(kwargs, *, issue: str):
    assert "max_tokens" not in kwargs
    window = json.loads(kwargs["messages"][-1]["content"].split("\n", 1)[1])
    turn = window["turns"][0]
    draft = {
        "content": turn["visible_text"],
        "entity": "Ada", "attribute": "note", "value": "note",
        "fact_kind": "state", "assertion_type": "fact",
        "polarity": "positive", "recorded_at": None,
        "event_time": None, "source_refs": [turn["source_ref"]],
    }
    if issue == "missing":
        draft.pop("recorded_at")
    elif issue == "extra":
        draft["SECRET_USER_123456_PRIVATE_DATA"] = "password-never-log"
    elif issue == "source_type":
        draft["source_refs"] = "do-not-log-this-private-value"
    elif issue == "literal":
        draft["fact_kind"] = "sensitive-not-a-valid-kind"
    elif issue == "time":
        draft["recorded_at"] = "private-malformed-date"
    elif issue == "nested":
        draft["source_refs"] = [{"session_id": turn["source_ref"]["session_id"]}]
    elif issue == "value_number":
        draft["value"] = 23
    elif issue == "resolution":
        draft["event_time"] = {"raw": "yesterday", "start": None,
            "end_exclusive": None, "precision": "day",
            "resolution": "PRIVATE-invalid-resolution-label"}
    else:
        raise AssertionError("unknown invalid test")
    return SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="stop",
                 message=SimpleNamespace(content=json.dumps({"facts": [draft]}),
                                         reasoning_content="private-reasoning-never-log"))],
        model="deepseek-flash",
        usage=SimpleNamespace(
            prompt_tokens=123, completion_tokens=78,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=35),
        ),
    )


class InvalidAtSDK(FakeSDK):
    def __init__(self, invalid_at=0, issue="missing"):
        super().__init__()
        self.invalid_at = invalid_at
        self.issue = issue

    def create(self, **kwargs):
        if self.calls == self.invalid_at:
            self.calls += 1
            return _invalid_response(kwargs, issue=self.issue)
        return super().create(**kwargs)


def _plan_for_fake(sdk):
    bundle, settings, _, _ = fixture_63()
    return prepare_plan(mode="offline_test", bundle=bundle, settings=settings,
                        sample_id="synthetic-0", fake_client=sdk)


@pytest.mark.parametrize("issue,field,kind", [
    ("missing", "facts[0].recorded_at", "missing"),
    ("extra", "facts[0].unknown_field", "extra_forbidden"),
    ("source_type", "facts[0].source_refs", "list_type"),
    ("literal", "facts[0].fact_kind", "literal_error"),
    ("time", "facts[0].recorded_at", "value_error"),
    ("nested", "facts[0].source_refs[0].dia_id", "missing"),
    ("resolution", "facts[0].event_time.resolution", "literal_error"),
    ("value_number", "facts[0].value", "string_type"),
])
def test_schema_failure_reports_only_allowlisted_fields(
    tmp_path, issue, field, kind,
):
    sdk = InvalidAtSDK(issue=issue)
    plan = _plan_for_fake(sdk)
    _, _, approval, digest = approved_plan(tmp_path)
    state = tmp_path / "state"
    result = run_plan(plan, state_dir=state, approval=approval,
                      approval_fingerprint=digest, allow_new_calls=True)
    assert result["status"] == "FAILED"
    assert result["error_code"] == "SCHEMA_INVALID"
    assert result["network_calls"] == 1 and sdk.calls == 1
    assert result["validation_error_count"] >= 1
    assert {"field": field, "type": kind} in result["validation_errors"]
    path = state / "private_diagnostics" / f"{plan.selected[0].extraction_window_id}.json"
    assert path.is_file() and str(path) == result["private_diagnostic_path"]
    private_diag = json.loads(path.read_text(encoding="utf-8"))
    assert private_diag["validation_errors"] == result["validation_errors"]
    assert private_diag["validation_error_count"] == result["validation_error_count"]
    assert len(private_diag["validation_errors"]) <= 8
    assert result["known_prompt_tokens"] == 123
    assert result["known_completion_tokens"] == 78
    assert result["known_reasoning_tokens"] == 35
    # Neither terminal summaries nor local audit events contain raw answers.
    event = plan.runtime.recorder.store.events()[-1]
    assert event["validation_errors"] == result["validation_errors"]
    combined = json.dumps(result) + json.dumps(private_diag) + json.dumps(event)
    for forbidden in ("password-never-log", "SECRET_USER_123456_PRIVATE_DATA",
                      "private-reasoning-never-log",
                      "do-not-log-this-private-value",
                      "private-malformed-date", "sensitive-not-a-valid-kind",
                      "PRIVATE-invalid-resolution-label"):
        assert forbidden not in combined
    # Replaying this spent window is never permitted even after validation fails.
    result2 = run_plan(plan, state_dir=state, approval=approval,
                       approval_fingerprint=digest, allow_new_calls=True)
    assert result2["status"] == "BUDGET_ATTEMPT_SPENT"
    assert sdk.calls == 1


def test_explicit_skip_preserves_failed_window_and_extracts_remaining_60(
    tmp_path,
):
    sdk = InvalidAtSDK(invalid_at=2)
    plan = _plan_for_fake(sdk)
    _, _, approval, digest = approved_plan(tmp_path)
    state = tmp_path / "state"

    first = run_plan(plan, state_dir=state, approval=approval,
                     approval_fingerprint=digest, allow_new_calls=True)
    assert first["status"] == "FAILED"
    assert first["completed_windows"] == 2 and first["network_calls"] == 3
    assert sdk.calls == 3
    failed_id = plan.selected[2].extraction_window_id
    no_skip = run_plan(plan, state_dir=state, approval=approval,
                       approval_fingerprint=digest, allow_new_calls=True)
    assert no_skip["status"] == "BUDGET_ATTEMPT_SPENT" and sdk.calls == 3

    # Opt in to inspect the historical failed attempt and continue with NEW
    # windows. This never reissues the failed window or creates a review queue.
    continued = run_plan(
        plan, state_dir=state, approval=approval,
        approval_fingerprint=digest, allow_new_calls=True,
        continue_past_failed=True,
    )
    assert continued["status"] == "INCOMPLETE_SPENT_FAILURES"
    assert continued["skipped_failed_windows"] == [failed_id]
    assert continued["completed_windows"] == 62
    assert continued["missing_windows"] == 1
    assert continued["new_calls_this_run"] == 60
    assert continued["network_calls"] == 60
    assert continued["published"] is False
    assert sdk.calls == 63
    assert not (state / "private_review/queue.json").exists()
    assert not (state / "artifacts/synthetic-0/current.json").exists()
    ledger = BatchBudgetLedger(state / "budget", approval, digest)
    assert ledger.status()["attempts_reserved"] == 63
    assert ledger.status()["extracted"] == 62
    assert ledger.status()["failed"] == 1
    again = run_plan(
        plan, state_dir=state, approval=approval,
        approval_fingerprint=digest, allow_new_calls=False,
        continue_past_failed=True,
    )
    assert again["status"] == "INCOMPLETE_SPENT_FAILURES"
    assert again["network_calls"] == 0
    assert sdk.calls == 63


def test_current_failure_can_skip_remaining_only_with_explicit_opt_in(tmp_path):
    sdk = InvalidAtSDK(invalid_at=0)
    plan = _plan_for_fake(sdk)
    _, _, approval, digest = approved_plan(tmp_path)
    state = tmp_path / "state"
    result = run_plan(plan, state_dir=state, approval=approval,
                      approval_fingerprint=digest,
                      allow_new_calls=True, continue_past_failed=True,
                      stop_after=6)
    assert result["status"] == "PAUSED"
    assert result["network_calls"] == 6
    assert result["completed_windows"] == 5
    assert sdk.calls == 6
    done = run_plan(plan, state_dir=state, approval=approval,
                    approval_fingerprint=digest,
                    allow_new_calls=True, continue_past_failed=True)
    assert done["status"] == "INCOMPLETE_SPENT_FAILURES"
    assert done["completed_windows"] == 62
    assert done["missing_windows"] == 1
    assert sdk.calls == 63


def test_spent_unknown_is_not_skippable_and_no_retries_after_crash(
    tmp_path, monkeypatch,
):
    sdk = FakeSDK()
    plan = _plan_for_fake(sdk)
    _, _, approval, digest = approved_plan(tmp_path)
    state = tmp_path / "state"
    original_save = WindowCheckpointStore.save

    def fail_before_checkpoint(self, *_args, **_kwargs):
        raise CheckpointError("SYNTHETIC_CRASH")

    monkeypatch.setattr(WindowCheckpointStore, "save", fail_before_checkpoint)
    with pytest.raises(CheckpointError, match="SYNTHETIC_CRASH"):
        run_plan(plan, state_dir=state, approval=approval,
                 approval_fingerprint=digest, allow_new_calls=True)
    monkeypatch.setattr(WindowCheckpointStore, "save", original_save)
    assert sdk.calls == 1
    result = run_plan(plan, state_dir=state, approval=approval,
                      approval_fingerprint=digest,
                      allow_new_calls=True, continue_past_failed=True)
    assert result["status"] == "BUDGET_ATTEMPT_SPENT"
    assert sdk.calls == 1
    assert not (state / "private_review/queue.json").exists()


def test_invalid_skip_policy_and_missing_checkpoint_never_publish(
    tmp_path,
):
    sdk = FakeSDK()
    plan = _plan_for_fake(sdk)
    _, _, approval, digest = approved_plan(tmp_path)
    state = tmp_path / "state"
    with pytest.raises(BudgetError, match="RUN_SKIP_POLICY_INVALID"):
        run_plan(plan, state_dir=state, approval=approval,
                 approval_fingerprint=digest, allow_new_calls=True,
                 continue_past_failed="yes")
    first = run_plan(plan, state_dir=state, approval=approval,
                     approval_fingerprint=digest, allow_new_calls=True,
                     stop_after=1)
    assert first["status"] == "PAUSED" and sdk.calls == 1
    checkpoint = state / "checkpoints" / f"{plan.selected[0].extraction_window_id}.json"
    checkpoint.unlink()
    with pytest.raises(BudgetError, match="CHECKPOINT_MISSING_FOR_EXTRACTED"):
        run_plan(plan, state_dir=state, approval=approval,
                 approval_fingerprint=digest, allow_new_calls=True,
                 continue_past_failed=True)
    assert sdk.calls == 1
    assert not (state / "private_review/queue.json").exists()


def test_schema_recorder_is_part_of_runtime_code_fingerprint():
    assert "src/dynamic_memory/llm/recording.py" in CODE_FILES
    assert "src/dynamic_memory/llm/structured_output.py" in CODE_FILES


def test_stale_approval_fingerprint_fails_before_any_fake_sdk_call(tmp_path):
    sdk = FakeSDK()
    plan = _plan_for_fake(sdk)
    _, _, approval, digest = approved_plan(tmp_path)
    stale = approval.model_copy(update={
        "run_fingerprint": "sha256:" + "f" * 64,
    })
    with pytest.raises(BudgetError, match="APPROVAL_INVALID_OR_EXPIRED"):
        run_plan(plan, state_dir=tmp_path / "state", approval=stale,
                 approval_fingerprint=digest, allow_new_calls=True,
                 continue_past_failed=True)
    assert sdk.calls == 0


def test_provider_wide_failure_does_not_spend_remaining_windows(tmp_path):
    sdk = FailingFourthSDK()
    plan = _plan_for_fake(sdk)
    _, _, approval, digest = approved_plan(tmp_path)
    state = tmp_path / "state"
    result = run_plan(
        plan, state_dir=state, approval=approval,
        approval_fingerprint=digest, allow_new_calls=True,
        continue_past_failed=True,
    )
    assert result["status"] == "FAILED"
    assert result["error_code"] == "API_STATUS"
    assert result["safe_status_code"] == 402
    assert result["network_calls"] == 4
    assert sdk.calls == 4  # DO NOT attempt remaining 59 windows.
    attempt_rows = BatchBudgetLedger(state / "budget", approval, digest).window_rows()
    assert attempt_rows[-1]["error_code"] == "API_STATUS"
    again = run_plan(
        plan, state_dir=state, approval=approval,
        approval_fingerprint=digest, allow_new_calls=True,
        continue_past_failed=True,
    )
    assert again["status"] == "BUDGET_ATTEMPT_SPENT"
    assert again["network_calls"] == 0
    assert sdk.calls == 4


def test_legacy_failure_without_categorized_error_is_not_skippable(tmp_path):
    sdk = FakeSDK()
    plan = _plan_for_fake(sdk)
    _, _, approval, digest = approved_plan(tmp_path)
    state = tmp_path / "state"
    ledger = BatchBudgetLedger(state / "budget", approval, digest)
    window = plan.selected[0]
    call_fp = plan.call_fingerprints[window.extraction_window_id]
    ledger.reserve(window_id=window.extraction_window_id,
        call_fingerprint=call_fp, prompt_token_ceiling=934464,
        completion_token_ceiling=65536,
        input_bytes=100, input_tokens_estimated=120)
    ledger.record(call_fp, state="failed",
                  safe_reason="UNKNOWN", prompt_tokens=100,
                  completion_tokens=80, reasoning_tokens=10,
                  finish_reason="stop", response_model_id="deepseek-flash",
                  http_response_received=True)
    assert ledger.window_rows()[0]["error_code"] is None
    result = run_plan(
        plan, state_dir=state, approval=approval,
        approval_fingerprint=digest, allow_new_calls=True,
        continue_past_failed=True,
    )
    assert result["status"] == "BUDGET_ATTEMPT_SPENT"
    assert result["network_calls"] == 0 and sdk.calls == 0


def test_prompt_v4_explicit_time_resolution_enums_in_actual_sdk_request():
    from dynamic_memory.memory.extraction import MEMORY_DRAFT_PROMPT, build_extraction_request
    from dynamic_memory.contracts.models import TimeSpan

    assert MEMORY_DRAFT_PROMPT.version == "4"
    plan = _plan_for_fake(FakeSDK())
    request = build_extraction_request(plan.selected[0], plan.settings.models)
    system_text = request.messages[0].content
    assert "resolution is explicit/relative/unknown" in system_text
    assert "precision is instant/day/month/year/unknown" in system_text
    assert "event_time is null unless a complete interval is evidenced" in system_text
    assert "start strictly before end_exclusive" in system_text
    assert "entity, attribute and value are strings or null" in system_text
    assert "numbers are strings" in system_text
    assert request.settings.omit_max_tokens is True
    assert TimeSpan.model_validate({
        "raw": "next month", "start": None, "end_exclusive": None,
        "precision": "month", "resolution": "relative",
    }).resolution == "relative"
    assert TimeSpan.model_validate({
        "raw": None, "start": None, "end_exclusive": None,
        "precision": "unknown", "resolution": "unknown",
    }).resolution == "unknown"
