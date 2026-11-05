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
