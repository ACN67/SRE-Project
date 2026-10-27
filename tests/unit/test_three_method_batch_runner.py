"""A-17 Gold-free synthetic orchestration and byte-bound persistence."""

import json
import hashlib
from dataclasses import replace
from uuid import uuid4

import pytest

from dynamic_memory.contracts import (AnswerResult, CandidateSet, ConversationSample,
    ConversationSession, QueryAnalysis, QueryResponse, ResolutionResult)
from dynamic_memory.pipeline.query_runner import QueryOrchestrator
from evaluation.question_manifest import (EvaluationInputError, QuestionManifest,
                                          QuestionRecord, load_question_manifest)
from evaluation.runners import RunProtocol, run_three_method_batch, verify_run
import evaluation.runners.three_method_runner as batch_module
from test_run_query_dispatch import SAMPLE, analysis, ports
from test_eval_question_identity import synthetic_files


def setup(count=2, *, run_id=None):
    records = tuple(QuestionRecord(SAMPLE, f"synthetic_q{i}", i,
        f"Synthetic question {i}?", f"sha256:{i + 1:064x}", "main_dev") for i in range(count))
    manifest = QuestionManifest("a" * 64, records, ("main_dev",), None)
    protocol = RunProtocol(run_id or f"synthetic-{uuid4().hex}", manifest.snapshot_sha256,
        manifest.selected_splits, "synthetic-prompt", "synthetic-model",
        "synthetic-settings", "synthetic-index", "synthetic-index-artifact",
        "latest_known", 8, 12)
    return manifest, protocol


def test_one_analysis_three_methods_order_saved_and_verified(tmp_path):
    manifest, protocol = setup()
    events = []
    result = run_three_method_batch(manifest, QueryOrchestrator(**ports(event_log=events)),
                                    protocol, results_root=tmp_path)
    assert result.status == "completed_verified"
    assert [c.status for c in result.comparisons] == ["identical", "identical"]
    assert events.count("analyze") == len(manifest.records)
    assert events.count("retrieve") == 2 * len(manifest.records)  # observed equality, independent calls
    assert [event for event in events if event.endswith("answer")] == [
        "full_answer", "rag_answer", "dynamic_answer"] * len(manifest.records)
    assert len(result.receipts) == 3
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert payload["status"] == "completed_verified"
    assert payload["query_ids"] == [r.query_id for r in manifest.records]
    assert payload["methods"] == ["full_text", "vector_rag", "dynamic"]
    assert payload["comparison_counts"] == {"identical": 2, "mismatch": 0,
                                             "not_comparable": 0, "unverified": 0}
    assert payload["execution_kind"] == "synthetic_fixture" and payload["provider_status"] == "provider_not_run"
    assert all(item["count"] == 2 and item["error_count"] == 0 for item in payload["predictions"])
    assert all((result.directory / f"{method}.predictions.json").is_file()
               for method in payload["methods"])
    assert verify_run(result.directory, manifest,
                      expected_manifest_sha256=result.manifest_sha256).manifest_sha256 == result.manifest_sha256
    assert "SECRET_GOLD" not in result.manifest_path.read_text(encoding="utf-8")


def test_real_a14_synthetic_manifest_exposes_no_gold_to_batch(tmp_path, monkeypatch):
    source, split, _ = synthetic_files(tmp_path)
    manifest = load_question_manifest(source, split, selected_splits=("main_dev",))
    protocol = RunProtocol(f"a14-{uuid4().hex}", manifest.snapshot_sha256, manifest.selected_splits,
        "synthetic-prompt", "synthetic-model", "synthetic-settings", "synthetic-retriever",
        "synthetic-index", "latest_known", 8, 12)
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Gold access is forbidden during prediction")
    import evaluation.gold_loader as gold_loader
    monkeypatch.setattr(gold_loader, "load_gold_after_predictions", forbidden)
    events = []
    def analyze(request):
        events.append(request.query_id)
        assert "SECRET_GOLD" not in request.model_dump_json()
        return QueryAnalysis(query_id=request.query_id, sample_id=request.sample_id,
                             query_intent="unknown", time_mode="latest_known")
    runner = QueryOrchestrator(analyze_query=analyze,
        get_sample=lambda sample_id: ConversationSample(sample_id=sample_id, source_order_1_based=1,
            sessions=[ConversationSession(session_id="session_1", turns=[])]),
        answer_full_text=lambda *_: AnswerResult(status="no_evidence", uncertainty="synthetic"),
        load_verified_index=lambda _sample: object(),
        retrieve=lambda request, _analysis, _index: CandidateSet(query_id=request.query_id,
            sample_id=request.sample_id, base_top_k=8, max_candidates=12,
            retriever_fingerprint="synthetic-retriever", items=[]),
        load_candidates=lambda _candidates, _index: [],
        resolve=lambda request, _analysis, _memories: ResolutionResult(query_id=request.query_id))
    result = run_three_method_batch(manifest, runner, protocol, results_root=tmp_path)
    assert len(events) == len(manifest.records) == 5
    assert result.status == "completed_verified"
    assert all(len(receipt.query_ids) == 5 for receipt in result.receipts)
    assert "SECRET_GOLD" not in result.manifest_path.read_text(encoding="utf-8")
    assert all("SECRET_GOLD" not in (result.directory / f"{m}.predictions.json").read_text(encoding="utf-8")
               for m in ("full_text", "vector_rag", "dynamic"))
    assert verify_run(result.directory, manifest).status == "completed_verified"


