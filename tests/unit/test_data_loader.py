"""Synthetic-only B-02 loader tests. No official conversations or Gold fixtures."""

import hashlib
import json

import pytest
import yaml

from dynamic_memory.contracts import DatasetBundle, render_visible_turn
from dynamic_memory.data import load_conversations
from dynamic_memory.data.loader import _parse_json, _parse_sample
from dynamic_memory.data.snapshot import DataSnapshotError, freeze_split_config, inspect_snapshot


def turn(dia_id="d1", text="", **extra):
    return {"speaker": "A", "dia_id": dia_id, "text": text, **extra}


def item(sample_id="synthetic-0"):
    return {
        "sample_id": sample_id,
        "conversation": {
            "speaker_a": "unused", "speaker_b": "unused",
            "session_10": [turn("d10", "literal text", blip_caption="synthetic caption", img_url="SECRET_GOLD_URL", query="SECRET_GOLD_QUERY", **{"re-download": "SECRET_GOLD_EXTRA"})],
            "session_10_date_time": "7:15 pm on 12 August, 2023",
            "session_2": [turn("d2", "")],
            "session_99_date_time": "bad orphan is ignored",
        },
        "qa": {"answer": "SECRET_GOLD_ANSWER", "evidence": "SECRET_GOLD_EVIDENCE"},
        "observation": "SECRET_GOLD_OBSERVATION",
        "session_summary": "SECRET_GOLD_SESSION_SUMMARY",
        "event_summary": "SECRET_GOLD_EVENT_SUMMARY",
    }


@pytest.fixture
def frozen(tmp_path):
    source = tmp_path / "locomo10.json"
    source.write_bytes(json.dumps([item(f"synthetic-{i}") for i in range(10)]).encode())
    config = tmp_path / "data_split.yaml"
    config.write_text(
        "snapshot_sha256: null\nsample_ids: null\ngroups:\n"
        "  main_dev: {source_order_1_based: [1, 2, 3, 4], sample_ids: null}\n"
        "  extra_dev: {source_order_1_based: [5, 6, 7, 8], sample_ids: null}\n"
        "  test: {source_order_1_based: [9, 10], sample_ids: null}\n", encoding="utf-8"
    )
    freeze_split_config(inspect_snapshot(source), config, apply=True)
    return source, config


def fail(code, operation, *args):
    with pytest.raises(DataSnapshotError) as error:
        operation(*args)
    assert error.value.code == code
    message = str(error.value)
    assert "SECRET_GOLD_" not in message
    assert "literal text" not in message


def test_pure_parse_preserves_order_caption_time_and_filters_gold():
    sample = _parse_sample(item(), 1)
    assert [session.session_id for session in sample.sessions] == ["session_10", "session_2"]
    assert len(sample.sessions) == 2
    first = sample.sessions[0].turns[0]
    assert first.sample_id == "synthetic-0" and first.dia_id == "d10"
    assert first.text == "literal text"
    assert first.recorded_at.isoformat() == "2023-08-12T19:15:00"
    assert first.recorded_at.tzinfo is None
    assert first.image_caption == "synthetic caption" and first.caption_origin == "blip_caption"
    assert render_visible_turn(first) == "literal text [image_caption: synthetic caption]"
    assert sample.sessions[1].turns[0].text == ""
    assert sample.sessions[1].turns[0].recorded_at is None
    serialized = sample.model_dump_json()
    assert "SECRET_GOLD_" not in serialized and "img_url" not in serialized


def test_empty_session_unknown_and_iso_offset():
    raw = item()
    raw["conversation"] = {
        "session_1": [], "session_1_date_time": "unknown",
        "session_2": [turn()], "session_2_date_time": "2023-08-12T19:15:00+05:30",
    }
    sample = _parse_sample(raw, 1)
    assert sample.sessions[0].turns == []
    assert sample.sessions[1].turns[0].recorded_at.isoformat() == "2023-08-12T19:15:00+05:30"


@pytest.mark.parametrize("mutate,code", [
    (lambda x: x["conversation"]["session_10"].append(turn("d10")), "DUPLICATE_SOURCE_REF"),
    (lambda x: x["conversation"]["session_10"][0].pop("speaker"), "DATA_FORMAT_INVALID"),
    (lambda x: x["conversation"]["session_10"][0].pop("dia_id"), "DATA_FORMAT_INVALID"),
    (lambda x: x["conversation"]["session_10"][0].pop("text"), "DATA_FORMAT_INVALID"),
    (lambda x: x["conversation"]["session_10"][0].__setitem__("dia_id", " "), "DATA_FORMAT_INVALID"),
    (lambda x: x["conversation"]["session_10"].__setitem__(0, "SECRET_GOLD_BAD_TURN"), "DATA_FORMAT_INVALID"),
    (lambda x: x["conversation"].__setitem__("session_10", {}), "DATA_FORMAT_INVALID"),
    (lambda x: x["conversation"].__setitem__("session_0", []), "DATA_FORMAT_INVALID"),
    (lambda x: x["conversation"].__setitem__("session_10_date_time", "32:99 pm on 12 August, 2023"), "DATA_FORMAT_INVALID"),
    (lambda x: x["conversation"]["session_10"][0].__setitem__("blip_caption", 5), "DATA_FORMAT_INVALID"),
])
def test_pure_parse_rejects_bad_source(mutate, code):
    raw = item()
    mutate(raw)
    fail(code, _parse_sample, raw, 1)


