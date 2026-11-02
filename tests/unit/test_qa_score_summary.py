"""Synthetic saved predictions, Gold gate, denominator and status tests."""

import json
from dataclasses import replace

import pytest
import yaml

from dynamic_memory.contracts import AnswerResult, ErrorInfo, QueryResponse
from evaluation.metrics.qa_summary import evaluate_saved_qa, score_qa_results, summarize_questions
from evaluation.gold_loader import GoldLoadResult, GoldRecord
from evaluation.predictions import save_predictions
from evaluation.question_manifest import EvaluationInputError, load_question_manifest
from test_eval_question_identity import refreeze, synthetic_files


def response(record, result, method="full_text"):
    return QueryResponse(query_id=record.query_id, sample_id=record.sample_id,
                         question=record.question, method=method, query_analysis=None,
                         retrieved=None, resolution=None, answer_result=result)


def scored_fixture(tmp_path):
    source, split, samples = synthetic_files(tmp_path)
    samples[0]["qa"] = [
        {"question": f"Synthetic {i}?", "answer": answer, "category": category, "evidence": []}
        for i, (answer, category) in enumerate([
            ("red, blue", 1), ("running", 2), ("Paris; France", 3),
            ("apple apple", 4), ("unknown", 5),
        ])
    ]
    samples[1]["qa"][0].update(answer="unknown", category=5)
    samples[2]["qa"][0].pop("answer")
    samples[3]["qa"][0].update(answer="green", category=2)
    source.write_text(json.dumps(samples), encoding="utf-8")
    refreeze(source, split)
    manifest = load_question_manifest(source, split, selected_splits=("main_dev",))
    predictions = [
        AnswerResult(status="answered", answer="blue, red"),
        AnswerResult(status="answered", answer="run"),
        AnswerResult(status="answered", answer="Paris"),
        AnswerResult(status="answered", answer="apple"),
        AnswerResult(status="no_evidence", uncertainty="Synthetic rejection"),
        AnswerResult(status="error", error=ErrorInfo(code="MODEL_FAILED", message="Synthetic failure")),
        AnswerResult(status="time_uncertain", uncertainty="Synthetic time"),
        AnswerResult(status="unresolved_conflict", uncertainty="Synthetic conflict"),
    ]
    rows = [response(record, prediction) for record, prediction in zip(manifest.records, predictions)]
    path = tmp_path / "synthetic.predictions.json"
    receipt = save_predictions(path, manifest, rows, "full_text")
    return source, split, manifest, path, receipt, rows


def test_saved_entrypoint_exact_scores_and_denominators(tmp_path):
    source, split, manifest, path, receipt, rows = scored_fixture(tmp_path)
    report = evaluate_saved_qa(path, manifest, receipt, source, split,
                               annotation_source="synthetic_fixture")
    assert len(report.questions) == 8
    assert [item.score for item in report.questions[:6]] == pytest.approx([1, 1, 1, 2/3, 1, 0])
    assert report.questions[6].score is None and report.questions[6].gold_missing
    assert report.questions[6].original_status == "time_uncertain"
    assert report.questions[7].score == 0 and not report.questions[7].gold_missing
    assert report.questions[5].execution_failed and report.questions[5].adapted_text is None
    assert report.questions[4].adapted_text == "No information available"
    assert report.questions[0].original_prediction == "blue, red"
    assert report.questions[0].annotation_source == "synthetic_fixture"
    summary = report.summary
    assert summary.score_name == "LoCoMo 官方 QA 综合得分"
    assert (summary.total_predictions, summary.gold_scored, summary.gold_missing,
            summary.execution_failed, summary.conditional_scored) == (8, 7, 1, 1, 6)
    assert summary.main_mean == pytest.approx(2/3)
    assert summary.conditional_mean == pytest.approx(7/9)
    assert summary.execution_success_coverage == pytest.approx(7/8)
    assert summary.scored_execution_coverage == pytest.approx(6/7)
    assert [(item.key, item.gold_scored, item.main_mean) for item in summary.by_category[:5]] == [
        ("1", 1, 1), ("2", 2, 0.5), ("3", 1, 1), ("4", 1, pytest.approx(2/3)),
        ("5", 2, 0.5)]
    assert [(item.key, item.total_predictions, item.gold_missing) for item in summary.by_category[5:]] == [
        ("unknown", 1, 1)]
    assert [item.key for item in summary.by_sample] == [f"synthetic-{i}" for i in range(4)]
    assert summary.by_sample[2].main_mean is None
    assert summary.by_method[0].key == "full_text" and summary.by_method[0].gold_scored == 7
    assert path.read_bytes() and "SECRET_GOLD" not in path.read_text(encoding="utf-8")


