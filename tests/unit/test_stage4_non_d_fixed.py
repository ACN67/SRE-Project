"""Stage 4 approval and no-network gate tests."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from scripts.stage4_non_d_fixed import (
    Stage4Approval, Stage4Error, _validate_approval,
)


def plan():
    return {
        "run_fingerprint": "sha256:" + "a" * 64,
        "budget": {
            "maximum_new_http_attempts": 12,
            "input_tokens_per_call": 32768,
            "provider_default_output_tokens_per_call": 65536,
            "worst_case_usd_total": "1.0616832",
        },
    }


def approval_data(run_fingerprint=None):
    now = datetime.now(timezone.utc)
    return {
        "format_version": 1,
        "authorization_id": "user-chat-20261104-stage4-test",
        "approved_by": "ACN67",
        "approved_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
        "run_fingerprint": run_fingerprint or "sha256:" + "a" * 64,
        "sample_id": "conv-30",
        "model_id": "deepseek-flash",
        "thinking": "enabled",
        "reasoning_effort": "high",
        "request_max_tokens_omitted": True,
        "max_input_tokens_per_call": 32768,
        "provider_default_output_tokens_per_call": 65536,
        "max_new_http_attempts": 12,
        "retry_attempts": 0,
        "max_concurrency": 1,
        "max_total_usd": "1.0616832",
        "price_checked_on": now.date().isoformat(),
        "authorized_methods": ["full_text", "vector_rag", "pure_vector_topk"],
    }


def test_exact_stage4_approval_is_accepted(tmp_path):
    checked = Stage4Approval.model_validate_json(json.dumps(approval_data()))
    _validate_approval(
        checked,
        plan(),
        tmp_path / "approval.json",
        tmp_path / "run",
    )


def test_stage4_approval_rejects_other_fingerprint(tmp_path):
    checked = Stage4Approval.model_validate_json(
        json.dumps(approval_data("sha256:" + "b" * 64))
    )
    with pytest.raises(Stage4Error, match="APPROVAL_MISMATCH"):
        _validate_approval(
            checked,
            plan(),
            tmp_path / "approval.json",
            tmp_path / "run",
        )