def test_full_loader_split_and_roundtrip(frozen):
    source, config = frozen
    bundle = load_conversations(str(source), str(config))
    assert isinstance(bundle, DatasetBundle)
    assert bundle.snapshot_sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert [s.sample_id for s in bundle.samples] == [f"synthetic-{i}" for i in range(10)]
    assert [s.source_order_1_based for s in bundle.samples] == list(range(1, 11))
    assert list(bundle.split_by_sample_id.values()) == ["main_dev"] * 4 + ["extra_dev"] * 4 + ["test"] * 2
    encoded = bundle.model_dump_json()
    assert "SECRET_GOLD_" not in encoded
    assert DatasetBundle.model_validate_json(encoded) == bundle


def test_missing_and_unfrozen(frozen):
    source, config = frozen
    fail("DATA_FILE_NOT_FOUND", load_conversations, str(source.parent / "missing.json"), str(config))
    document = yaml.safe_load(config.read_text(encoding="utf-8"))
    document["groups"]["test"]["sample_ids"] = None
    config.write_text(yaml.safe_dump(document), encoding="utf-8")
    fail("SPLIT_UNFROZEN", load_conversations, str(source), str(config))


def test_snapshot_and_split_tamper(frozen):
    source, config = frozen
    source.write_bytes(source.read_bytes() + b" ")
    fail("DATA_SNAPSHOT_MISMATCH", load_conversations, str(source), str(config))
    source.write_bytes(source.read_bytes()[:-1])
    document = yaml.safe_load(config.read_text(encoding="utf-8"))
    document["sample_ids"].reverse()
    config.write_text(yaml.safe_dump(document), encoding="utf-8")
    fail("SPLIT_MISMATCH", load_conversations, str(source), str(config))


@pytest.mark.parametrize("mutation,code", [
    (lambda d: d["groups"]["main_dev"]["sample_ids"].reverse(), "SPLIT_MISMATCH"),
    (lambda d: d["groups"]["test"]["sample_ids"].__setitem__(0, "synthetic-0"), "SPLIT_MISMATCH"),
    (lambda d: d["groups"]["main_dev"]["source_order_1_based"].reverse(), "SPLIT_MISMATCH"),
    (lambda d: d.__setitem__("snapshot_sha256", "0" * 64), "DATA_SNAPSHOT_MISMATCH"),
])
def test_full_loader_rejects_split_changes(frozen, mutation, code):
    source, config = frozen
    document = yaml.safe_load(config.read_text(encoding="utf-8"))
    mutation(document)
    config.write_text(yaml.safe_dump(document), encoding="utf-8")
    fail(code, load_conversations, str(source), str(config))


def test_full_loader_rejects_source_id_reorder_even_with_matching_sha(frozen):
    source, config = frozen
    document = json.loads(source.read_text(encoding="utf-8"))
    document[0], document[1] = document[1], document[0]
    source.write_text(json.dumps(document), encoding="utf-8")
    split = yaml.safe_load(config.read_text(encoding="utf-8"))
    split["snapshot_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    config.write_text(yaml.safe_dump(split), encoding="utf-8")
    fail("SPLIT_MISMATCH", load_conversations, str(source), str(config))


def test_invalid_json_yaml_and_duplicate_raw_key(frozen):
    source, config = frozen
    original_source = source.read_bytes()
    original_config = config.read_bytes()
    config.write_text("groups: [SECRET_GOLD_BAD", encoding="utf-8")
    fail("SPLIT_MISMATCH", load_conversations, str(source), str(config))
    source.write_bytes(b"[")
    fail("DATA_FORMAT_INVALID", load_conversations, str(source), str(config))
    # Snapshot checker validates shape; the loader additionally catches keys
    # that normal json.loads would silently overwrite.
    source.write_bytes(original_source)
    config.write_bytes(original_config)
    text = source.read_text(encoding="utf-8")
    text = text.replace('"speaker_a": "unused"', '"speaker_a": "unused", "speaker_a": "SECRET_GOLD_DUPLICATE"', 1)
    source.write_text(text, encoding="utf-8")
    document = yaml.safe_load(config.read_text(encoding="utf-8"))
    document["snapshot_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    config.write_text(yaml.safe_dump(document), encoding="utf-8")
    fail("DATA_FORMAT_INVALID", load_conversations, str(source), str(config))


def test_duplicate_sample_and_bad_turn_in_full_loader(frozen):
    source, config = frozen
    original_source = source.read_bytes()
    document = json.loads(source.read_text(encoding="utf-8"))
    document[1]["sample_id"] = document[0]["sample_id"]
    source.write_text(json.dumps(document), encoding="utf-8")
    fail("DUPLICATE_SAMPLE_ID", load_conversations, str(source), str(config))
    source.write_bytes(original_source)
    document = json.loads(source.read_text(encoding="utf-8"))
    document[0]["conversation"]["session_10"][0]["text"] = None
    source.write_text(json.dumps(document), encoding="utf-8")
    frozen_config = yaml.safe_load(config.read_text(encoding="utf-8"))
    frozen_config["snapshot_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    config.write_text(yaml.safe_dump(frozen_config), encoding="utf-8")
    fail("DATA_FORMAT_INVALID", load_conversations, str(source), str(config))


def test_deeply_nested_json_parser_error_is_stable_and_redacted():
    nested = "[" * 1100 + '"SECRET_GOLD_DEEP"' + "]" * 1100
    fail("DATA_FORMAT_INVALID", _parse_json, nested.encode("utf-8"))


def test_deeply_nested_json_snapshot_error_is_stable_and_redacted(frozen):
    source, config = frozen
    nested = "[" * 1100 + '"SECRET_GOLD_DEEP"' + "]" * 1100
    raw = source.read_text(encoding="utf-8")
    assert '"SECRET_GOLD_ANSWER"' in raw
    source.write_text(raw.replace('"SECRET_GOLD_ANSWER"', nested, 1), encoding="utf-8")
    fail("DATA_FORMAT_INVALID", load_conversations, str(source), str(config))
