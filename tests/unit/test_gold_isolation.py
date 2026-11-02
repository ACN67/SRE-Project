"""Synthetic Gold stays behind a real persisted-prediction receipt."""

import json
import hashlib
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

import evaluation.predictions as predictions_module

from dynamic_memory.contracts import AnswerResult, QueryResponse
from evaluation.gold_loader import load_gold_after_predictions
from evaluation.predictions import save_predictions, verify_predictions
from evaluation.question_manifest import EvaluationInputError, load_question_manifest
from test_eval_question_identity import refreeze, synthetic_files


def predictions(manifest, method="full_text"):
    return [QueryResponse(query_id=record.query_id, sample_id=record.sample_id,
                          question=record.question, method=method, query_analysis=None,
                          retrieved=None, resolution=None,
                          answer_result=AnswerResult(status="no_evidence", uncertainty="Synthetic fixture"))
            for record in manifest.records]


def fail(code, fn, *args, **kwargs):
    with pytest.raises(EvaluationInputError) as error:
        fn(*args, **kwargs)
    assert error.value.code == code
    assert "SECRET_GOLD" not in str(error.value)


def test_save_verify_then_load_gold_by_unique_key(tmp_path):
    source, split, _ = synthetic_files(tmp_path)
    manifest = load_question_manifest(source, split, selected_splits=("main_dev",))
    path = tmp_path / "synthetic.predictions.json"
    rows = predictions(manifest)
    receipt = save_predictions(path, manifest, rows, "full_text")
    assert path.is_file() and receipt.file_sha256
    assert verify_predictions(path, manifest, receipt) == tuple(rows)
    gold = load_gold_after_predictions(source, split, path, manifest, receipt,
                                       annotation_source="synthetic_fixture")
    assert len(gold.records) == 5 and not gold.missing_keys
    assert gold.records[0].query_id != gold.records[1].query_id
    assert gold.records[0].gold_record_key != gold.records[1].gold_record_key
    assert gold.records[0].answer == "SECRET_GOLD_ANSWER"
    assert gold.records[1].answer == "Second synthetic Gold"
    assert gold.records[0].category == 1 and gold.records[1].category == 5
    assert json.loads(gold.records[0].evidence_json) == ["SECRET_GOLD_EVIDENCE"]
    assert "SECRET_GOLD" not in path.read_text(encoding="utf-8")


def test_missing_receipt_file_hash_and_mismatched_identity_fail_before_gold(tmp_path):
    source, split, _ = synthetic_files(tmp_path)
    manifest = load_question_manifest(source, split, selected_splits=("main_dev",))
    path = tmp_path / "synthetic.predictions.json"
    fail("PREDICTION_RECEIPT_INVALID", load_gold_after_predictions,
         tmp_path / "missing-gold.json", split, path, manifest, True,
         annotation_source="synthetic_fixture")
    fail("PREDICTION_INCOMPLETE", save_predictions, path, manifest,
         predictions(manifest)[:-1], "full_text")
    wrong = predictions(manifest)
    wrong[0] = wrong[0].model_copy(update={"sample_id": "synthetic-1"})
    fail("PREDICTION_ID_MISMATCH", save_predictions, path, manifest, wrong, "full_text")
    receipt = save_predictions(path, manifest, predictions(manifest), "full_text")
    fail("PREDICTION_RECEIPT_INVALID", load_gold_after_predictions,
         tmp_path / "missing-gold.json", split, path, manifest,
         replace(receipt, snapshot_sha256="0" * 64), annotation_source="synthetic_fixture")
    fail("PREDICTION_NOT_FOUND", load_gold_after_predictions,
         tmp_path / "missing-gold.json", split, tmp_path / "absent.predictions.json", manifest,
         replace(receipt, path=(tmp_path / "absent.predictions.json").resolve()),
         annotation_source="synthetic_fixture")
    path.write_bytes(path.read_bytes() + b" ")
    fail("PREDICTION_HASH_MISMATCH", load_gold_after_predictions,
         tmp_path / "missing-gold.json", split, path, manifest, receipt,
         annotation_source="synthetic_fixture")


def test_missing_gold_is_separate_from_failed_prediction(tmp_path):
    source, split, samples = synthetic_files(tmp_path)
    samples[0]["qa"][0].pop("answer")
    source.write_text(json.dumps(samples), encoding="utf-8")
    # Freeze a new synthetic snapshot; the question remains public without Gold.
    from dynamic_memory.data.snapshot import freeze_split_config, inspect_snapshot
    split.write_text(
        "snapshot_sha256: null\nsample_ids: null\ngroups:\n"
        "  main_dev: {source_order_1_based: [1, 2, 3, 4], sample_ids: null}\n"
        "  extra_dev: {source_order_1_based: [5, 6, 7, 8], sample_ids: null}\n"
        "  test: {source_order_1_based: [9, 10], sample_ids: null}\n", encoding="utf-8")
    freeze_split_config(inspect_snapshot(source), split, apply=True)
    manifest = load_question_manifest(source, split, selected_splits=("main_dev",))
    path = tmp_path / "synthetic.predictions.json"
    receipt = save_predictions(path, manifest, predictions(manifest), "full_text")
    gold = load_gold_after_predictions(source, split, path, manifest, receipt,
                                       annotation_source="synthetic_fixture")
    assert gold.missing_keys == (manifest.records[0].gold_record_key,)
    assert len(gold.records) == len(manifest.records) - 1


