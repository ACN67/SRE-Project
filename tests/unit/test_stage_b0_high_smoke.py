"""The High live entry is offline unless both switches and safe prerequisites hold."""

import json

import pytest

from scripts import stage_b0_high_smoke as smoke


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(smoke, "EVIDENCE", tmp_path)
    monkeypatch.setattr(smoke, "LEDGER", tmp_path / "high_api_attempts.json")
    monkeypatch.setattr(smoke, "LOCK", tmp_path / "high_api.lock")
    monkeypatch.setattr(smoke, "read_key", lambda: "synthetic-secret")


@pytest.mark.parametrize("flags", [[], ["--live"], ["--confirm-low-cost"]])
def test_dry_run_requires_both_switches(capsys, isolated, flags):
    assert smoke.main(flags) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["network_calls"] == 0
    assert "synthetic-secret" not in output
    assert not smoke.LEDGER.exists()


def test_key_missing_never_claims(capsys, monkeypatch, isolated):
    monkeypatch.setattr(smoke, "read_key", lambda: None)
    assert smoke.main(["--live", "--confirm-low-cost"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "KEY_MISSING"
    assert not smoke.LEDGER.exists()


def test_two_attempt_cap_persists_and_live_rejects_third(capsys, isolated):
    assert smoke.claim() == 1
    assert smoke.claim() == 2
    with pytest.raises(RuntimeError, match="BUDGET_EXHAUSTED"):
        smoke.claim()
    assert smoke.main(["--live", "--confirm-low-cost"]) == 2
    assert json.loads(capsys.readouterr().out)["network_calls"] == 0


def test_only_high_flash_bounded_synthetic_requests():
    for kind in ("text", "json"):
        call = smoke.request(kind)
        assert call.model_id == "deepseek-flash"
        assert call.settings.thinking == "enabled"
        assert call.settings.reasoning_effort == "high"
        assert call.settings.max_input_tokens == 200
        assert 512 <= call.settings.max_output_tokens <= 4096
        assert sum(len(message.content) for message in call.messages) < 200
