"""Offline supplementary Top-K contract and persistence checks."""

from dataclasses import replace
import hashlib
import json

import pytest

from dynamic_memory.contracts import CandidateItem, CandidateSet, QueryResponse
from dynamic_memory.contracts.memory_id import compute_memory_id
from dynamic_memory.pipeline.query_runner import QueryOrchestrator
from evaluation.predictions import PredictionReceipt, save_predictions, verify_predictions
from evaluation.question_manifest import EvaluationInputError, QuestionManifest, QuestionRecord
from evaluation.runners import RunProtocol, run_pure_vector_ablation, verify_pure_vector_run
from evaluation.runners.three_method_runner import METHODS
from test_run_query_dispatch import SAMPLE, analysis, answered, memory, ports, query, source_repository


def fixture():
    record = QuestionRecord(SAMPLE, "synthetic_q1", 0, query().question, "sha256:" + "1" * 64, "main_dev")
    manifest = QuestionManifest("a" * 64, (record,), ("main_dev",), None)
    protocol = RunProtocol("same-logical-run", manifest.snapshot_sha256, manifest.selected_splits,
        "synthetic-prompt", "synthetic-model", "synthetic-settings", "synthetic-index",
        "synthetic-index-artifact", "latest_known", 2, 4)
    a = memory()
    b = a.model_copy(update={"content": "Second synthetic memory"})
    b = b.model_copy(update={"memory_id": compute_memory_id(b)})
    c = a.model_copy(update={"content": "Third synthetic memory"})
    c = c.model_copy(update={"memory_id": compute_memory_id(c)})
    return manifest, protocol, (a, b, c)


def runner(records, *, raw=None, log=None, answer=None):
    events = log if log is not None else []
    base = ports(event_log=events)
    mapping = {record.memory_id: record for record in records}
    def retrieve(req, _analysis, _index):
        events.append("raw")
        if isinstance(raw, Exception):
            raise raw
        if callable(raw):
            return raw(req)
        if raw is not None:
            return raw
        items = [CandidateItem(memory_id=record.memory_id, rank=i, score=0.9,
            source_refs=record.source_refs, origin="semantic" if i < 3 else "history_expansion")
            for i, record in enumerate(reversed(records), 1)]
        return CandidateSet(query_id=req.query_id, sample_id=req.sample_id, base_top_k=2,
            max_candidates=4, retriever_fingerprint="synthetic-index", items=items)
    base["retrieve_raw_topk"] = retrieve
    base["load_candidates"] = lambda candidates, _index: (
        events.append("load_candidates") or [mapping[item.memory_id] for item in candidates.items])
    base["answer_vector_rag"] = answer or (lambda _q, _a, memories: (
        events.append("rag_answer") or answered(memories[0])))
    return QueryOrchestrator(**base), events


def test_normal_deterministic_run_and_strict_readback(tmp_path):
    manifest, protocol, records = fixture()
    hashes = []
    for directory in (tmp_path / "first", tmp_path / "second"):
        orchestrator, events = runner(records)
        result = run_pure_vector_ablation(manifest, orchestrator, protocol, results_root=directory)
        checked = verify_pure_vector_run(result.directory, manifest,
            expected_manifest_sha256=result.manifest_sha256)
        assert checked.responses == result.responses
        assert events.count("raw") == events.count("rag_answer") == 1
        assert "resolve" not in events and "retrieve" not in events and "dynamic_answer" not in events
        row = result.responses[0]
        expected = sorted((records[1].memory_id, records[2].memory_id))
        assert [item.memory_id for item in row.retrieved.items] == expected
        assert [item.rank for item in row.retrieved.items] == [1, 2]
        assert all(item.origin == "semantic" for item in row.retrieved.items)
        payload = json.loads(result.manifest_path.read_bytes())
        assert payload["initial_topk_ids"] == [{"query_id": row.query_id, "ids": expected, "excluded_candidates": 1}]
        assert payload["excluded_candidates"] == 1
        assert payload["method"] == "pure_vector_topk" and payload["main_experiment"] is False
        assert payload["ablation_type"] == "supplementary_ablation"
        assert payload["history_expansion"] is payload["state_resolution"] is False
        assert payload["prediction_sha256"] == hashlib.sha256(result.receipt.path.read_bytes()).hexdigest()
        hashes.append((result.receipt.path.read_bytes(), result.manifest_path.read_bytes(),
                       result.receipt.file_sha256, result.manifest_sha256))
    assert hashes[0] == hashes[1]
    assert METHODS == ("full_text", "vector_rag", "dynamic")


