"""Offline, synthetic checks for snapshot identity and fixed split semantics."""

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from dynamic_memory.data.snapshot import (
    DataSnapshotError,
    freeze_split_config,
    inspect_snapshot,
    main,
    validate_split,
)
from dynamic_memory.pipeline.settings import DataSplitSettings, load_settings


@pytest.fixture
def sample(tmp_path):
    path = tmp_path / "synthetic.json"
    items = [{"sample_id": f"id-{i}", "conversation": {"session_1": []}, "qa": "SECRET_GOLD"} for i in range(10)]
    raw = json.dumps(items, indent=1).encode()
    path.write_bytes(raw)
    return path, items, raw


@pytest.fixture
def split_file(tmp_path):
    path = tmp_path / "data_split.yaml"
    path.write_text(
        "snapshot_sha256: null\nsample_ids: null\ngroups:\n"
        "  main_dev: {source_order_1_based: [1, 2, 3, 4], sample_ids: null}\n"
        "  extra_dev: {source_order_1_based: [5, 6, 7, 8], sample_ids: null}\n"
        "  test: {source_order_1_based: [9, 10], sample_ids: null}\n", encoding="utf-8"
    )
    return path


def assert_code(code, fn, *args):
    with pytest.raises(DataSnapshotError) as error:
        fn(*args)
    assert error.value.code == code
    assert "SECRET_GOLD" not in str(error.value)


def test_inspect_and_freeze(sample, split_file, tmp_path):
    source, _, raw = sample
    info = inspect_snapshot(source)
    assert info.sha256 == hashlib.sha256(raw).hexdigest()
    assert info.sample_ids == tuple(f"id-{i}" for i in range(10))
    original = split_file.read_bytes()
    preview = freeze_split_config(info, split_file)
    assert split_file.read_bytes() == original
    assert_code("SPLIT_UNFROZEN", validate_split, info, DataSplitSettings.model_validate(yaml.safe_load(original)))
    assert preview.groups.main_dev.sample_ids == [f"id-{i}" for i in range(4)]
    assert preview.groups.extra_dev.sample_ids == [f"id-{i}" for i in range(4, 8)]
    assert preview.groups.test.sample_ids == ["id-8", "id-9"]
    frozen = freeze_split_config(info, split_file, apply=True)
    validate_split(info, frozen)
    assert yaml.safe_load(split_file.read_text(encoding="utf-8"))["sample_ids"] == list(info.sample_ids)
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    originals = Path(__file__).resolve().parents[2] / "configs"
    for name in ("models", "evaluation", "extraction", "retrieval", "reasoning"):
        (config_dir / f"{name}.yaml").write_bytes((originals / f"{name}.yaml").read_bytes())
    (config_dir / "data_split.yaml").write_bytes(split_file.read_bytes())
    assert load_settings(config_dir).data_split.sample_ids == list(info.sample_ids)
    changed = replace(info, sha256="0" * 64)
    assert_code("DATA_SNAPSHOT_MISMATCH", freeze_split_config, changed, split_file)


@pytest.mark.parametrize("field", ["snapshot_sha256", "sample_ids", "main_dev", "extra_dev", "test"])
def test_any_null_rejected(sample, split_file, field):
    info = inspect_snapshot(sample[0])
    config = freeze_split_config(info, split_file)
    data = config.model_dump()
    if field in data:
        data[field] = None
    else:
        data["groups"][field]["sample_ids"] = None
    assert_code("SPLIT_UNFROZEN", validate_split, info, DataSplitSettings.model_validate(data))


@pytest.mark.parametrize("mutation,code", [
    (lambda d: d.update(snapshot_sha256="0" * 64), "DATA_SNAPSHOT_MISMATCH"),
    (lambda d: d.update(snapshot_sha256="A" * 64), "SPLIT_MISMATCH"),
    (lambda d: d["sample_ids"].reverse(), "SPLIT_MISMATCH"),
    (lambda d: d["sample_ids"].__setitem__(0, "1"), "SPLIT_MISMATCH"),
    (lambda d: d["groups"]["test"]["sample_ids"].reverse(), "SPLIT_MISMATCH"),
    (lambda d: d["groups"]["test"]["sample_ids"].__setitem__(0, "id-0"), "SPLIT_MISMATCH"),
    (lambda d: d["groups"]["main_dev"]["source_order_1_based"].reverse(), "SPLIT_MISMATCH"),
])
def test_split_mismatches(sample, split_file, mutation, code):
    info = inspect_snapshot(sample[0])
    data = freeze_split_config(info, split_file).model_dump()
    mutation(data)
    assert_code(code, validate_split, info, DataSplitSettings.model_validate(data))


@pytest.mark.parametrize("change,code", [
    (lambda d: d.pop(), "DATA_FORMAT_INVALID"),
    (lambda d: d.__setitem__(1, d[0]), "DUPLICATE_SAMPLE_ID"),
    (lambda d: d[0].pop("sample_id"), "DATA_FORMAT_INVALID"),
    (lambda d: d[0].__setitem__("sample_id", "  "), "DATA_FORMAT_INVALID"),
    (lambda d: d[0].pop("conversation"), "DATA_FORMAT_INVALID"),
    (lambda d: d[0].__setitem__("conversation", []), "DATA_FORMAT_INVALID"),
])
def test_bad_sample_shape(sample, change, code):
    path, items, _ = sample
    change(items)
    path.write_text(json.dumps(items), encoding="utf-8")
    assert_code(code, inspect_snapshot, path)


@pytest.mark.parametrize("raw", [b"\xff", b"{", b"{}", b"[]", b"SECRET_GOLD"])
def test_bad_file_format(sample, raw):
    path = sample[0]
    path.write_bytes(raw)
    assert_code("DATA_FORMAT_INVALID", inspect_snapshot, path)


def test_missing_file(sample):
    sample[0].unlink()
    assert_code("DATA_FILE_NOT_FOUND", inspect_snapshot, sample[0])


def test_partial_config_cannot_be_overwritten(sample, split_file):
    info = inspect_snapshot(sample[0])
    document = yaml.safe_load(split_file.read_text(encoding="utf-8"))
    document["sample_ids"] = list(info.sample_ids)
    split_file.write_text(yaml.safe_dump(document), encoding="utf-8")
    before = split_file.read_bytes()
    assert_code("SPLIT_UNFROZEN", freeze_split_config, info, split_file)
    assert split_file.read_bytes() == before


def test_cli_read_only_and_no_gold(sample, split_file, capsys):
    path = sample[0]
    before = split_file.read_bytes()
    assert main([str(path), "--config", str(split_file)]) == 2
    output = capsys.readouterr().out
    assert "unfrozen" in output and "id-0" in output
    assert "SECRET_GOLD" not in output
    assert split_file.read_bytes() == before
    assert main([str(path), "--config", str(split_file), "--freeze"]) == 0
    assert main([str(path), "--config", str(split_file)]) == 0
    assert "SECRET_GOLD" not in capsys.readouterr().out