def test_analyzer_failure_and_missing_dynamic_port_keep_every_question(tmp_path):
    manifest, protocol = setup(1)
    def failed(_request):
        raise RuntimeError("SECRET_GOLD private exception")
    runner = QueryOrchestrator(**ports(analyzer=failed))
    result = run_three_method_batch(manifest, runner, protocol, results_root=tmp_path)
    assert result.status == "complete_not_comparable"
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert [item["error_count"] for item in payload["predictions"]] == [1, 1, 1]
    assert result.comparisons[0].status == "not_comparable"
    assert "SECRET_GOLD" not in result.manifest_path.read_text(encoding="utf-8")
    assert verify_run(result.directory, manifest).status == "complete_not_comparable"

    manifest2, protocol2 = setup(1)
    config = ports()
    config.pop("resolve")
    result2 = run_three_method_batch(manifest2, QueryOrchestrator(**config), protocol2, results_root=tmp_path)
    payload2 = json.loads(result2.manifest_path.read_text(encoding="utf-8"))
    assert [item["error_count"] for item in payload2["predictions"]] == [0, 0, 1]
    assert result2.comparisons[0].status == "not_comparable"


def test_early_stop_empty_candidates_and_unverified_identity(tmp_path):
    manifest, protocol = setup(1)
    events = []
    def conflict(request):
        from dynamic_memory.contracts import QueryAnalysis, TimeIssue
        return QueryAnalysis(query_id=request.query_id, sample_id=request.sample_id,
            query_intent="historical_state", time_mode="conflict",
            time_issue=TimeIssue(code="QUERY_TIME_CONFLICT", message="synthetic",
                explicit_as_of="2024-01-01", question_time="2025-01-01"))
    result = run_three_method_batch(manifest, QueryOrchestrator(**ports(event_log=events, analyzer=conflict)),
                                    protocol, results_root=tmp_path)
    assert events == [] or events == ["analyze"]
    assert result.comparisons[0].status == "not_comparable"
    assert verify_run(result.directory, manifest).status == "complete_not_comparable"

    manifest2, protocol2 = setup(1)
    config = ports()
    config["retrieve"] = lambda req, _analysis, _index: CandidateSet(
        query_id=req.query_id, sample_id=req.sample_id, base_top_k=8,
        max_candidates=12, retriever_fingerprint="synthetic-index", items=[])
    config["load_candidates"] = lambda _candidates, _index: []
    config["resolve"] = lambda req, _analysis, _memories: __import__(
        "dynamic_memory.contracts", fromlist=["ResolutionResult"]).ResolutionResult(query_id=req.query_id)
    empty = run_three_method_batch(manifest2, QueryOrchestrator(**config), protocol2, results_root=tmp_path)
    assert empty.comparisons[0].status == "identical"
    assert verify_run(empty.directory, manifest2).status == "completed_verified"

    manifest3, protocol3 = setup(1)
    unknown = replace(protocol3, index_fingerprint="unknown/unverified")
    unverified = run_three_method_batch(manifest3, QueryOrchestrator(**ports()), unknown, results_root=tmp_path)
    assert unverified.comparisons[0].status == "unverified"
    assert unverified.status == "complete_not_comparable"


@pytest.mark.parametrize("run_id", ["../escape", "..", "bad/name", "bad\\name", "a" * 65])
def test_bad_run_id_is_rejected_before_creation(tmp_path, run_id):
    manifest, protocol = setup(1, run_id=run_id)
    with pytest.raises(EvaluationInputError, match="RUN_ID_INVALID"):
        run_three_method_batch(manifest, QueryOrchestrator(**ports()), protocol, results_root=tmp_path)


def test_reject_test_split_wrong_snapshot_and_existing_directory(tmp_path):
    manifest, protocol = setup(1)
    with pytest.raises(EvaluationInputError, match="RUN_PROTOCOL_INVALID"):
        run_three_method_batch(manifest, QueryOrchestrator(**ports()),
                               replace(protocol, snapshot_sha256="b" * 64), results_root=tmp_path)
    test_manifest = replace(manifest, selected_splits=("test",),
                            records=(replace(manifest.records[0], split="test"),))
    with pytest.raises(EvaluationInputError, match="RUN_PROTOCOL_INVALID"):
        run_three_method_batch(test_manifest, QueryOrchestrator(**ports()),
                               replace(protocol, selected_splits=("test",)), results_root=tmp_path)
    result = run_three_method_batch(manifest, QueryOrchestrator(**ports()), protocol, results_root=tmp_path)
    before = result.manifest_path.read_bytes()
    with pytest.raises(EvaluationInputError, match="RUN_ID_EXISTS"):
        run_three_method_batch(manifest, QueryOrchestrator(**ports()), protocol, results_root=tmp_path)
    assert result.manifest_path.read_bytes() == before