def test_receipt_tampering_fails_before_gold_and_mappings_reject_duplicates(tmp_path):
    source, split, manifest, path, receipt, rows = scored_fixture(tmp_path)
    with pytest.raises(EvaluationInputError, match="PREDICTION_RECEIPT_INVALID"):
        evaluate_saved_qa(path, manifest, replace(receipt, snapshot_sha256="0" * 64),
                          tmp_path / "missing-source.json", split, annotation_source="synthetic_fixture")
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(EvaluationInputError, match="PREDICTION_HASH_MISMATCH"):
        evaluate_saved_qa(path, manifest, receipt, tmp_path / "missing-source.json", split,
                          annotation_source="synthetic_fixture")

    duplicate = GoldRecord(manifest.records[0].sample_id, manifest.records[0].query_id,
                           manifest.records[0].gold_record_key, 0, "synthetic",
                           "[]", 1, "synthetic_fixture")
    with pytest.raises(EvaluationInputError, match="GOLD_ID_MISMATCH"):
        score_qa_results(manifest, rows, GoldLoadResult((duplicate, duplicate),
                         tuple(record.gold_record_key for record in manifest.records[1:])), "full_text")


def test_zero_denominator_is_na_and_duplicate_result_rejected():
    summary = summarize_questions(())
    assert summary.main_mean is None and summary.conditional_mean is None
    assert summary.execution_success_coverage is None and summary.scored_execution_coverage is None
    assert all(item.main_mean is None and item.gold_scored == 0 for item in summary.by_category)


def test_duplicate_or_inconsistent_result_fails(tmp_path):
    source, split, manifest, path, receipt, _ = scored_fixture(tmp_path)
    report = evaluate_saved_qa(path, manifest, receipt, source, split,
                               annotation_source="synthetic_fixture")
    with pytest.raises(EvaluationInputError, match="DUPLICATE_QA_RESULT"):
        summarize_questions(report.questions + report.questions[:1])
    with pytest.raises(EvaluationInputError, match="QA_RESULT_INVALID"):
        summarize_questions((replace(report.questions[0], gold_missing=True),))
    combined = summarize_questions((report.questions[0],
                                    replace(report.questions[0], method="vector_rag")))
    assert [group.key for group in combined.by_method] == ["full_text", "vector_rag"]
    assert [group.gold_scored for group in combined.by_method] == [1, 1]


def test_saved_entrypoint_rejects_wrong_method_and_split(tmp_path):
    source, split, manifest, path, receipt, _ = scored_fixture(tmp_path)
    with pytest.raises(EvaluationInputError, match="PREDICTION_INVALID"):
        evaluate_saved_qa(path, manifest, replace(receipt, method="vector_rag"),
                          source, split, annotation_source="synthetic_fixture")
    wrong = tmp_path / "wrong-split.yaml"
    document = yaml.safe_load(split.read_text(encoding="utf-8"))
    document["groups"]["main_dev"]["sample_ids"].reverse()
    wrong.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(EvaluationInputError, match="SPLIT_MISMATCH"):
        evaluate_saved_qa(path, manifest, receipt, source, wrong,
                          annotation_source="synthetic_fixture")
