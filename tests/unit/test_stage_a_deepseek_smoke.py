"""No-network safety checks for the explicitly paid Stage A entry point."""

import builtins
import json

import pytest

from scripts import stage_a_deepseek_smoke as smoke


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(smoke, "EVIDENCE", tmp_path)
    monkeypatch.setattr(smoke, "LEDGER", tmp_path / "api_attempts.json")
    monkeypatch.setattr(smoke, "LOCK", tmp_path / "api_attempts.lock")
    monkeypatch.setattr(smoke, "read_key", lambda: "secret-for-test")


@pytest.mark.parametrize("flags", [[], ["--live"], ["--confirm-low-cost"]])
def test_default_and_single_switch_never_call(monkeypatch, capsys, isolated, flags):
    monkeypatch.setattr(smoke, "GuardedLLMAdapter", lambda *a, **k: pytest.fail("network setup"))
    assert smoke.main(flags) == 0
    assert json.loads(capsys.readouterr().out)["network_calls"] == 0
    assert not smoke.LEDGER.exists()


def test_missing_key_never_claims_attempt(monkeypatch, capsys, isolated):
    monkeypatch.setattr(smoke, "read_key", lambda: None)
    assert smoke.main(["--live", "--confirm-low-cost"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "KEY_MISSING"
    assert not smoke.LEDGER.exists()


def test_sdk_missing_never_claims_attempt(monkeypatch, capsys, isolated):
    original = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "openai":
            raise ImportError
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    assert smoke.main(["--live", "--confirm-low-cost"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "SDK_MISSING"
    assert not smoke.LEDGER.exists()


def test_budget_persists_across_invocations(isolated):
    assert smoke.claim_attempt() == 1
    assert smoke.claim_attempt() == 2
    with pytest.raises(RuntimeError, match="BUDGET_EXHAUSTED"):
        smoke.claim_attempt()
    assert smoke.attempts_used() == 2


def test_key_not_reported_in_dry_run(capsys, isolated):
    smoke.main([])
    assert "secret-for-test" not in capsys.readouterr().out


def test_probe_parameters_and_json_mode():
    text = smoke.make_request("text")
    data = smoke.make_request("json")
    assert text.model_id == data.model_id == "deepseek-flash"
    assert text.settings.thinking == data.settings.thinking == "disabled"
    assert text.settings.max_output_tokens <= 128
    assert data.settings.max_output_tokens <= 256
    assert data.output_mode == "json"
    assert "json" in data.messages[0].content
