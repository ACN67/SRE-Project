"""Production Web composition exposes only the configured sample set."""

from pathlib import Path
import sys

import pytest

from dynamic_memory.integration import production
from dynamic_memory.integration.production import (
    ProductionConfigurationError,
    ProductionWebService,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))

from test_dynamic_memory_service import REF, SAMPLE, memory, service


def test_production_web_service_exposes_ordered_multi_sample_configuration():
    wrapped = ProductionWebService(service(), (SAMPLE, "conv-26"))

    assert wrapped.list_samples() == [SAMPLE, "conv-26"]
    assert wrapped.list_memories(SAMPLE) == [memory()]
    assert wrapped.get_memory(SAMPLE, memory().memory_id) == memory()
    assert wrapped.get_turn(SAMPLE, REF).dia_id == REF.dia_id

    with pytest.raises(ProductionConfigurationError) as exc:
        wrapped.list_memories("conv-30")
    assert exc.value.code == "SAMPLE_NOT_EXPOSED"


def test_production_web_service_caches_immutable_session_reads():
    base = service()
    calls = {
        "status": 0,
        "query_state": 0,
        "memories": 0,
        "memory": 0,
        "turn": 0,
    }
    original_status = base.get_build_status
    original_query_state = base.get_query_state
    original_memories = base.list_memories
    original_memory = base.get_memory
    original_turn = base.get_turn

    def status(sample):
        calls["status"] += 1
        return original_status(sample)

    def query_state(sample):
        calls["query_state"] += 1
        return original_query_state(sample)

    def memories(sample):
        calls["memories"] += 1
        return original_memories(sample)

    def get_one_memory(sample, memory_id):
        calls["memory"] += 1
        return original_memory(sample, memory_id)

    def turn(sample, ref):
        calls["turn"] += 1
        return original_turn(sample, ref)

    base.get_build_status = status
    base.get_query_state = query_state
    base.list_memories = memories
    base.get_memory = get_one_memory
    base.get_turn = turn
    wrapped = ProductionWebService(base, (SAMPLE,))

    assert wrapped.get_build_status(SAMPLE) == wrapped.get_build_status(SAMPLE)
    assert wrapped.get_query_state(SAMPLE) == wrapped.get_query_state(SAMPLE)
    first = wrapped.list_memories(SAMPLE)
    second = wrapped.list_memories(SAMPLE)
    assert first == second
    first[0].content = "mutated cached list"
    assert wrapped.list_memories(SAMPLE)[0].content != "mutated cached list"
    memory_id = memory().memory_id
    assert wrapped.get_memory(SAMPLE, memory_id) == wrapped.get_memory(
        SAMPLE, memory_id
    )
    assert wrapped.get_turn(SAMPLE, REF) == wrapped.get_turn(SAMPLE, REF)
    assert calls == {
        "status": 1,
        "query_state": 1,
        "memories": 1,
        "memory": 0,
        "turn": 1,
    }


def test_production_web_service_rejects_empty_or_duplicate_samples():
    for value in ((), ("",), ("conv-26", "conv-26")):
        with pytest.raises(ProductionConfigurationError) as exc:
            ProductionWebService(service(), value)
        assert exc.value.code == "SAMPLE_CONFIGURATION_INVALID"


def test_multi_sample_environment_uses_index_root_and_explicit_order(monkeypatch, tmp_path):
    root = tmp_path / "indexes"
    monkeypatch.setenv("SRE_PRODUCTION_SAMPLE_IDS", "conv-30, conv-26")
    monkeypatch.setenv("SRE_PRODUCTION_INDEX_ROOT", str(root))
    monkeypatch.delenv("SRE_PRODUCTION_INDEX_PATH", raising=False)
    monkeypatch.setenv("SRE_PRODUCTION_ALLOWED_SPLITS", "main_dev,test")

    samples = production._configured_samples()
    paths = production._configured_index_paths(samples)

    assert samples == ("conv-30", "conv-26")
    assert paths == {
        "conv-30": root.resolve() / "conv-30.json",
        "conv-26": root.resolve() / "conv-26.json",
    }
    assert production._configured_splits() == ("main_dev", "test")


def test_legacy_single_sample_index_path_remains_supported(monkeypatch, tmp_path):
    index = tmp_path / "conv-30.json"
    monkeypatch.delenv("SRE_PRODUCTION_SAMPLE_IDS", raising=False)
    monkeypatch.setenv("SRE_PRODUCTION_SAMPLE_ID", "conv-30")
    monkeypatch.setenv("SRE_PRODUCTION_INDEX_PATH", str(index))
    monkeypatch.delenv("SRE_PRODUCTION_INDEX_ROOT", raising=False)

    samples = production._configured_samples()
    assert production._configured_index_paths(samples) == {
        "conv-30": index.resolve()
    }


def test_conflicting_index_configuration_and_multi_legacy_paths_fail_closed(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("SRE_PRODUCTION_INDEX_ROOT", str(tmp_path / "indexes"))
    monkeypatch.setenv("SRE_PRODUCTION_INDEX_PATH", str(tmp_path / "one.json"))
    with pytest.raises(ProductionConfigurationError) as exc:
        production._configured_index_paths(("conv-30",))
    assert exc.value.code == "PRODUCTION_CONFIGURATION_CONFLICT"

    monkeypatch.delenv("SRE_PRODUCTION_INDEX_ROOT")
    with pytest.raises(ProductionConfigurationError) as exc:
        production._configured_index_paths(("conv-30", "conv-26"))
    assert exc.value.code == "PRODUCTION_CONFIGURATION_REQUIRED"


def test_multi_sample_system_rejects_mismatched_index_keys(tmp_path):
    with pytest.raises(ProductionConfigurationError) as exc:
        production.create_multi_sample_system(
            artifact_root=tmp_path / "artifacts",
            index_paths={"conv-30": tmp_path / "conv-30.json"},
            model_root=tmp_path / "model",
            model_revision="a" * 40,
            sample_ids=("conv-30", "conv-26"),
        )
    assert exc.value.code == "SAMPLE_CONFIGURATION_INVALID"
