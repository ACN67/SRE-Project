"""Synthetic B disk -> C index/QA -> A service -> E contract checks.

All construction below is test-only composition, not a production A runtime.
Missing upstream modules are skipped until the team merges those branches.
"""
import json
from types import SimpleNamespace

import pytest

from dynamic_memory.web.bindings import WebBindings


@pytest.fixture
def bc_service(tmp_path):
    service_api = pytest.importorskip("dynamic_memory.services")
    store_api = pytest.importorskip("dynamic_memory.memory.artifact_store")
    retrieval = pytest.importorskip("dynamic_memory.retrieval.service_ports")
    from dynamic_memory.contracts import (
        BuildStatus, ConversationSample, ConversationSession, DatasetBundle,
        MemoryRecord, SourceRef, SourceTurn,
    )
    from dynamic_memory.contracts.memory_id import compute_memory_id
    from dynamic_memory.data.repository import SourceRepository
    from dynamic_memory.memory.build import MemoryBuildResult, WindowDiagnostic
    from dynamic_memory.memory.windows import plan_extraction_windows
    from dynamic_memory.pipeline.settings import load_settings
    from dynamic_memory.pipeline.runtime import create_runtime
    from dynamic_memory.pipeline.query_runner import QueryOrchestrator
    from dynamic_memory.retrieval import (
        EncoderIdentity, IndexManager, save_index, analyze_query,
        VectorAnswerProvenance, QUERY_ANALYSIS_VERSION,
    )
    from dynamic_memory.retrieval.index import fingerprint
    from dynamic_memory.llm.protocol import LLMResponse

    ref = SourceRef(session_id="s1", dia_id="d1")
    turn = SourceTurn(sample_id="synthetic-bc", **ref.model_dump(), speaker="Sam", text="Sam is a teacher.")
    sample = ConversationSample(sample_id=turn.sample_id, source_order_1_based=1,
        sessions=[ConversationSession(session_id="s1", turns=[turn])])
    bundle = DatasetBundle(snapshot_sha256="a" * 64, samples=[sample], split_by_sample_id={sample.sample_id: "main_dev"})
    config = load_settings()
    store = store_api.MemoryArtifactStore(tmp_path / "artifact", bundle, config.extraction,
        config.models, allow_mock_publication=True)
    draft = MemoryRecord(memory_id="mem_" + "0" * 20, sample_id=sample.sample_id,
        content=turn.text, entity="Sam", attribute="job", value="teacher", fact_kind="state",
        assertion_type="fact", polarity="positive", source_refs=[ref], extractor_version="b07-v1")
    memory = draft.model_copy(update={"memory_id": compute_memory_id(draft)})
    window = plan_extraction_windows(sample, config.extraction, reserved_prompt_tokens=1800)[0]
    diagnostic = WindowDiagnostic(window.extraction_window_id, window.source_refs,
        "success", None, None, 1, 1, 0, 1, "review")
    report = MemoryBuildResult(sample.sample_id, (memory,), "complete", False, 1, 1, (diagnostic,), "b07-v1")
    # Explicit offline setup; no Web browsing action calls publish/build_index.
    published = store.publish(report)

    class Encoder:
        identity = EncoderIdentity(model_name="synthetic-model", model_revision="synthetic-v1",
            model_fingerprint=fingerprint("synthetic-model"), encoding_fingerprint=fingerprint("synthetic-encoding"))
        document_calls = 0
        def embed_documents(self, texts):
            self.document_calls += 1
            return [[1.0, 0.0] for _ in texts]
        def embed_query(self, text):
            return [1.0, 0.0]

    class QA:
        calls = 0
        def generate(self, request):
            self.calls += 1
            return LLMResponse(output_mode="text", raw_output=json.dumps({
                "status": "answered", "answer": "Synthetic: Sam is a teacher.",
                "evidence_refs": [ref.model_dump()], "uncertainty": None}), finish_reason="stop")

    encoder, qa = Encoder(), QA()
    settings = config.retrieval.model_copy(update={"embedding_model": "synthetic-model"})
    index_path = tmp_path / "index.json"
    save_index(IndexManager(encoder).build_index(store.load(), settings), index_path)

    def load_artifact(sample_id):
        assert sample_id == sample.sample_id
        return store.load()  # B's actual strict loader on every access.

    ports = retrieval.RetrievalServicePorts(encoder=encoder, settings=settings,
        load_artifact=load_artifact, index_path=lambda _: index_path,
        readiness_factory=service_api.IndexReadiness,
        runtime=create_runtime(synthetic_adapter=qa), synthetic_test_mode=True,
        provenance_factory=lambda artifact: VectorAnswerProvenance(
            "sha256:" + artifact.manifest.dataset_snapshot_sha256, QUERY_ANALYSIS_VERSION, "0" * 40, "development"))

    def verified(sample_id):
        artifact = load_artifact(sample_id)
        return service_api.VerifiedMemoryLoad(artifact=artifact, sample_id=sample_id,
            dataset_snapshot_sha256=bundle.snapshot_sha256,
            source_manifest_fingerprint=artifact.source_manifest_fingerprint,
            provenance="synthetic_fixture", verification_id="synthetic-b-disk-roundtrip")

    service = service_api.DynamicMemoryService(sources=SourceRepository(bundle),
        build_status=lambda _: BuildStatus(dataset_state="ready", memory_state="complete",
            partial_build=False, build_id=published.manifest.build_id),
        load_verified_artifact=verified, index_readiness=ports.index_readiness,
        query_orchestrator=QueryOrchestrator(analyze_query=analyze_query,
            load_verified_index=ports.load_verified_index, retrieve=ports.retrieve,
            load_candidates=ports.load_candidates, answer_vector_rag=ports.answer_vector_rag))
    return SimpleNamespace(bindings=WebBindings(service), service=service, store=store,
        sample=sample, memory=memory, ref=ref, turn=turn, encoder=encoder, qa=qa, index_path=index_path)


