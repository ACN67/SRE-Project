"""Synthetic A service/C ports/E binding check using reviewed local A Git source.

Runs in its own process, registering A's actual classes without editing checkout
files. No real B loader, D inference, model downloads or API calls are involved.
"""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types

from check_c_a_integration import load_a, SyntheticEncoder, SyntheticQA
import dynamic_memory.contracts as contracts
from dynamic_memory.contracts import ConversationSample, ConversationSession, DatasetBundle, MemoryArtifact, SourceTurn
from dynamic_memory.contracts.memory_id import compute_memory_id
from dynamic_memory.contracts.artifacts import memory_set_fingerprint
from dynamic_memory.contracts.models import QueryRequest
from dynamic_memory.pipeline.runtime import create_runtime
from dynamic_memory.pipeline.settings import load_settings
from dynamic_memory.retrieval import (IndexManager, RetrievalServicePorts, VectorAnswerProvenance,
                                      save_index, analyze_query, QUERY_ANALYSIS_VERSION)
from dynamic_memory.retrieval.index_smoke import synthetic_artifact
from dynamic_memory.web.bindings import WebBindings


def load_module(revision, name, path):
    root = Path(__file__).resolve().parents[1]
    source = subprocess.check_output(["git", "show", f"{revision}:{path}"], cwd=root).decode("utf-8")
    module = types.ModuleType(name)
    sys.modules[name] = module
    exec(compile(source, f"git:{revision}:{path}", "exec"), module.__dict__)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--a-ref", required=True)
    args = parser.parse_args()
    orchestrator, revision = load_a(args.a_ref)
    status_module = load_module(revision, "dynamic_memory.contracts.service_status",
                                "src/dynamic_memory/contracts/service_status.py")
    contracts.BuildStatus = status_module.BuildStatus
    # A's service requires exact class identity and its newer repository snapshot property.
    sys.modules["dynamic_memory.pipeline.query_runner"] = sys.modules[orchestrator.__module__]
    repo_module = load_module(revision, "dynamic_memory.data.repository", "src/dynamic_memory/data/repository.py")
    service_module = load_module(revision, "_synthetic_c_a_service", "src/dynamic_memory/services/dynamic_memory_service.py")

    first = synthetic_artifact()
    second_memories = []
    for memory in first.memories:
        changed = memory.model_copy(update={"sample_id": "synthetic_second_sample"})
        second_memories.append(changed.model_copy(update={"memory_id": compute_memory_id(changed)}))
    second = MemoryArtifact(manifest=first.manifest.model_copy(update={
        "sample_id": "synthetic_second_sample", "memory_set_fingerprint": memory_set_fingerprint(second_memories)}),
        memories=tuple(second_memories))
    sources = {a.manifest.sample_id: a for a in (first, second)}
    samples = []
    for order, artifact in enumerate(sources.values(), 1):
        turns = [SourceTurn(sample_id=m.sample_id, session_id=r.session_id, dia_id=r.dia_id,
                            speaker="Synthetic Sam", text=m.content) for m in artifact.memories for r in m.source_refs]
        samples.append(ConversationSample(sample_id=artifact.manifest.sample_id, source_order_1_based=order,
                       sessions=[ConversationSession(session_id="synthetic_session", turns=turns)]))
    repository = repo_module.SourceRepository(DatasetBundle(
        snapshot_sha256=first.manifest.dataset_snapshot_sha256, samples=samples,
        split_by_sample_id={sample.sample_id: "main_dev" for sample in samples}))

    def build_status(name):
        return status_module.BuildStatus(dataset_state="ready", memory_state="complete", partial_build=False,
                                         build_id=sources[name].manifest.build_id)

    def verified_fixture(name):
        a = sources[name]
        return service_module.VerifiedMemoryLoad(
            artifact=a, sample_id=name, dataset_snapshot_sha256=a.manifest.dataset_snapshot_sha256,
            source_manifest_fingerprint=a.source_manifest_fingerprint,
            provenance="synthetic_fixture", verification_id="synthetic-probe-only")

    encoder = SyntheticEncoder()
    settings = load_settings().retrieval.model_copy(update={"embedding_model": encoder.identity.model_name})
    runtime = create_runtime(synthetic_adapter=SyntheticQA())
    def provenance(a):
        return VectorAnswerProvenance("sha256:" + a.manifest.dataset_snapshot_sha256,
                                      QUERY_ANALYSIS_VERSION, revision, "development")

    with tempfile.TemporaryDirectory(prefix="acn-c-service-") as folder:
        paths = {name: Path(folder) / f"sample-{n}.json" for n, name in enumerate(sources)}
        manager = IndexManager(encoder)
        for name, artifact in sources.items():
            save_index(manager.build_index(artifact, settings), paths[name])
        ports = RetrievalServicePorts(encoder=encoder, settings=settings, load_artifact=sources.__getitem__,
                                      index_path=paths.__getitem__, readiness_factory=service_module.IndexReadiness,
                                      runtime=runtime, provenance_factory=provenance, synthetic_test_mode=True)
        runner = orchestrator(analyze_query=analyze_query, load_verified_index=ports.load_verified_index,
                              retrieve=ports.retrieve, load_candidates=ports.load_candidates,
                              answer_vector_rag=ports.answer_vector_rag)
        service = service_module.DynamicMemoryService(sources=repository, build_status=build_status,
                    load_verified_artifact=verified_fixture, query_orchestrator=runner,
                    index_readiness=ports.index_readiness)
        web = WebBindings(service, synthetic=True)
        assert web.list_samples() == list(sources)
        for name in sources:
            memories = web.list_memories(name)
            assert memories
            assert web.get_memory(name, memories[0].memory_id) == memories[0]
            assert web.get_turn(name, memories[0].source_refs[0]).sample_id == name
            assert service.get_build_status(name).memory_state == "complete"
        assert encoder.query_calls == 0  # Browsing does not retrieve or generate.

        responses = []
        for name in sources:
            request = QueryRequest(query_id="same-query", sample_id=name, question="What is Sam's job?")
            response = service.run_query(request, "vector_rag")
            assert response.answer_result.status == "answered", response.model_dump_json()
            responses.append(response)
        assert not set(responses[0].answer_result.used_memory_ids) & set(responses[1].answer_result.used_memory_ids)
        name = first.manifest.sample_id
        old_fp = responses[0].retrieved.retriever_fingerprint
        sources[name] = first.model_copy(update={"manifest": first.manifest.model_copy(update={"build_id": "synthetic-v2"})})
        request = QueryRequest(query_id="same-query", sample_id=name, question="What is Sam's job?")
        assert service.run_query(request, "vector_rag").answer_result.error.code == "INDEX_STALE"
        save_index(manager.build_index(sources[name], settings), paths[name])
        updated = service.run_query(request, "vector_rag")
        assert updated.answer_result.status == "answered"
        assert updated.retrieved.retriever_fingerprint != old_fp
        paths[name].unlink()
        assert service.run_query(request, "vector_rag").answer_result.error.code == "INDEX_NOT_FOUND"
        assert not paths[name].exists()  # No implicit index rebuild.
        assert web.list_memories(name)  # Source browsing is independent of index availability.
        print(json.dumps({"run_kind": "synthetic_A_service_C_ports_E_readonly", "a_revision": revision,
                          "samples_answered": 2, "sample_isolation": True, "read_only_no_query_encoding": True,
                          "published_index_update": True, "missing_index_no_rebuild": True,
                          "real_B_loader": False, "real_model": False}, indent=2))


if __name__ == "__main__":
    main()