def test_gold_schema_category_and_manifest_mismatch(tmp_path):
    source, split, _ = synthetic_files(tmp_path)
    manifest = load_question_manifest(source, split, selected_splits=("main_dev",))
    path = tmp_path / "synthetic.predictions.json"
    receipt = save_predictions(path, manifest, predictions(manifest), "full_text")
    fail("ANNOTATION_SOURCE_INVALID", load_gold_after_predictions, source, split, path,
         manifest, receipt, annotation_source="pretend_course_gold")
    altered = replace(manifest, records=manifest.records[:-1])
    fail("PREDICTION_RECEIPT_INVALID", load_gold_after_predictions,
         source, split, path, altered, receipt, annotation_source="synthetic_fixture")


def test_rehashed_prediction_tampering_still_fails_identity_and_schema(tmp_path):
    source, split, _ = synthetic_files(tmp_path)
    manifest = load_question_manifest(source, split, selected_splits=("main_dev",))
    path = tmp_path / "synthetic.predictions.json"
    receipt = save_predictions(path, manifest, predictions(manifest), "full_text")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["predictions"][0]["response"]["sample_id"] = "synthetic-1"
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    path.write_bytes(raw)
    forged = replace(receipt, file_sha256=hashlib.sha256(raw).hexdigest())
    fail("PREDICTION_ID_MISMATCH", load_gold_after_predictions,
         source, split, path, manifest, forged, annotation_source="synthetic_fixture")
    payload["predictions"][0]["response"]["sample_id"] = "synthetic-0"
    payload["predictions"][0]["response"]["answer_result"]["status"] = "unknown"
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    path.write_bytes(raw)
    forged = replace(receipt, file_sha256=hashlib.sha256(raw).hexdigest())
    fail("PREDICTION_INVALID", load_gold_after_predictions,
         source, split, path, manifest, forged, annotation_source="synthetic_fixture")


def test_duplicate_prediction_and_unsafe_path_are_rejected(tmp_path):
    source, split, _ = synthetic_files(tmp_path)
    manifest = load_question_manifest(source, split, selected_splits=("main_dev",))
    rows = predictions(manifest)
    fail("PREDICTION_ID_MISMATCH", save_predictions,
         tmp_path / "synthetic.predictions.json", manifest, rows + rows[:1], "full_text")
    fail("PREDICTION_PATH_INVALID", save_predictions,
         source, manifest, rows, "full_text")
    occupied = tmp_path / "occupied.predictions.json"
    occupied.write_text("synthetic source, not predictions", encoding="utf-8")
    fail("PREDICTION_PATH_IN_USE", save_predictions,
         occupied, manifest, rows, "full_text")
    assert occupied.read_text(encoding="utf-8") == "synthetic source, not predictions"


@pytest.mark.parametrize("mutation", [
    lambda qa: qa.__setitem__("category", 9),
    lambda qa: qa.pop("category"),
    lambda qa: qa.pop("evidence"),
])
def test_malformed_synthetic_gold_is_content_free_error(tmp_path, mutation):
    source, split, samples = synthetic_files(tmp_path)
    mutation(samples[0]["qa"][0])
    source.write_text(json.dumps(samples), encoding="utf-8")
    refreeze(source, split)
    manifest = load_question_manifest(source, split, selected_splits=("main_dev",))
    path = tmp_path / "synthetic.predictions.json"
    receipt = save_predictions(path, manifest, predictions(manifest), "full_text")
    fail("GOLD_FORMAT_INVALID", load_gold_after_predictions,
         source, split, path, manifest, receipt, annotation_source="synthetic_fixture")


def test_actual_repository_results_path_is_allowed_and_sibling_results_is_forbidden(tmp_path):
    source, split, _ = synthetic_files(tmp_path)
    manifest = load_question_manifest(source, split, selected_splits=("main_dev",))
    repository_root = Path(predictions_module.__file__).resolve().parents[1]
    valid = repository_root / "results" / f"a14-{uuid4().hex}.predictions.json"
    wrong_root = repository_root.parent / "results" / f"a14-{uuid4().hex}.predictions.json"

    assert predictions_module._target(valid) == valid.resolve()
    fail("PREDICTION_PATH_INVALID", predictions_module._target, wrong_root)

    try:
        receipt = save_predictions(valid, manifest, predictions(manifest), "full_text")
        assert valid.is_file()
        assert verify_predictions(valid, manifest, receipt) == tuple(predictions(manifest))
    finally:
        valid.unlink(missing_ok=True)


def test_publish_race_cannot_overwrite_an_existing_prediction(tmp_path, monkeypatch):
    source, split, _ = synthetic_files(tmp_path)
    manifest = load_question_manifest(source, split, selected_splits=("main_dev",))
    target = tmp_path / "concurrent.predictions.json"
    original_link = predictions_module.os.link
    winner_content = b"another completed writer"

    def simulate_concurrent_publisher(temp, final):
        Path(final).write_bytes(winner_content)
        return original_link(temp, final)

    monkeypatch.setattr(predictions_module.os, "link", simulate_concurrent_publisher)
    fail("PREDICTION_PATH_IN_USE", save_predictions, target, manifest,
         predictions(manifest), "full_text")
    assert target.read_bytes() == winner_content
    assert not list(tmp_path.glob(".predictions-*.tmp"))
