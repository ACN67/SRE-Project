"""A-only offline execution proof. Every artifact is a synthetic fixture."""

import hashlib
import json
import logging
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))

from dynamic_memory.contracts import (AnswerResult, CandidateItem, CandidateSet,
    ConversationSample, ConversationSession, DatasetBundle, MemoryDecision,
    MemoryRecord, QueryAnalysis, ResolutionResult, SourceRef, SourceTurn)
from dynamic_memory.contracts.memory_id import compute_memory_id
from dynamic_memory.data import SourceRepository
from dynamic_memory.pipeline.query_runner import QueryOrchestrator
from dynamic_memory.services.dynamic_memory_service import DynamicMemoryService, ServiceError
from dynamic_memory.web.bindings import WebBindings
from evaluation.metrics.qa_scoring_manifest import (save_qa_scoring_manifest,
    verify_qa_scoring_manifest)
from evaluation.metrics.qa_summary import evaluate_saved_qa
from evaluation.question_manifest import load_question_manifest
from evaluation.runners import (RunProtocol, run_pure_vector_ablation,
    run_three_method_batch, verify_pure_vector_run, verify_run)
from test_eval_question_identity import synthetic_files, refreeze


def test_a_standalone_synthetic_flow(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    source, split, samples = synthetic_files(tmp_path)
    # The source is deliberately untrusted; it is opened for Gold only after run verification.
    for index, sample in enumerate(samples):
        for qa in sample["qa"]:
            qa["category"] = index % 5 + 1
            qa["answer"] = "Synthetic answer."
            qa["evidence"] = ["SECRET_GOLD_EVIDENCE"]
    samples[0]["qa"][1]["category"] = 5
    samples[2]["qa"][0]["answer"] = None
    source.write_text(json.dumps(samples), encoding="utf-8")
    refreeze(source, split)
    manifest = load_question_manifest(source, split, selected_splits=("main_dev", "extra_dev"))
    assert len(manifest.records) == 9
    assert len({r.sample_id for r in manifest.records}) == 8
    assert {s["qa"][0]["category"] for s in samples} == {1, 2, 3, 4, 5}
    assert len({r.query_id for r in manifest.records}) == 9

    sample_ids = tuple(dict.fromkeys(r.sample_id for r in manifest.records))
    ref = SourceRef(session_id="session_1", dia_id="synthetic_turn_0")
    source_bundle = DatasetBundle(snapshot_sha256=manifest.snapshot_sha256,
        samples=[ConversationSample(sample_id=sample_id, source_order_1_based=i + 1,
            sessions=[ConversationSession(session_id="session_1", turns=[SourceTurn(
                sample_id=sample_id, session_id="session_1", dia_id=f"synthetic_turn_{i}",
                speaker="Synthetic speaker", text=f"Synthetic source for {sample_id}.")])])
            for i, sample_id in enumerate(sample_ids)],
        split_by_sample_id={r.sample_id: r.split for r in manifest.records})
    repository = SourceRepository(source_bundle)
    draft = MemoryRecord(memory_id="mem_" + "0" * 20,
        sample_id=manifest.records[1].sample_id, content="Synthetic candidate only.",
        fact_kind="event", assertion_type="fact", polarity="positive",
        source_refs=[ref], extractor_version="synthetic-v1")
    candidate_memory = draft.model_copy(update={"memory_id": compute_memory_id(draft)})
    retrieval_calls = {}
    resolution_calls = []
    def analyze(req):
        if req.query_id == manifest.records[2].query_id:
            raise RuntimeError("SECRET_KEY analyzer failure")
        return QueryAnalysis(query_id=req.query_id, sample_id=req.sample_id,
                             query_intent="unknown", time_mode="latest_known")
    def candidates(req, _analysis, _index):
        retrieval_calls[req.query_id] = retrieval_calls.get(req.query_id, 0) + 1
        # Vector RAG gets a legal empty set; Dynamic gets one valid final candidate.
        selected = req.query_id == manifest.records[1].query_id and retrieval_calls[req.query_id] % 2 == 0
        return CandidateSet(query_id=req.query_id, sample_id=req.sample_id,
            base_top_k=8, max_candidates=12, retriever_fingerprint="synthetic-index",
            items=[CandidateItem(memory_id=candidate_memory.memory_id, rank=1,
                score=0.9, source_refs=[ref], origin="semantic")] if selected else [])
    def full_text_answer(req, _analysis, _sample):
        if req.query_id == manifest.records[4].query_id:
            raise RuntimeError("SECRET_KEY model failure")
        if req.query_id == manifest.records[0].query_id:
            return AnswerResult(status="answered", answer="Synthetic answer.")
        return AnswerResult(status="no_evidence", uncertainty="synthetic fixture")
    def resolve(req, _analysis, memories):
        resolution_calls.append(req.query_id)
        return ResolutionResult(query_id=req.query_id,
            decisions=[MemoryDecision(memory_id=m.memory_id, action="RETAIN", reason="Synthetic")
                       for m in memories], effective_memory_ids=[m.memory_id for m in memories])
    runner = QueryOrchestrator(analyze_query=analyze, get_sample=repository.get_sample,
        answer_full_text=full_text_answer,
        load_verified_index=lambda _sample: object(), retrieve=candidates,
        retrieve_raw_topk=lambda req, analysis, index: CandidateSet(query_id=req.query_id,
            sample_id=req.sample_id, base_top_k=8, max_candidates=12,
            retriever_fingerprint="synthetic-index", items=[]),
        load_candidates=lambda selected, _index: [candidate_memory for _ in selected.items],
        resolve=resolve,
        answer_vector_rag=lambda *_: AnswerResult(status="no_evidence", uncertainty="Synthetic fixture"),
        answer_dynamic=lambda *_: AnswerResult(status="no_evidence", uncertainty="Synthetic fixture"))
    protocol = RunProtocol("synthetic-main", manifest.snapshot_sha256, manifest.selected_splits,
        "synthetic-prompt", "synthetic-model", "synthetic-settings", "synthetic-index",
        "synthetic-index-artifact", "latest_known", 8, 12)
    run = run_three_method_batch(manifest, runner, protocol, results_root=tmp_path)
    checked = verify_run(run.directory, manifest, expected_manifest_sha256=run.manifest_sha256)
    assert checked.comparisons == run.comparisons
    assert {c.status for c in checked.comparisons} == {"identical", "mismatch", "not_comparable"}
    assert checked.comparisons[0].status == "identical"
    assert checked.comparisons[1].status == "mismatch"
    assert checked.comparisons[1].reason == "final_candidate_differs"
    assert checked.comparisons[2].status == "not_comparable"
    assert {status: sum(c.status == status for c in checked.comparisons)
            for status in ("identical", "mismatch", "not_comparable", "unverified")} == {
                "identical": 7, "mismatch": 1, "not_comparable": 1, "unverified": 0}
    assert len(checked.receipts) == 3
    assert all(receipt.path.is_file() and receipt.file_sha256 == hashlib.sha256(
        receipt.path.read_bytes()).hexdigest() for receipt in checked.receipts)

    unknown = run_three_method_batch(manifest, runner,
        replace(protocol, run_id="synthetic-unverified", index_fingerprint="unknown/unverified"),
        results_root=tmp_path)
    unknown_checked = verify_run(unknown.directory, manifest,
        expected_manifest_sha256=unknown.manifest_sha256)
    assert unknown_checked.comparisons[0].status == "unverified"
    assert unknown_checked.comparisons[0].reason == "upstream_identity_unverified"
    # Repeated synthetic execution has identical prediction bytes; the
    # changed index identity affects comparability, not the answer files.
    assert {receipt.path.name: receipt.path.read_bytes() for receipt in checked.receipts} == {
        receipt.path.name: receipt.path.read_bytes() for receipt in unknown_checked.receipts}
    main_bytes = {p.name: p.read_bytes() for p in run.directory.iterdir() if p.is_file()}
    retrieval_count = sum(retrieval_calls.values())
    resolution_count = len(resolution_calls)
    ablation = run_pure_vector_ablation(manifest, runner,
        replace(protocol, run_id="synthetic-ablation"), results_root=tmp_path)
    assert verify_pure_vector_run(ablation.directory, manifest,
        expected_manifest_sha256=ablation.manifest_sha256).responses == ablation.responses
    assert {p.name: p.read_bytes() for p in run.directory.iterdir() if p.is_file()} == main_bytes
    assert json.loads(ablation.manifest_path.read_text(encoding="utf-8"))["main_experiment"] is False
    assert len(ablation.responses) == 9
    assert all(row.method == "pure_vector_topk" for row in ablation.responses)
    assert sum(retrieval_calls.values()) == retrieval_count
    assert len(resolution_calls) == resolution_count

    # Gold enters only here, after all persisted prediction checks.
    receipt = checked.receipts[0]
    report = evaluate_saved_qa(receipt.path, manifest, receipt, source, split,
                               annotation_source="synthetic_fixture")
    assert (report.summary.total_predictions, report.summary.gold_missing,
            report.summary.execution_failed) == (9, 1, 2)
    assert dict(report.summary.reason_counts) == {
        "execution_failure": 2, "missing_gold": 1, "no_evidence": 5,
        "time_uncertain": 0, "other": 1}
    assert report.questions[2].original_status == "error"
    assert report.questions[3].gold_missing and report.questions[3].score is None
    assert report.questions[4].original_status == "error"
    assert report.questions[4].score == 0.0
    assert report.questions[0].score == 1.0
    assert (report.questions[0].text_token_f1, report.questions[0].category,
            report.questions[0].gold_missing) == (1.0, 1, False)
    assert (report.questions[1].score, report.questions[1].text_token_f1,
            report.questions[1].category) == (1.0, 0.0, 5)
    assert sum((q.score or 0) for q in report.questions) == 3.0
    assert report.summary.main_mean == pytest.approx(3 / 8)
    assert (report.summary.gold_scored, report.summary.conditional_scored,
            report.summary.conditional_mean, report.summary.text_f1_main_mean) == (
                8, 6, pytest.approx(3 / 6), pytest.approx(1 / 8))
    assert {group.key: (group.total_predictions, group.gold_scored)
            for group in report.summary.by_category} == {
                "1": (2, 2), "2": (2, 2), "3": (1, 1),
                "4": (1, 1), "5": (2, 2), "unknown": (1, 0)}
    assert sum(group.total_predictions for group in report.summary.by_category_sample) == 9
    assert len(report.summary.by_category_sample) == 9
    cross = {group.key: group for group in report.summary.by_category_sample}
    # Hand ledger: every sample has one question except synthetic-0, which
    # has two. Category 5 accepts no evidence; failed executions score zero.
    expected_cells = {
        "1|synthetic-0": (1, 1, 0, 0, 1.0),
        "1|synthetic-5": (1, 1, 0, 0, 0.0),
        "2|synthetic-1": (1, 1, 0, 1, 0.0),
        "2|synthetic-6": (1, 1, 0, 0, 0.0),
        "3|synthetic-7": (1, 1, 0, 0, 0.0),
        "4|synthetic-3": (1, 1, 0, 1, 0.0),
        "5|synthetic-0": (1, 1, 0, 0, 1.0),
        "5|synthetic-4": (1, 1, 0, 0, 1.0),
        "unknown|synthetic-2": (1, 0, 1, 0, None),
    }
    assert {key: (cell.total_predictions, cell.gold_scored,
                  cell.gold_missing, cell.execution_failed, cell.main_mean)
            for key, cell in cross.items()} == expected_cells
    scoring_path = tmp_path / "qa_scoring_manifest.json"
    saved = save_qa_scoring_manifest(scoring_path, receipt.path, manifest, receipt,
        report, annotation_source="synthetic_fixture", run_manifest_path=run.manifest_path)
    assert verify_qa_scoring_manifest(scoring_path, receipt.path, manifest, receipt,
        report, annotation_source="synthetic_fixture", run_manifest_path=run.manifest_path,
        expected_sha256=saved.file_sha256) == saved
    exported = json.loads(scoring_path.read_text(encoding="utf-8"))
    assert exported["annotation_source"] == "synthetic_fixture"
    assert {cell["key"]: (cell["total_predictions"], cell["gold_scored"],
                          cell["gold_missing"], cell["execution_failed"],
                          cell["main_mean"])
            for cell in exported["by_category_sample"]} == expected_cells
    assert hashlib.sha256(scoring_path.read_bytes()).hexdigest() == saved.file_sha256
    export = tmp_path / "synthetic_flow_summary.json"
    export.write_text(json.dumps({"execution_kind": "synthetic_fixture",
        "score_status": "not_a_formal_score", "run_sha256": checked.manifest_sha256,
        "ablation_sha256": ablation.manifest_sha256,
        "scoring_sha256": saved.file_sha256,
        "question_count": report.summary.total_predictions,
        "gold_scored": report.summary.gold_scored,
        "gold_missing": report.summary.gold_missing,
        "execution_failed": report.summary.execution_failed,
        "synthetic_main_mean": report.summary.main_mean,
        "reason_counts": dict(report.summary.reason_counts)}, sort_keys=True), encoding="utf-8")

    # The read-only service starts from a separately reopened, persisted
    # Gold-free source artifact, not the runner's in-memory repository.
    source_artifact = tmp_path / "synthetic_fixture_source_bundle.json"
    source_artifact.write_text(source_bundle.model_dump_json(), encoding="utf-8")
    source_sha256 = hashlib.sha256(source_artifact.read_bytes()).hexdigest()
    reopened_bundle = DatasetBundle.model_validate_json(source_artifact.read_bytes())
    assert reopened_bundle.snapshot_sha256 == manifest.snapshot_sha256
    assert hashlib.sha256(source_artifact.read_bytes()).hexdigest() == source_sha256
    service = DynamicMemoryService(sources=SourceRepository(reopened_bundle))
    bindings = WebBindings(service=service, label="synthetic_fixture", synthetic=True)
    assert bindings.list_samples() == list(sample_ids)
    turn = bindings.get_turn(sample_ids[0], ref)
    assert turn.text == f"Synthetic source for {sample_ids[0]}."
    with pytest.raises(ServiceError, match="SAMPLE_MISMATCH"):
        bindings.get_turn(sample_ids[1], ref)
    with pytest.raises(ServiceError, match="BUILD_STATUS_UNAVAILABLE"):
        bindings.list_memories(sample_ids[0])
    public = b"".join(p.read_bytes() for p in tmp_path.rglob("*.predictions.json"))
    public += b"".join(p.read_bytes() for p in tmp_path.rglob("*manifest.json"))
    public += export.read_bytes()
    public += caplog.text.encode("utf-8")
    assert b"SECRET_GOLD" not in public and b"SECRET_KEY" not in public
    assert b"synthetic_fixture" in public
    assert b"not_a_formal_score" in export.read_bytes()
