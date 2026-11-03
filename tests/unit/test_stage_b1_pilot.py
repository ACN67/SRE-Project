"""One-window pilot never calls the provider without both switches and its own slot."""

import json

import pytest

from scripts import stage_b1_pilot as pilot


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(pilot, "EVIDENCE", tmp_path)
    monkeypatch.setattr(pilot, "LEDGER", tmp_path / "b1_api_attempts.json")
    monkeypatch.setattr(pilot, "LOCK", tmp_path / "b1_run.lock")
    monkeypatch.setattr(pilot, "prepare", lambda: (None,) * 6)
    monkeypatch.setattr(pilot, "metadata", lambda *args: {"window_id": "synthetic_window"})
    monkeypatch.setattr(pilot, "read_key", lambda: "synthetic-secret")


@pytest.mark.parametrize("switches", [[], ["--live"], ["--confirm-low-cost"]])
def test_dry_run_does_not_claim_or_reveal_key(capsys, isolated, switches):
    assert pilot.main(switches) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["network_calls"] == 0
    assert "synthetic-secret" not in output
    assert not pilot.LEDGER.exists()


def test_missing_key_never_spends(capsys, monkeypatch, isolated):
    monkeypatch.setattr(pilot, "read_key", lambda: None)
    assert pilot.main(["--live", "--confirm-low-cost"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "KEY_MISSING"
    assert not pilot.LEDGER.exists()


def test_one_slot_persists_and_second_run_rejected(capsys, isolated):
    pilot.claim()
    assert pilot.used() == 1
    with pytest.raises(RuntimeError, match="BUDGET_EXHAUSTED"):
        pilot.claim()
    assert pilot.main(["--live", "--confirm-low-cost"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "BUDGET_EXHAUSTED"


def test_bad_ledger_fails_closed(isolated):
    pilot.LEDGER.write_text('{"attempts": 2}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="LEDGER_INVALID"):
        pilot.used()
