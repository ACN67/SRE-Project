"""Permanent regressions for the audited ABCE publication and evaluation faults."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from streamlit.testing.v1 import AppTest

from dynamic_memory.contracts import QueryRequest
from dynamic_memory.data.loader import load_conversations
from dynamic_memory.data.repository import SourceRepository
from dynamic_memory.data.snapshot import freeze_split_config, inspect_snapshot
from dynamic_memory.integration.artifacts import ArtifactPorts
from dynamic_memory.integration.offline import (
    _builder, create_system, index_path, prepare_artifacts, prepare_indexes,
    prepare_source,
)
from dynamic_memory.pipeline.settings import load_settings
from dynamic_memory.retrieval.index import fingerprint
from dynamic_memory.services.dynamic_memory_service import ServiceError
from dynamic_memory.web.status import ERROR_MESSAGES
from evaluation.predictions import verify_predictions
from evaluation.question_manifest import QuestionManifest, load_question_manifest
from evaluation.runners.non_d_offline import run_non_d_offline


@pytest.fixture
def prepared(tmp_path):
    root = tmp_path / "state"
    prepare_source(root)
    prepare_artifacts(root)
    prepare_indexes(root)
    return root


def request(sample="synthetic-0", query="repair"):
    return QueryRequest(query_id=query, sample_id=sample, question="What does Ada keep?")


def answer_state(response):
    answer = response.answer_result
    return answer.status, answer.error.code if answer.error else None


@pytest.mark.parametrize("receipt", [False, True])
def test_corrupt_current_preserves_error_and_full_text(prepared, receipt):
    service, _ = create_system(prepared)
    folder = prepared / "artifacts" / "synthetic-0"
    pointer = folder / "current.json"
    original = pointer.read_bytes()
    if not receipt:
        (folder / "integration_receipt.json").unlink()
    pointer.write_text("{bad", encoding="utf-8")
    if receipt:
        assert service.get_build_status("synthetic-0").memory_state == "corrupt"
    else:
        with pytest.raises(ServiceError, match="ARTIFACT_CORRUPT"):
            service.get_build_status("synthetic-0")
    for method in ("vector_rag", "pure_vector_topk"):
        assert answer_state(service.run_query(request(query=method), method)) == ("error", "ARTIFACT_CORRUPT")
    assert answer_state(service.run_query(request(query="full"), "full_text")) == ("answered", None)
    pointer.write_bytes(original)
    assert service.get_build_status("synthetic-0").memory_state == "complete"
    assert answer_state(service.run_query(request(query="restored"), "vector_rag")) == ("answered", None)


def test_stale_manifest_without_receipt_keeps_stale_code(prepared):
    service, _ = create_system(prepared)
    folder = prepared / "artifacts" / "synthetic-0"
    pointer_path = folder / "current.json"
    original_pointer = pointer_path.read_bytes()
    pointer = json.loads(original_pointer)
    manifest_path = folder / "builds" / pointer["build_id"] / "manifest.json"
    original_manifest = manifest_path.read_bytes()
    manifest = json.loads(original_manifest)
    manifest["extraction_config_fingerprint"] = "sha256:" + "f" * 64
    updated = json.dumps(manifest, sort_keys=True).encode()
    manifest_path.write_bytes(updated)
    pointer["manifest_sha256"] = hashlib.sha256(updated).hexdigest()
    pointer_path.write_text(json.dumps(pointer), encoding="utf-8")
    (folder / "integration_receipt.json").unlink()
    with pytest.raises(ServiceError, match="ARTIFACT_STALE"):
        service.get_build_status("synthetic-0")
    assert answer_state(service.run_query(request(), "vector_rag")) == ("error", "ARTIFACT_STALE")
    manifest_path.write_bytes(original_manifest)
    pointer_path.write_bytes(original_pointer)
    assert service.get_build_status("synthetic-0").memory_state == "complete"


def test_real_web_shows_corrupt_without_fabricated_build_id(prepared, monkeypatch):
    monkeypatch.setenv("SRE_INTEGRATION_PROFILE", "offline_synthetic")
    monkeypatch.setenv("SRE_OFFLINE_STATE", str(prepared))
    monkeypatch.setenv("DYNAMIC_MEMORY_WEB_FACTORY", "dynamic_memory.integration.offline:create_web_bindings")
    folder = prepared / "artifacts" / "synthetic-0"
    original = (folder / "current.json").read_bytes()
    (folder / "integration_receipt.json").unlink()
    (folder / "current.json").write_text("{bad", encoding="utf-8")
    app = AppTest.from_file(str(Path(__file__).resolve().parents[2] / "streamlit_app.py")).run()
    assert not app.exception
    next(button for button in app.button if button.label == "进入服务状态 →").click().run()
    assert any("ARTIFACT_CORRUPT" in error.value for error in app.error)
    assert "build_identity" in app.session_state
    next(button for button in app.button if button.label == "← 返回首页").click().run()
    next(button for button in app.button if button.label == "进入问答工作台 →").click().run()
    app.selectbox(key="query_method").select("vector_rag").run()
    app.text_area(key="query_question").set_value("What does Ada keep?")
    next(button for button in app.button if button.label == "提交问题").click().run()
    assert answer_state(app.session_state["query_response"]) == ("error", "ARTIFACT_CORRUPT")
    (folder / "current.json").write_bytes(original)
    app.run()
    assert "query_response" not in app.session_state
    next(button for button in app.button if button.label == "提交问题").click().run()
    assert answer_state(app.session_state["query_response"]) == ("answered", None)


@pytest.mark.parametrize("attempt,expected", [("failed", "MEMORY_BUILD_FAILED"),
                                               ("partial", "ARTIFACT_INCOMPLETE")])
def test_real_builder_failed_or_partial_status_reaches_web(tmp_path, monkeypatch, attempt, expected):
    root = tmp_path / "state"
    prepare_source(root)
    bundle = load_conversations(str(root / "synthetic_locomo.json"), str(root / "split.yaml"))
    repository = SourceRepository(bundle)
    sample = repository.get_sample("synthetic-0")
    ports = ArtifactPorts(root / "artifacts", bundle, load_settings())
    builder = _builder(repository, load_settings(), sample)
    if attempt == "failed":
        builder.evidence_checker = None
    else:
        original = builder.evidence_checker
        from dynamic_memory.memory.normalization import EvidenceAssessment
        builder.evidence_checker = lambda draft, turns: (
            EvidenceAssessment("unclear", "SYNTHETIC_REJECT") if "plans" in draft.content
            else original(draft, turns))
    report = builder.build_memories_with_report(sample)
    assert report.build_status == attempt
    ports.record_failed_attempt(sample.sample_id, attempt)
    service, _ = create_system(root)
    assert service.get_build_status(sample.sample_id).memory_state == attempt
    assert answer_state(service.run_query(request(), "vector_rag")) == ("error", expected)
    assert answer_state(service.run_query(request(query="full"), "full_text")) == ("answered", None)
    monkeypatch.setenv("SRE_INTEGRATION_PROFILE", "offline_synthetic")
    monkeypatch.setenv("SRE_OFFLINE_STATE", str(root))
    monkeypatch.setenv("DYNAMIC_MEMORY_WEB_FACTORY", "dynamic_memory.integration.offline:create_web_bindings")
    app = AppTest.from_file(str(Path(__file__).resolve().parents[2] / "streamlit_app.py")).run()
    assert not app.exception
    next(button for button in app.button if button.label == "进入服务状态 →").click().run()
    assert any(ERROR_MESSAGES[expected] in warning.value for warning in app.warning)


def test_pure_vector_missing_build_and_time_conflict(tmp_path):
    root = tmp_path / "state"
    prepare_source(root)
    service, _ = create_system(root)
    assert answer_state(service.run_query(request(), "pure_vector_topk")) == ("error", "BUILD_NOT_FOUND")
    from datetime import date
    conflict = QueryRequest(query_id="conflict", sample_id="synthetic-0",
                            question="What happened on 2024-01-01?", as_of=date(2023, 1, 1))
    response = service.run_query(conflict, "pure_vector_topk")
    assert response.answer_result.status == "time_uncertain"
    assert response.retrieved is None and response.query_analysis.time_mode == "conflict"


def test_complete_zero_memory_is_not_failure_in_service_or_web(tmp_path, monkeypatch):
    root = tmp_path / "state"
    prepare_source(root)
    source = root / "synthetic_locomo.json"
    document = json.loads(source.read_text(encoding="utf-8"))
    document[0]["conversation"] = {"session_1": []}
    source.write_text(json.dumps(document), encoding="utf-8")
    split = root / "split.yaml"
    split.write_text(
        "snapshot_sha256: null\nsample_ids: null\ngroups:\n"
        "  main_dev: {source_order_1_based: [1, 2, 3, 4], sample_ids: null}\n"
        "  extra_dev: {source_order_1_based: [5, 6, 7, 8], sample_ids: null}\n"
        "  test: {source_order_1_based: [9, 10], sample_ids: null}\n", encoding="utf-8")
    freeze_split_config(inspect_snapshot(source), split, apply=True)
    prepare_artifacts(root, ("synthetic-0",))
    prepare_indexes(root, ("synthetic-0",))
    service, _ = create_system(root)
    assert service.get_build_status("synthetic-0").memory_state == "complete"
    assert service.list_memories("synthetic-0") == []
    assert answer_state(service.run_query(request(), "vector_rag")) == ("no_evidence", None)
    monkeypatch.setenv("SRE_INTEGRATION_PROFILE", "offline_synthetic")
    monkeypatch.setenv("SRE_OFFLINE_STATE", str(root))
    monkeypatch.setenv("DYNAMIC_MEMORY_WEB_FACTORY", "dynamic_memory.integration.offline:create_web_bindings")
    app = AppTest.from_file(str(Path(__file__).resolve().parents[2] / "streamlit_app.py")).run()
    assert not app.exception
    next(button for button in app.button if button.label == "进入记忆浏览 →").click().run()
    assert any("尚无记忆" in info.value for info in app.info)
    next(button for button in app.button if button.label == "← 返回首页").click().run()
    next(button for button in app.button if button.label == "进入服务状态 →").click().run()
    assert any("完整构建" in metric.value for metric in app.metric)
    assert "dynamic" not in [option for box in app.selectbox for option in box.options]


@pytest.mark.parametrize("change,expected", [("build", "ARTIFACT_STALE"), ("index", "INDEX_STALE")])
@pytest.mark.parametrize("method", ["vector_rag", "pure_vector_topk"])
def test_generation_switch_rejects_both_methods(prepared, change, expected, method):
    service, _ = create_system(prepared)
    original = service._query_orchestrator._answer_vector_rag
    def switch(query, analysis, memories):
        answer = original(query, analysis, memories)
        if change == "build":
            bundle = load_conversations(str(prepared / "synthetic_locomo.json"), str(prepared / "split.yaml"))
            ports = ArtifactPorts(prepared / "artifacts", bundle, load_settings())
            sample = service._sources.get_sample("synthetic-0")
            report = _builder(service._sources, load_settings(), sample).build_memories_with_report(sample)
            ports.record_verified(ports.store(sample.sample_id).publish(report))
        else:
            path = index_path(prepared, "synthetic-0")
            doc = json.loads(path.read_text(encoding="utf-8"))
            doc["payload"]["vectors"] = [[0.0, 1.0] for _ in doc["payload"]["vectors"]]
            doc["payload_fingerprint"] = fingerprint(doc["payload"])
            path.write_text(json.dumps(doc), encoding="utf-8")
        return answer
    service._query_orchestrator._answer_vector_rag = switch
    response = service.run_query(request(), method)
    assert answer_state(response) == ("error", expected)
    assert response.method == method and response.retrieved is None


@pytest.mark.parametrize("change,expected", [("build", "ARTIFACT_STALE"), ("index", "INDEX_STALE")])
def test_pure_prediction_saved_after_switch_is_error(prepared, tmp_path, change, expected):
    service, orchestrator = create_system(prepared)
    manifest = load_question_manifest(prepared / "synthetic_locomo.json", prepared / "split.yaml",
                                      selected_splits=("main_dev",))
    one = QuestionManifest(manifest.snapshot_sha256, (manifest.records[0],),
                           manifest.selected_splits, manifest.official_id_field)
    original = service._query_orchestrator._answer_vector_rag
    calls = 0
    def switch_on_pure(query, analysis, memories):
        nonlocal calls
        answer = original(query, analysis, memories)
        calls += 1
        if calls == 2:
            if change == "build":
                bundle = load_conversations(str(prepared / "synthetic_locomo.json"), str(prepared / "split.yaml"))
                ports = ArtifactPorts(prepared / "artifacts", bundle, load_settings())
                sample = service._sources.get_sample("synthetic-0")
                report = _builder(service._sources, load_settings(), sample).build_memories_with_report(sample)
                ports.record_verified(ports.store(sample.sample_id).publish(report))
            else:
                path = index_path(prepared, "synthetic-0")
                doc = json.loads(path.read_text(encoding="utf-8"))
                doc["payload"]["vectors"] = [[0.0, 1.0] for _ in doc["payload"]["vectors"]]
                doc["payload_fingerprint"] = fingerprint(doc["payload"])
                path.write_text(json.dumps(doc), encoding="utf-8")
        return answer
    service._query_orchestrator._answer_vector_rag = switch_on_pure
    receipts = run_non_d_offline(one, service, orchestrator, tmp_path / "results")
    rows = verify_predictions(receipts["pure_vector_topk"].path, one, receipts["pure_vector_topk"])
    assert calls == 2
    assert answer_state(rows[0]) == ("error", expected)
    assert answer_state(verify_predictions(receipts["vector_rag"].path, one, receipts["vector_rag"])[0]) == ("answered", None)


def test_public_cli_prepares_every_main_dev_question_and_preserves_failure_count(tmp_path):
    root = tmp_path / "state"
    script = Path(__file__).resolve().parents[2] / "scripts" / "offline_integration.py"
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    def run(action, results=None):
        command = [sys.executable, "-B", str(script), action, "--state", str(root)]
        if results is not None:
            command += ["--results", str(results)]
        completed = subprocess.run(command, capture_output=True, text=True, env=env, timeout=60)
        assert completed.returncode == 0, completed.stderr
        return completed.stdout
    prepared_output = json.loads(run("prepare"))
    assert set(prepared_output["builds"]) == {f"synthetic-{i}" for i in range(4)}
    assert set(prepared_output["indexes"]) == set(prepared_output["builds"])
    assert run("check").count("complete; full_text=answered; vector_rag=answered") == 4
    results = tmp_path / "results"
    run("evaluate", results)
    summary = json.loads((results / "non_d_summary.json").read_text(encoding="utf-8"))
    assert summary["question_count"] == 4
    assert all(counts["answered"] == 4 and counts["error"] == 0
               for counts in summary["answer_status_counts"].values())
    manifest = load_question_manifest(root / "synthetic_locomo.json", root / "split.yaml",
                                      selected_splits=("main_dev",))
    assert [record.sample_id for record in manifest.records] == [f"synthetic-{i}" for i in range(4)]
    index_path(root, "synthetic-3").unlink()
    failed = tmp_path / "results_one_missing"
    run("evaluate", failed)
    summary = json.loads((failed / "non_d_summary.json").read_text(encoding="utf-8"))
    assert summary["question_count"] == 4
    assert summary["answer_status_counts"]["full_text"]["answered"] == 4
    for method in ("vector_rag", "pure_vector_topk"):
        assert summary["answer_status_counts"][method]["answered"] == 3
        assert summary["answer_status_counts"][method]["error"] == 1
        assert summary["error_code_counts"][method] == {"INDEX_NOT_FOUND": 1}
