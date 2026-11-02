"""Synthetic-only question identity and frozen split checks."""

import json

import pytest
import yaml

from dynamic_memory.data.snapshot import freeze_split_config, inspect_snapshot
from evaluation.question_manifest import EvaluationInputError, load_question_manifest


def synthetic_files(tmp_path):
    snapshot = tmp_path / "synthetic-locomo10.json"
    samples = []
    for index in range(10):
        qa = [{"question": "Repeated synthetic question?", "answer": "SECRET_GOLD_ANSWER",
               "evidence": ["SECRET_GOLD_EVIDENCE"], "category": 1}]
        if index == 0:
            qa.append({"question": "Repeated synthetic question?", "answer": "Second synthetic Gold",
                       "evidence": [], "category": 5})
        samples.append({"sample_id": f"synthetic-{index}",
                        "conversation": {"session_1": []}, "qa": qa,
                        "observation": "SECRET_GOLD_OBSERVATION",
                        "session_summary": "SECRET_GOLD_SUMMARY"})
    snapshot.write_text(json.dumps(samples), encoding="utf-8")
    split = tmp_path / "split.yaml"
    split.write_text(
        "snapshot_sha256: null\nsample_ids: null\ngroups:\n"
        "  main_dev: {source_order_1_based: [1, 2, 3, 4], sample_ids: null}\n"
        "  extra_dev: {source_order_1_based: [5, 6, 7, 8], sample_ids: null}\n"
        "  test: {source_order_1_based: [9, 10], sample_ids: null}\n", encoding="utf-8")
    freeze_split_config(inspect_snapshot(snapshot), split, apply=True)
    return snapshot, split, samples


def assert_error(code, operation, *args, **kwargs):
    with pytest.raises(EvaluationInputError) as error:
        operation(*args, **kwargs)
    assert error.value.code == code
    assert "SECRET_GOLD" not in str(error.value)


def refreeze(source, split):
    document = yaml.safe_load(split.read_text(encoding="utf-8"))
    document["snapshot_sha256"] = inspect_snapshot(source).sha256
    split.write_text(yaml.safe_dump(document), encoding="utf-8")


def test_fallback_identity_original_position_and_business_whitelist(tmp_path):
    source, split, _ = synthetic_files(tmp_path)
    manifest = load_question_manifest(source, split, selected_splits=("main_dev",))
    assert len(manifest.records) == 5
    first, second = manifest.records[:2]
    assert first.question == second.question
    assert (first.query_id, second.query_id) == ("qa_synthetic-0_0", "qa_synthetic-0_1")
    assert first.gold_record_key != second.gold_record_key
    assert (first.original_qa_index, second.original_qa_index) == (0, 1)
    assert {record.split for record in manifest.records} == {"main_dev"}
    request = first.to_query_request()
    assert request.as_of is None and request.question == first.question
    assert set(request.model_dump()) == {"schema_version", "query_id", "sample_id", "question", "as_of"}
    assert "SECRET_GOLD" not in repr(manifest) + request.model_dump_json()
    assert load_question_manifest(source, split, selected_splits=("test",)).records[0].sample_id == "synthetic-8"


def test_explicit_verified_official_id_and_collisions(tmp_path):
    source, split, samples = synthetic_files(tmp_path)
    assert_error("OFFICIAL_ID_FIELD_INVALID", load_question_manifest, source, split,
                 official_id_field="answer")
    assert_error("OFFICIAL_ID_MISSING", load_question_manifest, source, split,
                 official_id_field="official_qid")
    for index, item in enumerate(samples):
        for qa_index, qa in enumerate(item["qa"]):
            qa["official_qid"] = f"official_{index}_{qa_index}"
    source.write_text(json.dumps(samples), encoding="utf-8")
    # The frozen split binds raw bytes; a changed snapshot must fail until separately frozen.
    assert_error("DATA_SNAPSHOT_MISMATCH", load_question_manifest, source, split,
                 official_id_field="official_qid")
    split.write_text(
        "snapshot_sha256: null\nsample_ids: null\ngroups:\n"
        "  main_dev: {source_order_1_based: [1, 2, 3, 4], sample_ids: null}\n"
        "  extra_dev: {source_order_1_based: [5, 6, 7, 8], sample_ids: null}\n"
        "  test: {source_order_1_based: [9, 10], sample_ids: null}\n", encoding="utf-8")
    freeze_split_config(inspect_snapshot(source), split, apply=True)
    manifest = load_question_manifest(source, split, official_id_field="official_qid")
    assert manifest.records[0].query_id == "official_0_0"
    samples[0]["qa"][1]["official_qid"] = "official_0_0"
    source.write_text(json.dumps(samples), encoding="utf-8")
    split.write_text(
        "snapshot_sha256: null\nsample_ids: null\ngroups:\n"
        "  main_dev: {source_order_1_based: [1, 2, 3, 4], sample_ids: null}\n"
        "  extra_dev: {source_order_1_based: [5, 6, 7, 8], sample_ids: null}\n"
        "  test: {source_order_1_based: [9, 10], sample_ids: null}\n", encoding="utf-8")
    freeze_split_config(inspect_snapshot(source), split, apply=True)
    assert_error("DUPLICATE_QA_ID", load_question_manifest, source, split,
                 official_id_field="official_qid")


def test_bad_qa_and_split_are_rejected(tmp_path):
    source, split, samples = synthetic_files(tmp_path)
    assert_error("SPLIT_SELECTION_INVALID", load_question_manifest, source, split,
                 selected_splits=("unknown",))
    samples[0]["qa"][0]["question"] = "   "
    source.write_text(json.dumps(samples), encoding="utf-8")
    assert_error("DATA_SNAPSHOT_MISMATCH", load_question_manifest, source, split)
    refreeze(source, split)
    assert_error("QA_FORMAT_INVALID", load_question_manifest, source, split)


def test_wrong_frozen_split_and_empty_qa_fail(tmp_path):
    source, split, samples = synthetic_files(tmp_path)
    split_doc = yaml.safe_load(split.read_text(encoding="utf-8"))
    split_doc["groups"]["test"]["sample_ids"].reverse()
    split.write_text(yaml.safe_dump(split_doc), encoding="utf-8")
    assert_error("SPLIT_MISMATCH", load_question_manifest, source, split)
    split_doc["groups"]["test"]["sample_ids"].reverse()
    split.write_text(yaml.safe_dump(split_doc), encoding="utf-8")
    samples[0]["qa"] = []
    source.write_text(json.dumps(samples), encoding="utf-8")
    refreeze(source, split)
    assert_error("QA_EMPTY_OR_INVALID", load_question_manifest, source, split)
