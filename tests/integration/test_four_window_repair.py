"""Offline-only coverage for targeted repair and cross-run provenance."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dynamic_memory.memory import repair_lineage as lineage
from dynamic_memory.memory.batch_budget import BatchApproval, BudgetError, load_approval
from dynamic_memory.memory.extraction import AtomicFactDraftBatch, MEMORY_DRAFT_PROMPT, WindowExtractionResult
from scripts.stage_b_repair_runner import prepare_plan

REAL_DATA = Path(__file__).resolve().parents[2] / "data/raw/locomo10.json"
requires_real_data = pytest.mark.skipif(
    not REAL_DATA.is_file(),
    reason="official LoCoMo raw data is intentionally Git-ignored",
)

@requires_real_data
def test_repair_plan_is_exactly_four_failed_original_windows():
    plan = prepare_plan(mode="repair4")
    assert len(plan.windows) == 63
    assert len(plan.selected) == 4
    assert {w.extraction_window_id for w in plan.selected} == lineage.FAILED_WINDOW_IDS
    assert plan.settings.models.max_retries == 0
    assert plan.settings.models.max_concurrent_api_requests == 1
    assert plan.settings.models.extraction_omit_max_tokens is True


@requires_real_data
def test_four_window_approval_requires_exact_scope_and_budget(tmp_path):
    plan = prepare_plan(mode="repair4")
    now = datetime.now(timezone.utc)
    ids = tuple(w.extraction_window_id for w in plan.selected)
    approval = BatchApproval(authorization_id="test-repair-four-window-01",
        approved_by="test-reviewer", mode="repair4", sample_id="conv-30",
        dataset_sha256=plan.bundle.snapshot_sha256,
        run_fingerprint=plan.run_fingerprint, plan_fingerprint=plan.plan_fingerprint,
        selected_window_ids=ids, output_policy="provider_default_64k",
        max_new_attempts=4, max_prompt_tokens_total=4*934464,
        max_completion_tokens_total=4*65536, max_usd="1.4359296",
        input_usd_per_million="0.30", output_usd_per_million="1.20",
        price_checked_on=now.date(), provider_default_checked_on=now.date(),
        expires_at=now+timedelta(hours=1))
    path = tmp_path / "approval.json"
    path.write_text(approval.model_dump_json(), encoding="utf-8")
    loaded, _ = load_approval(path, mode="repair4", sample_id="conv-30",
        dataset_sha256=plan.bundle.snapshot_sha256,
        run_fingerprint=plan.run_fingerprint, plan_fingerprint=plan.plan_fingerprint,
        authorization_id=approval.authorization_id, selected_window_ids=ids, now=now)
    assert loaded == approval
    for bad_ids in (ids[:-1], tuple(reversed(ids))):
        with pytest.raises(BudgetError, match="APPROVAL_INVALID_OR_EXPIRED"):
            load_approval(path, mode="repair4", sample_id="conv-30",
                dataset_sha256=plan.bundle.snapshot_sha256,
                run_fingerprint=plan.run_fingerprint, plan_fingerprint=plan.plan_fingerprint,
                authorization_id=approval.authorization_id, selected_window_ids=bad_ids, now=now)
    with pytest.raises(BudgetError, match="APPROVAL_INVALID_OR_EXPIRED"):
        load_approval(path, mode="repair4", sample_id="conv-30",
            dataset_sha256=plan.bundle.snapshot_sha256,
            run_fingerprint=plan.run_fingerprint, plan_fingerprint=plan.plan_fingerprint,
            authorization_id=approval.authorization_id, selected_window_ids=ids,
            now=now+timedelta(hours=2))


@pytest.mark.parametrize("bad_time", [
    {"raw": "unknown", "start": "2020-01-01T00:00:00", "end_exclusive": None},
    {"raw": "unknown", "start": "2020-02-01T00:00:00", "end_exclusive": "2020-01-01T00:00:00"},
    {"raw": "unknown", "start": "2020-01-01T00:00:00+00:00", "end_exclusive": "2020-01-02T00:00:00"},
])
def test_invalid_time_is_not_silently_repaired(bad_time):
    fact = {"content": "Example", "entity": None, "attribute": None, "value": None,
            "fact_kind": "event", "assertion_type": "fact", "polarity": "positive",
            "recorded_at": None, "event_time": bad_time,
            "source_refs": [{"session_id": "s", "dia_id": "d"}]}
    with pytest.raises(Exception):
        AtomicFactDraftBatch.model_validate({"facts": [fact]})


def test_unknown_fact_kind_rejected_and_genuine_zero_fact_batch_valid():
    fact = {"content": "Example", "entity": None, "attribute": None, "value": None,
            "fact_kind": "activity", "assertion_type": "fact", "polarity": "positive",
            "recorded_at": None, "event_time": None,
            "source_refs": [{"session_id": "s", "dia_id": "d"}]}
    with pytest.raises(Exception):
        AtomicFactDraftBatch.model_validate({"facts": [fact]})
    assert AtomicFactDraftBatch.model_validate({"facts": []}).facts == []


@requires_real_data
def test_cross_run_merge_has_63_unique_origin_entries_and_rejects_missing(monkeypatch, tmp_path):
    plan = prepare_plan(mode="repair4")
    old_root = tmp_path / "old"
    new_root = tmp_path / "new"
    (old_root / "checkpoints").mkdir(parents=True)
    new_root.mkdir()
    monkeypatch.setattr(lineage, "OLD_ROOT", old_root)
    old_run = lineage.OLD_RUN
    old_prompt = "sha256:" + "1" * 64
    baseline = {"old_prompt_fingerprint": old_prompt,
                "old_code_fingerprint": "sha256:" + "2" * 64}
    old_results, new_results, old_rows, new_rows = {}, {}, {}, {}
    for w in plan.windows:
        wid = w.extraction_window_id
        if wid in lineage.FAILED_WINDOW_IDS:
            new_results[wid] = WindowExtractionResult(plan.sample_id, wid, (),
                MEMORY_DRAFT_PROMPT.template_fingerprint, 1, "checkpoint")
            new_rows[wid] = {"state": "extracted", "call_fingerprint": plan.call_fingerprints[wid]}
            (new_root / (wid + ".json")).write_bytes(wid.encode())
        else:
            old_results[wid] = WindowExtractionResult(plan.sample_id, wid, (), old_prompt, 1, "checkpoint")
            old_rows[wid] = {"state": "extracted", "call_fingerprint": "sha256:" + "3" * 64}
            (old_root / "checkpoints" / (wid + ".json")).write_bytes(wid.encode())
    manifest, queue = lineage.merge_result_sets(plan, old_results, new_results,
        old_rows, new_rows, baseline, "test-new-authorization", "a" * 64, new_root)
    assert len(manifest["body"]["windows"]) == len(queue.windows) == 63
    assert [x["window_id"] for x in manifest["body"]["windows"]] == [
        w.extraction_window_id for w in plan.windows]
    assert len({x["window_id"] for x in manifest["body"]["windows"]}) == 63
    assert {x["origin_run_fingerprint"] for x in manifest["body"]["windows"]} == {
        old_run, plan.run_fingerprint}
    missing = dict(new_results)
    missing.pop(next(iter(missing)))
    with pytest.raises(lineage.LineageError):
        lineage.merge_result_sets(plan, old_results, missing, old_rows,
            new_rows, baseline, "test-new-authorization", "a" * 64, new_root)
    overlapping = dict(new_results)
    overlapping[next(iter(old_results))] = next(iter(new_results.values()))
    with pytest.raises(lineage.LineageError):
        lineage.merge_result_sets(plan, old_results, overlapping, old_rows,
            new_rows, baseline, "test-new-authorization", "a" * 64, new_root)