def test_b_verified_disk_artifact_browses_through_a_and_e(bc_service):
    f = bc_service
    assert f.bindings.get_build_status(f.sample.sample_id).memory_state == "complete"
    assert f.bindings.list_memories(f.sample.sample_id) == [f.memory]
    assert f.bindings.get_memory(f.sample.sample_id, f.memory.memory_id) == f.memory
    assert f.bindings.get_turn(f.sample.sample_id, f.ref) == f.turn
    assert f.encoder.document_calls == 1 and f.qa.calls == 0


def test_c_vector_answer_through_a_and_e(bc_service):
    from dynamic_memory.contracts import QueryRequest
    f = bc_service
    response = f.bindings.run_query(QueryRequest(query_id="interactive_synthetic_bc",
        sample_id=f.sample.sample_id, question="What is Sam's job?"), "vector_rag")
    assert response.answer_result.status == "answered"
    assert response.answer_result.used_memory_ids == [f.memory.memory_id]
    assert response.answer_result.evidence_refs == [f.ref]
    assert response.resolution is None
    assert f.qa.calls == 1 and f.encoder.document_calls == 1


def test_c_missing_index_errors_without_rebuilding_or_losing_sources(bc_service):
    from dynamic_memory.contracts import QueryRequest
    f = bc_service
    f.index_path.unlink()  # Only the test's own file under pytest tmp_path.
    response = f.bindings.run_query(QueryRequest(query_id="interactive_missing_index",
        sample_id=f.sample.sample_id, question="What is Sam's job?"), "vector_rag")
    assert response.answer_result.status == "error"
    assert response.answer_result.error.code == "INDEX_NOT_FOUND"
    assert f.bindings.get_turn(f.sample.sample_id, f.ref) == f.turn
    assert not f.index_path.exists()
    assert f.qa.calls == 0 and f.encoder.document_calls == 1


def test_bc_service_in_web_does_not_regenerate_on_source_navigation(monkeypatch, bc_service):
    from test_empty_and_failure import launch
    f = bc_service
    app = launch(monkeypatch, f.service)
    assert not app.exception
    app.selectbox(key="query_method").select("vector_rag").run()
    app.text_area(key="query_question").set_value("What is Sam's job?")
    next(b for b in app.button if b.label == "提交问题").click().run()
    assert not app.exception
    assert any("Synthetic: Sam is a teacher." == text.value for text in app.text)
    next(b for b in app.button if b.label.startswith("答案证据 ↗")).click().run()
    assert not app.exception
    assert f.turn.text in [text.value for text in app.text]
    assert f.qa.calls == 1 and f.encoder.document_calls == 1
