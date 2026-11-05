"""Direct-main synthetic B builder -> C index -> A service -> E factory checks."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from dynamic_memory.contracts import (AnswerResult, ConversationSample, DatasetBundle,
                                      MemoryDecision, QueryRequest, ResolutionResult, SourceRef)
from dynamic_memory.data.loader import load_conversations
from dynamic_memory.data.repository import SourceRepository
from dynamic_memory.integration.artifacts import ArtifactPorts
from dynamic_memory.integration.offline import (
    FixedEncoder, OfflineConfigurationError, _builder, create_service,
    create_web_bindings, create_system, index_path, prepare_artifacts, prepare_indexes,
    prepare_source, state_root,
)
from dynamic_memory.memory.artifact_store import ArtifactStoreError
from dynamic_memory.pipeline.settings import load_settings
from dynamic_memory.memory.normalization import EvidenceAssessment
from dynamic_memory.services.dynamic_memory_service import DynamicMemoryService
from dynamic_memory.retrieval.index import IndexManager, RetrievalIndexError, fingerprint
from dynamic_memory.web.bindings import WebBindings
from evaluation.gold_loader import load_gold_after_predictions
from evaluation.predictions import verify_predictions
from evaluation.question_manifest import EvaluationInputError, load_question_manifest
from evaluation.runners.non_d_offline import run_non_d_offline


@pytest.fixture
def prepared(tmp_path):
    root = tmp_path / "state"
    prepare_source(root)
    builds = prepare_artifacts(root)
    indexes = prepare_indexes(root)
    return root, builds, indexes


def _request(sample, number=1, *, question="What does Ada keep?"):
    return QueryRequest(query_id=f"offline_{sample}_{number}", sample_id=sample,
                        question=question)


def test_raw_source_builder_to_bcae_and_two_samples(monkeypatch, prepared):
    root, builds, indexes = prepared
    bundle = load_conversations(*map(str, (root / "synthetic_locomo.json", root / "split.yaml")))
    assert len(bundle.samples) == 10 and len(builds) == len(indexes) == 2
    assert not any("qa" in sample.model_dump() for sample in bundle.samples)
    service = create_service(root)
    monkeypatch.setenv("SRE_INTEGRATION_PROFILE", "offline_synthetic")
    monkeypatch.setenv("SRE_OFFLINE_STATE", str(root))
    monkeypatch.setenv("DYNAMIC_MEMORY_WEB_FACTORY", "dynamic_memory.integration.offline:create_web_bindings")
    bindings = create_web_bindings()
    assert isinstance(bindings, WebBindings) and bindings.synthetic and not bindings.dynamic_available
    for sample in ("synthetic-0", "synthetic-1"):
        assert service.get_build_status(sample).build_id == builds[sample]
        memories = bindings.list_memories(sample)
        assert len(memories) == 2
        for memory in memories:
            assert bindings.get_memory(sample, memory.memory_id) == memory
            for ref in memory.source_refs:
                assert bindings.get_turn(sample, ref).text == memory.content
        for method in ("full_text", "vector_rag"):
            response = bindings.run_query(_request(sample), method)
            assert response.answer_result.status == "answered"
            assert response.answer_result.answer == "Synthetic offline answer."
            if method == "vector_rag":
                assert response.retrieved is not None and len(response.retrieved.items) == 2
                assert response.answer_result.evidence_refs
        dynamic = bindings.run_query(_request(sample), "dynamic")
        assert dynamic.answer_result.status == "error"
        assert dynamic.answer_result.error.code == "COMPONENT_UNAVAILABLE"
    app = AppTest.from_file(str(Path(__file__).resolve().parents[2] / "streamlit_app.py")).run()
    assert not app.exception
    assert any("Synthetic" in warning.value for warning in app.warning)
    next(b for b in app.button if b.label == "进入问答工作台 →").click().run()
    assert not app.exception
    app.text_area(key="query_question").set_value("What does Ada keep?")
    next(b for b in app.button if b.label == "提交问题").click().run()
    assert not app.exception
    assert any("Synthetic offline answer." in item.value for item in app.text)
    app.selectbox(key="query_method").select("vector_rag").run()
    next(b for b in app.button if b.label == "提交问题").click().run()
    assert not app.exception
    assert any("Synthetic offline answer." in item.value for item in app.text)
    assert app.session_state["query_response"].answer_result.status == "answered"
    assert "dynamic" not in [option for box in app.selectbox for option in box.options]


def test_profile_and_missing_resources_fail_closed(monkeypatch, tmp_path, prepared):
    root, _, _ = prepared
    with pytest.raises(OfflineConfigurationError, match="OFFLINE_STATE_INVALID"):
        state_root("relative/path")
    with pytest.raises(OfflineConfigurationError, match="OFFLINE_SOURCE_EXISTS"):
        prepare_source(root)
    with pytest.raises(OfflineConfigurationError, match="OFFLINE_ARTIFACT_EXISTS"):
        prepare_artifacts(root)
    monkeypatch.delenv("SRE_INTEGRATION_PROFILE", raising=False)
    monkeypatch.setenv("SRE_OFFLINE_STATE", str(root))
    with pytest.raises(OfflineConfigurationError, match="OFFLINE_PROFILE_REQUIRED"):
        create_web_bindings()
    service = create_service(root)
    absent = service.get_build_status("synthetic-2")
    assert absent.memory_state == "not_built" and absent.error_code == "BUILD_NOT_FOUND"
    assert service.run_query(_request("synthetic-2"), "full_text").answer_result.status == "answered"
    assert service.run_query(_request("synthetic-2"), "vector_rag").answer_result.error.code == "BUILD_NOT_FOUND"
    with pytest.raises(OfflineConfigurationError, match="SAMPLE_MISMATCH"):
        index_path(root, "../synthetic-0")


def test_current_switch_stales_index_and_corruption_is_distinct(prepared):
    root, builds, _ = prepared
    service = create_service(root)
    bundle = load_conversations(*map(str, (root / "synthetic_locomo.json", root / "split.yaml")))
    ports = ArtifactPorts(root / "artifacts", bundle, load_settings())
    sample = next(s for s in bundle.samples if s.sample_id == "synthetic-0")
    report = _builder(service._sources, load_settings(), sample).build_memories_with_report(sample)
    fresh = ports.store(sample.sample_id).publish(report)
    ports.record_verified(fresh)
    assert fresh.manifest.build_id != builds[sample.sample_id]
    assert service.get_build_status(sample.sample_id).build_id == fresh.manifest.build_id
    stale = service.run_query(_request(sample.sample_id), "vector_rag")
    assert stale.answer_result.status == "error" and stale.answer_result.error.code == "INDEX_STALE"
    assert service.run_query(_request(sample.sample_id), "full_text").answer_result.status == "answered"
    (root / "artifacts" / sample.sample_id / "current.json").write_text("{bad", encoding="utf-8")
    status = service.get_build_status(sample.sample_id)
    assert status.memory_state == "corrupt" and status.error_code == "ARTIFACT_CORRUPT"
    assert service.run_query(_request(sample.sample_id), "vector_rag").answer_result.error.code == "ARTIFACT_CORRUPT"


def test_index_missing_corrupt_and_sample_isolation(prepared):
    root, _, _ = prepared
    service = create_service(root)
    index_path(root, "synthetic-0").unlink()
    assert service.run_query(_request("synthetic-0"), "vector_rag").answer_result.error.code == "INDEX_NOT_FOUND"
    index_path(root, "synthetic-1").write_text("corrupt", encoding="utf-8")
    assert service.run_query(_request("synthetic-1"), "vector_rag").answer_result.error.code == "INDEX_CORRUPT"
    with pytest.raises(ArtifactStoreError, match="SAMPLE_MISMATCH"):
        ArtifactPorts(root / "artifacts", load_conversations(*map(str, (
            root / "synthetic_locomo.json", root / "split.yaml"))), load_settings()).load("../synthetic-0")


def test_concurrent_queries_keep_sample_and_sources(prepared):
    root, _, _ = prepared
    service = create_service(root)
    def one(i):
        sample = f"synthetic-{i % 2}"
        return sample, service.run_query(_request(sample, i + 1), "vector_rag")
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(one, range(20)))
    for sample, response in rows:
        assert response.answer_result.status == "answered"
        assert response.sample_id == sample
        assert all(service.get_memory(sample, item.memory_id).sample_id == sample
                   for item in response.retrieved.items)


def test_fake_d_only_checks_existing_public_ports(prepared):
    root, _, _ = prepared
    seen = []
    def resolve(request, analysis, memories):
        seen.append((request.query_id, analysis.query_id, tuple(m.memory_id for m in memories)))
        return ResolutionResult(query_id=request.query_id,
            decisions=[MemoryDecision(memory_id=m.memory_id, action="RETAIN", reason="Synthetic contract")
                       for m in memories], effective_memory_ids=[m.memory_id for m in memories])
    def answer(request, analysis, resolution, memories):
        refs = [ref for m in memories for ref in m.source_refs]
        return AnswerResult(status="answered", answer="Synthetic Fake D answer.",
            used_memory_ids=[m.memory_id for m in memories], context_source_refs=refs,
            evidence_refs=refs[:1])
    service = create_service(root, resolve=resolve, answer_dynamic=answer)
    request = _request(
        "synthetic-0", question="Is Ada still keeping the blue notebook?"
    )
    vector = service.run_query(request, "vector_rag")
    dynamic = service.run_query(request, "dynamic")
    assert dynamic.answer_result.status == "answered"
    assert dynamic.retrieved == vector.retrieved
    assert seen and seen[0][0] == seen[0][1] == request.query_id
    assert seen[0][2] == tuple(item.memory_id for item in vector.retrieved.items)


def test_fake_d_invalid_candidate_is_rejected(prepared):
    root, _, _ = prepared
    def invalid(request, analysis, memories):
        return ResolutionResult(query_id=request.query_id,
            decisions=[MemoryDecision(memory_id="mem_" + "f" * 20,
                                      action="RETAIN", reason="Synthetic invalid")],
            effective_memory_ids=["mem_" + "f" * 20])
    service = create_service(root, resolve=invalid,
        answer_dynamic=lambda *_: AnswerResult(status="answered", answer="Should not run."))
    response = service.run_query(
        _request("synthetic-0", question="Is Ada still keeping the blue notebook?"),
        "dynamic",
    )
    assert response.answer_result.status == "error"
    assert response.answer_result.error.code in ("RESOLUTION_INVALID", "QUERY_EXECUTION_FAILED")


def test_non_d_predictions_are_persisted_and_gold_is_gated(prepared, tmp_path):
    root, _, _ = prepared
    manifest = load_question_manifest(root / "synthetic_locomo.json", root / "split.yaml",
                                      selected_splits=("main_dev",))
    service, orchestrator = create_system(root)
    receipts = run_non_d_offline(manifest, service, orchestrator,
                                 tmp_path / "predictions")
    assert set(receipts) == {"full_text", "vector_rag", "pure_vector_topk"}
    full = verify_predictions(receipts["full_text"].path, manifest, receipts["full_text"])
    vector = verify_predictions(receipts["vector_rag"].path, manifest, receipts["vector_rag"])
    raw = verify_predictions(receipts["pure_vector_topk"].path, manifest,
                             receipts["pure_vector_topk"])
    assert len(full) == len(vector) == len(raw) == 4
    assert all(row.answer_result.status == "answered" for row in full)
    assert [row.answer_result.status for row in vector] == ["answered", "answered", "error", "error"]
    assert all(row.method == "pure_vector_topk" for row in raw)
    assert raw[0].retrieved.items and all(item.origin == "semantic" for item in raw[0].retrieved.items)
    summary = json.loads((tmp_path / "predictions" / "non_d_summary.json").read_text(encoding="utf-8"))
    assert summary["question_count"] == len(manifest.records) == 4
    assert summary["answer_status_counts"]["full_text"] == {
        "answered": 4, "no_evidence": 0, "time_uncertain": 0,
        "unresolved_conflict": 0, "error": 0,
    }
    assert summary["answer_status_counts"]["vector_rag"] == {
        "answered": 2, "no_evidence": 0, "time_uncertain": 0,
        "unresolved_conflict": 0, "error": 2,
    }
    for method, rows in (("full_text", full), ("vector_rag", vector), ("pure_vector_topk", raw)):
        statuses = summary["answer_status_counts"][method]
        assert sum(statuses.values()) == len(rows)
        assert statuses["answered"] == sum(row.answer_result.status == "answered" for row in rows)
        assert statuses["error"] == sum(row.answer_result.status == "error" for row in rows)
        assert sum(summary["error_code_counts"][method].values()) == statuses["error"]
    assert summary["prediction_sha256"] == {
        method: receipt.file_sha256 for method, receipt in receipts.items()
    }
    with pytest.raises(ValueError, match="OFFLINE_RUN_EXISTS"):
        run_non_d_offline(manifest, service, orchestrator,
                          tmp_path / "predictions")
    receipt = receipts["full_text"]
    receipt.path.write_bytes(receipt.path.read_bytes() + b"tamper")
    with pytest.raises(EvaluationInputError, match="PREDICTION_HASH_MISMATCH"):
        load_gold_after_predictions(root / "synthetic_locomo.json", root / "split.yaml",
                                    receipt.path, manifest, receipt,
                                    annotation_source="synthetic_fixture")


def test_non_d_summary_counts_no_evidence_from_verified_predictions(prepared, tmp_path):
    root, _, _ = prepared
    manifest = load_question_manifest(root / "synthetic_locomo.json", root / "split.yaml",
                                      selected_splits=("main_dev",))
    service, orchestrator = create_system(root)
    original = service.run_query

    def one_unknown_answer(request, method="dynamic"):
        response = original(request, method)
        if method == "full_text" and request.sample_id == "synthetic-0":
            return response.model_copy(update={"answer_result": AnswerResult(
                status="no_evidence", uncertainty="Synthetic test has no evidence."
            )})
        return response

    service.run_query = one_unknown_answer
    run_non_d_offline(manifest, service, orchestrator, tmp_path / "predictions")
    summary = json.loads((tmp_path / "predictions" / "non_d_summary.json").read_text(encoding="utf-8"))
    counts = summary["answer_status_counts"]["full_text"]
    assert counts["no_evidence"] == 1
    assert counts["answered"] == 3
    assert counts["error"] == 0
    assert sum(counts.values()) == summary["question_count"] == 4


def test_web_clears_answer_after_index_change(monkeypatch, prepared):
    root, _, _ = prepared
    monkeypatch.setenv("SRE_INTEGRATION_PROFILE", "offline_synthetic")
    monkeypatch.setenv("SRE_OFFLINE_STATE", str(root))
    monkeypatch.setenv("DYNAMIC_MEMORY_WEB_FACTORY", "dynamic_memory.integration.offline:create_web_bindings")
    app = AppTest.from_file(str(Path(__file__).resolve().parents[2] / "streamlit_app.py")).run()
    next(b for b in app.button if b.label == "进入问答工作台 →").click().run()
    app.selectbox(key="query_method").select("vector_rag").run()
    app.text_area(key="query_question").set_value("What does Ada keep?")
    next(b for b in app.button if b.label == "提交问题").click().run()
    assert app.session_state["query_response"].answer_result.status == "answered"
    index_path(root, "synthetic-0").write_text("corrupt", encoding="utf-8")
    app.run()
    assert "query_response" not in app.session_state


def test_real_factory_web_browses_source_switches_sample_and_refreshes(monkeypatch, prepared):
    root, _, _ = prepared
    monkeypatch.setenv("SRE_INTEGRATION_PROFILE", "offline_synthetic")
    monkeypatch.setenv("SRE_OFFLINE_STATE", str(root))
    monkeypatch.setenv("DYNAMIC_MEMORY_WEB_FACTORY", "dynamic_memory.integration.offline:create_web_bindings")
    app = AppTest.from_file(str(Path(__file__).resolve().parents[2] / "streamlit_app.py")).run()
    first_binding = app.session_state["bindings"]
    next(b for b in app.button if b.label == "进入记忆浏览 →").click().run()
    assert not app.exception
    assert len([b for b in app.button if b.label == "查看详情"]) == 2
    next(b for b in app.button if b.label == "查看详情").click().run()
    next(b for b in app.button if b.label.startswith("↗ session_")).click().run()
    assert not app.exception
    assert any("Ada" in item.value for item in app.text)
    next(b for b in app.button if b.label == "← 返回首页").click().run()
    app.selectbox(key="sample_choice").select("synthetic-1").run()
    assert app.session_state["sample"] == "synthetic-1"
    assert "selected_memory" not in app.session_state
    next(b for b in app.button if b.label == "刷新服务").click().run()
    assert not app.exception
    assert app.session_state["bindings"] is not first_binding


def test_failed_builder_attempt_has_distinct_status_without_publication(tmp_path):
    root = tmp_path / "state"
    prepare_source(root)
    bundle = load_conversations(*map(str, (root / "synthetic_locomo.json", root / "split.yaml")))
    settings = load_settings()
    repository = SourceRepository(bundle)
    sample = repository.get_sample("synthetic-0")
    ports = ArtifactPorts(root / "artifacts", bundle, settings)
    builder = _builder(repository, settings, sample)
    builder.evidence_checker = None
    failed = builder.build_memories_with_report(sample)
    assert failed.build_status == "failed" and not failed.memories
    ports.record_failed_attempt(sample.sample_id, failed.build_status)
    status = ports.build_status(sample.sample_id)
    assert status.memory_state == "failed" and status.error_code == "MEMORY_BUILD_FAILED"
    assert not (ports.store(sample.sample_id).root / "current.json").exists()
    with pytest.raises(ArtifactStoreError, match="BUILD_NOT_FOUND"):
        ports.load(sample.sample_id)


def test_partial_builder_attempt_and_complete_zero_memory_are_distinct(tmp_path):
    root = tmp_path / "state"
    prepare_source(root)
    bundle = load_conversations(*map(str, (root / "synthetic_locomo.json", root / "split.yaml")))
    settings = load_settings()
    repository = SourceRepository(bundle)
    sample = repository.get_sample("synthetic-0")
    ports = ArtifactPorts(root / "artifacts", bundle, settings)
    builder = _builder(repository, settings, sample)
    original_checker = builder.evidence_checker
    builder.evidence_checker = lambda draft, turns: (
        EvidenceAssessment("unclear", "SYNTHETIC_REJECT") if "plans" in draft.content
        else original_checker(draft, turns))
    partial = builder.build_memories_with_report(sample)
    assert partial.build_status == "partial" and partial.partial_build
    ports.record_failed_attempt(sample.sample_id, partial.build_status)
    assert ports.build_status(sample.sample_id).memory_state == "partial"
    assert not (ports.store(sample.sample_id).root / "current.json").exists()

    empty = ConversationSample(sample_id="synthetic-empty", source_order_1_based=1,
                               sessions=[])
    empty_bundle = DatasetBundle(snapshot_sha256="a" * 64, samples=[empty],
                                 split_by_sample_id={empty.sample_id: "main_dev"})
    empty_repo = SourceRepository(empty_bundle)
    empty_ports = ArtifactPorts(tmp_path / "empty-artifacts", empty_bundle, settings)
    report = _builder(empty_repo, settings, empty).build_memories_with_report(empty)
    assert report.build_status == "complete" and not report.memories
    artifact = empty_ports.store(empty.sample_id).publish(report)
    empty_ports.record_verified(artifact)
    service = DynamicMemoryService(sources=empty_repo, build_status=empty_ports.build_status,
                                   load_verified_artifact=empty_ports.verified)
    assert service.get_build_status(empty.sample_id).memory_state == "complete"
    assert service.list_memories(empty.sample_id) == []


def test_fake_d_excluded_memory_cannot_enter_answer(prepared):
    root, _, _ = prepared
    def resolve(request, analysis, memories):
        return ResolutionResult(query_id=request.query_id,
            decisions=[MemoryDecision(memory_id=m.memory_id, action="EXCLUDE", reason="Synthetic")
                       for m in memories], effective_memory_ids=[])
    def bad_answer(request, analysis, resolution, memories):
        refs = [ref for m in memories for ref in m.source_refs]
        return AnswerResult(status="answered", answer="Invalid Fake D answer",
            used_memory_ids=[memories[0].memory_id], context_source_refs=refs,
            evidence_refs=refs[:1])
    service = create_service(root, resolve=resolve, answer_dynamic=bad_answer)
    response = service.run_query(
        _request("synthetic-0", question="Is Ada still keeping the blue notebook?"),
        "dynamic",
    )
    assert response.answer_result.status == "error"


@pytest.mark.parametrize(("change", "expected_code"), [
    ("corrupt", "INDEX_CORRUPT"),
    ("missing", "INDEX_NOT_FOUND"),
    ("valid_replacement", "INDEX_STALE"),
])
def test_index_change_during_answer_rejects_response(prepared, change, expected_code):
    """Reject a changed index at the final request checkpoint, not just before QA."""
    root, _, _ = prepared
    service = create_service(root)
    path = index_path(root, "synthetic-0")
    original = service._query_orchestrator._answer_vector_rag

    def switch_after_generation(request, analysis, memories):
        answer = original(request, analysis, memories)
        if change == "corrupt":
            path.write_text("corrupt", encoding="utf-8")
        elif change == "missing":
            path.unlink()
        else:
            document = json.loads(path.read_text(encoding="utf-8"))
            original_retriever = document["payload"]["retriever_fingerprint"]
            original_digest = document["payload_fingerprint"]
            # A valid new payload with unchanged declared retriever identity.
            # Checking retriever_fingerprint alone cannot detect this swap.
            document["payload"]["vectors"] = [
                [0.0, 1.0] for _ in document["payload"]["vectors"]
            ]
            document["payload_fingerprint"] = fingerprint(document["payload"])
            assert document["payload_fingerprint"] != original_digest
            assert document["payload"]["retriever_fingerprint"] == original_retriever
            path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
        return answer

    service._query_orchestrator._answer_vector_rag = switch_after_generation
    response = service.run_query(_request("synthetic-0"), "vector_rag")
    assert response.answer_result.status == "error"
    assert response.answer_result.error.code == expected_code
    # Full Text must stay independent from Memory/index readiness.
    assert service.run_query(_request("synthetic-0", 2), "full_text").answer_result.status == "answered"


def test_publication_switch_during_answer_rejects_mixed_response(prepared):
    root, _, _ = prepared
    service = create_service(root)
    bundle = load_conversations(*map(str, (root / "synthetic_locomo.json", root / "split.yaml")))
    ports = ArtifactPorts(root / "artifacts", bundle, load_settings())
    sample = service._sources.get_sample("synthetic-0")
    original = service._query_orchestrator._answer_vector_rag
    def switch(request, analysis, memories):
        answer = original(request, analysis, memories)
        report = _builder(service._sources, load_settings(), sample).build_memories_with_report(sample)
        ports.record_verified(ports.store(sample.sample_id).publish(report))
        return answer
    service._query_orchestrator._answer_vector_rag = switch
    response = service.run_query(_request(sample.sample_id), "vector_rag")
    assert response.answer_result.status == "error"
    assert response.answer_result.error.code == "ARTIFACT_STALE"