@pytest.mark.parametrize("kind,code", [
    ("missing", "RAW_RETRIEVER_UNAVAILABLE"),
    ("none", "RAW_RETRIEVER_UNAVAILABLE"),
    ("empty", "RAW_CANDIDATES_EMPTY"),
    ("exception", "RAW_RETRIEVAL_FAILED"),
])
def test_retrieval_failures_are_valid_errors(kind, code):
    _, _, records = fixture()
    orchestrator, events = runner(records)
    if kind == "missing":
        orchestrator._retrieve_raw_topk = None
    elif kind == "none":
        orchestrator._retrieve_raw_topk = lambda *_: None
    elif kind == "empty":
        orchestrator._retrieve_raw_topk = lambda req, *_: CandidateSet(query_id=req.query_id,
            sample_id=req.sample_id, base_top_k=2, max_candidates=2,
            retriever_fingerprint="synthetic-index", items=[])
    else:
        orchestrator._retrieve_raw_topk = lambda *_: (_ for _ in ()).throw(RuntimeError("SECRET"))
    result = orchestrator.run_pure_vector_prepared(orchestrator.prepare_query(query()))
    assert result.response.answer_result.error.code == code
    assert result.response.retrieved is None
    assert QueryResponse.model_validate_json(result.response.model_dump_json()) == result.response
    assert "SECRET" not in result.response.answer_result.error.message
    assert "resolve" not in events and "rag_answer" not in events


@pytest.mark.parametrize("kind", ["query", "sample", "fingerprint", "nan", "infinity", "duplicate", "source"])
def test_invalid_raw_candidates_rejected(kind):
    _, _, records = fixture()
    a = records[0]
    item = CandidateItem(memory_id=a.memory_id, rank=1, score=float("nan") if kind == "nan" else
        float("inf") if kind == "infinity" else 0.5, source_refs=a.source_refs, origin="semantic")
    if kind == "source":
        item = item.model_copy(update={"source_refs": [a.source_refs[0].model_copy(update={"dia_id": "wrong"})]})
    raw = CandidateSet(query_id="wrong" if kind == "query" else query().query_id,
        sample_id="wrong" if kind == "sample" else SAMPLE, base_top_k=2, max_candidates=2,
        retriever_fingerprint="wrong" if kind == "fingerprint" else "synthetic-index",
        items=[item])
    if kind == "duplicate":
        raw = raw.model_copy(update={"items": [item, item.model_copy(update={"rank": 2})]})
    orchestrator, _ = runner(records, raw=raw)
    result = orchestrator.run_pure_vector_prepared(orchestrator.prepare_query(query()),
        retriever_fingerprint="synthetic-index")
    assert result.response.answer_result.error.code == "CANDIDATE_INVALID"


@pytest.mark.parametrize("field", ["query_id", "sample_id", "method"])
def test_prediction_identity_rejected(tmp_path, field):
    manifest, protocol, records = fixture()
    orchestrator, _ = runner(records)
    row = orchestrator.run_pure_vector_prepared(orchestrator.prepare_query(query()),
        base_top_k=protocol.base_top_k).response
    wrong = row.model_copy(update={field: "wrong" if field != "method" else "vector_rag"})
    with pytest.raises(EvaluationInputError, match="PREDICTION_(ID_MISMATCH|INVALID)"):
        save_predictions(tmp_path / "bad.predictions.json", manifest, (wrong,), "pure_vector_topk")


def test_public_dto_shapes_remain_strict():
    _, _, records = fixture()
    orchestrator, _ = runner(records)
    row = orchestrator.run_pure_vector_prepared(orchestrator.prepare_query(query())).response
    assert row.method == "pure_vector_topk" and row.resolution is None
    with pytest.raises(Exception):
        QueryResponse.model_validate({**row.model_dump(mode="python"), "resolution": {"query_id": row.query_id}})
    with pytest.raises(Exception):
        QueryResponse.model_validate({**row.model_dump(mode="python"), "retrieved":
            row.retrieved.model_copy(update={"items": []}).model_dump(mode="python")})
    for method in METHODS:
        base = ports()
        existing = QueryOrchestrator(**base).run_query(query(), method)
        assert QueryResponse.model_validate_json(existing.model_dump_json()) == existing