def test_invalid_response_identity_stops_before_completion(tmp_path):
    manifest, protocol = setup(1)
    runner = QueryOrchestrator(**ports())
    real = runner.run_prepared
    def wrong(prepared, method):
        response = real(prepared, method)
        return response.model_copy(update={"query_id": "wrong"}) if method == "dynamic" else response
    runner.run_prepared = wrong
    with pytest.raises(EvaluationInputError, match="PREDICTION_INVALID"):
        run_three_method_batch(manifest, runner, protocol, results_root=tmp_path)
    assert not (tmp_path / protocol.run_id / "run_manifest.json").exists()


@pytest.mark.parametrize("mutation", ["method", "duplicate"])
def test_wrong_method_or_duplicate_query_rejects_entire_batch(tmp_path, mutation):
    manifest, protocol = setup(2)
    runner = QueryOrchestrator(**ports())
    real = runner.run_prepared
    calls = {"dynamic": 0}
    def wrong(prepared, method):
        response = real(prepared, method)
        if method == "dynamic":
            calls["dynamic"] += 1
            if mutation == "method":
                return response.model_copy(update={"method": "vector_rag", "resolution": None})
            if calls["dynamic"] == 2:
                return response.model_copy(update={"query_id": manifest.records[0].query_id})
        return response
    runner.run_prepared = wrong
    with pytest.raises(EvaluationInputError, match="PREDICTION_"):
        run_three_method_batch(manifest, runner, protocol, results_root=tmp_path)
    assert not (tmp_path / protocol.run_id / "run_manifest.json").exists()


def test_third_save_failure_preserves_first_two_without_completion(tmp_path, monkeypatch):
    manifest, protocol = setup(1)
    real_save = batch_module.save_predictions
    calls = []
    def broken(path, manifest_arg, responses, method):
        calls.append(method)
        if method == "dynamic":
            raise EvaluationInputError("PREDICTION_SAVE_FAILED")
        return real_save(path, manifest_arg, responses, method)
    monkeypatch.setattr(batch_module, "save_predictions", broken)
    with pytest.raises(EvaluationInputError, match="PREDICTION_SAVE_FAILED"):
        run_three_method_batch(manifest, QueryOrchestrator(**ports()), protocol, results_root=tmp_path)
    assert calls == ["full_text", "vector_rag", "dynamic"]
    run_dir = tmp_path / protocol.run_id
    assert (run_dir / "full_text.predictions.json").is_file()
    assert (run_dir / "vector_rag.predictions.json").is_file()
    assert not (run_dir / "run_manifest.json").exists()
    with pytest.raises(EvaluationInputError, match="RUN_INCOMPLETE"):
        verify_run(run_dir, manifest)


def test_verify_run_detects_byte_and_manifest_tamper_and_missing_file(tmp_path):
    manifest, protocol = setup(1)
    result = run_three_method_batch(manifest, QueryOrchestrator(**ports()), protocol, results_root=tmp_path)
    target = result.directory / "vector_rag.predictions.json"
    target.write_bytes(target.read_bytes() + b" ")
    with pytest.raises(EvaluationInputError, match="PREDICTION_HASH_MISMATCH"):
        verify_run(result.directory, manifest)
    target.write_bytes(target.read_bytes()[:-1])
    result.manifest_path.write_bytes(result.manifest_path.read_bytes() + b" ")
    with pytest.raises(EvaluationInputError, match="RUN_MANIFEST_HASH_MISMATCH"):
        verify_run(result.directory, manifest)
    result.manifest_path.write_bytes(result.manifest_path.read_bytes()[:-1])
    target.unlink()
    with pytest.raises(EvaluationInputError, match="PREDICTION_NOT_FOUND"):
        verify_run(result.directory, manifest)


def test_rehashed_manifest_cannot_change_reported_counts(tmp_path):
    manifest, protocol = setup(1)
    result = run_three_method_batch(manifest, QueryOrchestrator(**ports()), protocol, results_root=tmp_path)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    payload["predictions"][0]["error_count"] = 1
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    result.manifest_path.write_bytes(raw)
    (result.directory / "run_manifest.sha256").write_text(hashlib.sha256(raw).hexdigest() + "\n", encoding="ascii")
    with pytest.raises(EvaluationInputError, match="RUN_MANIFEST_INVALID"):
        verify_run(result.directory, manifest)


def test_external_digest_detects_rehashed_metadata_change(tmp_path):
    manifest, protocol = setup(1)
    result = run_three_method_batch(manifest, QueryOrchestrator(**ports()), protocol, results_root=tmp_path)
    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    payload["started_at"] = "2020-01-01T00:00:00+00:00"
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    result.manifest_path.write_bytes(raw)
    (result.directory / "run_manifest.sha256").write_text(hashlib.sha256(raw).hexdigest() + "\n", encoding="ascii")
    with pytest.raises(EvaluationInputError, match="RUN_MANIFEST_HASH_MISMATCH"):
        verify_run(result.directory, manifest, expected_manifest_sha256=result.manifest_sha256)