@pytest.mark.parametrize("failure", ["load", "qa"])
def test_downstream_failure_keeps_verified_candidates_and_readback(tmp_path, failure):
    manifest, protocol, records = fixture()
    orchestrator, events = runner(records)
    if failure == "load":
        def broken_load(*_):
            events.append("load_failure")
            raise RuntimeError("PRIVATE_LOAD_DETAIL")
        orchestrator._load_candidates = broken_load
    else:
        def broken_answer(*_):
            events.append("qa_failure")
            raise RuntimeError("PRIVATE_QA_DETAIL")
        orchestrator._answer_vector_rag = broken_answer
    result = run_pure_vector_ablation(manifest, orchestrator, protocol, results_root=tmp_path)
    row = result.responses[0]
    audit = json.loads(result.manifest_path.read_bytes())["initial_topk_ids"][0]
    assert events.count("raw") == 1
    assert "resolve" not in events and "dynamic_answer" not in events
    assert ("qa_failure" in events) == (failure == "qa")
    assert row.answer_result.status == "error"
    assert row.answer_result.error.code == "COMPONENT_FAILED"
    assert "PRIVATE_" not in row.answer_result.error.message
    assert row.resolution is None and row.answer_result.used_memory_ids == []
    assert row.answer_result.evidence_refs == []
    assert row.retrieved is not None
    assert audit["ids"] == [item.memory_id for item in row.retrieved.items]
    assert audit["excluded_candidates"] == 1
    assert verify_predictions(result.receipt.path, manifest, result.receipt) == result.responses
    assert verify_pure_vector_run(result.directory, manifest).responses == result.responses


@pytest.mark.parametrize("field,rehash", [
    ("ids", False), ("ids", True), ("per_query_excluded", True),
    ("total_excluded", True), ("query_id", True),
])
def test_downstream_failure_audit_tampering_rejected(tmp_path, field, rehash):
    manifest, protocol, records = fixture()
    orchestrator, _ = runner(records)
    orchestrator._load_candidates = lambda *_: (_ for _ in ()).throw(RuntimeError("PRIVATE"))
    result = run_pure_vector_ablation(manifest, orchestrator, protocol, results_root=tmp_path)
    assert verify_pure_vector_run(result.directory, manifest).responses == result.responses
    payload = json.loads(result.manifest_path.read_bytes())
    audit = payload["initial_topk_ids"][0]
    if field == "ids":
        audit["ids"] = ["tampered"]
    elif field == "per_query_excluded":
        audit["excluded_candidates"] += 1
    elif field == "total_excluded":
        payload["excluded_candidates"] += 1
    else:
        audit["query_id"] = "wrong_query"
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    result.manifest_path.write_bytes(raw)
    if rehash:
        (result.directory / "run_manifest.sha256").write_text(hashlib.sha256(raw).hexdigest() + "\n", encoding="ascii")
    with pytest.raises(EvaluationInputError, match="RUN_MANIFEST_(HASH_MISMATCH|INVALID)") as exc:
        verify_pure_vector_run(result.directory, manifest)
    assert str(exc.value).startswith("RUN_MANIFEST_INVALID" if rehash else "RUN_MANIFEST_HASH_MISMATCH")


@pytest.mark.parametrize("failure", ["load", "qa"])
def test_downstream_failure_artifacts_are_byte_deterministic(tmp_path, failure):
    manifest, protocol, records = fixture()
    artifacts = []
    for root in (tmp_path / "first", tmp_path / "second"):
        orchestrator, _ = runner(records)
        def broken(*_):
            raise RuntimeError("PRIVATE_" + failure)
        if failure == "load":
            orchestrator._load_candidates = broken
        else:
            orchestrator._answer_vector_rag = broken
        result = run_pure_vector_ablation(manifest, orchestrator, protocol, results_root=root)
        checked = verify_pure_vector_run(result.directory, manifest,
            expected_manifest_sha256=result.manifest_sha256)
        assert checked.responses == result.responses
        artifacts.append((result.receipt.path.read_bytes(), result.manifest_path.read_bytes(),
            result.receipt.file_sha256, result.manifest_sha256,
            result.responses[0].answer_result.error.code,
            json.loads(result.manifest_path.read_bytes())["initial_topk_ids"]))
    assert artifacts[0] == artifacts[1]
