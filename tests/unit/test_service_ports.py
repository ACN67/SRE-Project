"""Synthetic multi-sample service ports; no B disk attestation is claimed."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from dynamic_memory.contracts import ConversationSample, ConversationSession, SourceTurn, QueryRequest
from dynamic_memory.pipeline.runtime import create_runtime
from dynamic_memory.retrieval import (RetrievalServicePorts, IndexManager, save_index, analyze_query,
                                      VectorAnswerProvenance, QUERY_ANALYSIS_VERSION)
from dynamic_memory.retrieval.index import fingerprint
from test_retrieval_index import artifact, settings, assert_code
from test_vector_retrieval import QueryEncoder
from test_vector_answer import SyntheticQA


def sample(source):
    return ConversationSample(sample_id=source.manifest.sample_id, source_order_1_based=1,
                              sessions=[ConversationSession(session_id="s1", turns=[
                                  SourceTurn(sample_id=m.sample_id, session_id=r.session_id, dia_id=r.dia_id,
                                             speaker="Synthetic", text=m.content)
                                  for m in source.memories for r in m.source_refs])])


def setup_ports(tmp_path, diagnostics=None):
    sources = {name: artifact(sample=name) for name in ("synthetic-a", "synthetic-b")}
    paths = {name: tmp_path / f"{name}.json" for name in sources}
    encoder, qa = QueryEncoder(), SyntheticQA()
    manager = IndexManager(encoder)
    for name, source in sources.items():
        save_index(manager.build_index(source, settings()), paths[name])
    def provenance(source):
        return VectorAnswerProvenance("sha256:" + source.manifest.dataset_snapshot_sha256,
                                      QUERY_ANALYSIS_VERSION, "0" * 40, "development")
    ports = RetrievalServicePorts(encoder=encoder, settings=settings(), load_artifact=sources.__getitem__,
                                  index_path=paths.__getitem__, readiness_factory=SimpleNamespace,
                                  runtime=create_runtime(synthetic_adapter=qa), provenance_factory=provenance,
                                  on_diagnostic=diagnostics.append if diagnostics is not None else None,
                                  synthetic_test_mode=True)
    return ports, sources, paths, encoder, qa, manager


def prepare(ports, source):
    request = QueryRequest(query_id="same-query-id", sample_id=source.manifest.sample_id, question="What is Sam's job?")
    analysis = analyze_query(request)
    index = ports.load_verified_index(sample(source))
    found = ports.retrieve(request, analysis, index)
    memories = ports.load_candidates(found, index)
    return request, analysis, memories, found


def test_switch_samples_and_concurrent_queries_do_not_share_answers(tmp_path):
    diagnostics = []
    ports, sources, _, encoder, qa, _ = setup_ports(tmp_path, diagnostics)
    def run(name):
        request, analysis, memories, found = prepare(ports, sources[name])
        answer = ports.answer_vector_rag(request, analysis, memories)
        assert answer.status == "answered"
        assert answer.used_memory_ids == [item.memory_id for item in found.items]
        return set(answer.used_memory_ids)
    a, b = run("synthetic-a"), run("synthetic-b")
    assert not a.intersection(b)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ["synthetic-a", "synthetic-b"]))
    assert results == [a, b]
    assert len(encoder.questions) == 2  # Shared cache stays sample-local.
    assert len(qa.calls) == 2
    assert len(diagnostics) == 4
    by_sample = {name: [d for d in diagnostics if d.sample_id == name] for name in sources}
    assert all(len(records) == 2 and records[0] == records[1] for records in by_sample.values())
    assert len({d.query_fingerprint for d in diagnostics}) == 2
    assert all(d.query_id == "same-query-id" for d in diagnostics)
    for name, records in by_sample.items():
        assert set(records[0].included_memory_ids) == {m.memory_id for m in sources[name].memories}


@pytest.mark.parametrize("state", ["ready", "missing", "corrupt", "stale"])
def test_readiness_only_reads_and_checks_existing_index(tmp_path, state):
    ports, sources, paths, encoder, qa, _ = setup_ports(tmp_path)
    name = "synthetic-a"
    calls = len(encoder.calls)
    if state == "missing": paths[name].unlink()
    if state == "corrupt": paths[name].write_text("broken", encoding="utf-8")
    if state == "stale": sources[name] = artifact(sample=name, build="new-build")
    info = ports.index_readiness(name, sources[name].source_manifest_fingerprint)
    assert info.state == ("not_found" if state == "missing" else state)
    assert (info.index_fingerprint is not None) == (state == "ready")
    assert len(encoder.calls) == calls and encoder.questions == [] and qa.calls == []
    if state == "missing": assert not paths[name].exists()


def test_publish_change_between_retrieval_and_answer_is_rejected_then_new_query_works(tmp_path):
    diagnostics = []
    ports, sources, paths, encoder, qa, manager = setup_ports(tmp_path, diagnostics)
    name = "synthetic-a"
    request, analysis, memories, old = prepare(ports, sources[name])
    sources[name] = artifact(sample=name, build="published-v2")
    save_index(manager.build_index(sources[name], settings()), paths[name])
    assert_code("INDEX_STALE", lambda: ports.answer_vector_rag(request, analysis, memories))
    assert qa.calls == []
    request, analysis, memories, new = prepare(ports, sources[name])
    assert new.retriever_fingerprint != old.retriever_fingerprint
    assert ports.answer_vector_rag(request, analysis, memories).status == "answered"
    assert len(encoder.questions) == 2
    assert len(diagnostics) == 1
    assert diagnostics[0].retriever_fingerprint == new.retriever_fingerprint
    assert diagnostics[0].retriever_fingerprint != old.retriever_fingerprint


def test_wrong_sample_path_and_source_are_rejected(tmp_path):
    ports, sources, paths, _, qa, _ = setup_ports(tmp_path)
    paths["synthetic-a"] = paths["synthetic-b"]
    assert_code("SAMPLE_MISMATCH", lambda: ports.load_verified_index(sample(sources["synthetic-a"])))
    assert qa.calls == []


def test_no_pinned_query_or_interleaved_wrong_query_cannot_answer(tmp_path):
    ports, sources, _, _, qa, _ = setup_ports(tmp_path)
    request, analysis, memories, _ = prepare(ports, sources["synthetic-a"])
    prepare(ports, sources["synthetic-b"])
    assert_code("SAMPLE_MISMATCH", lambda: ports.answer_vector_rag(request, analysis, memories))
    assert_code("INDEX_STALE", lambda: ports.answer_vector_rag(request, analysis, memories))
    assert qa.calls == []


def test_source_manifest_mismatch_and_missing_source_refs(tmp_path):
    ports, sources, _, _, _, _ = setup_ports(tmp_path)
    source = sources["synthetic-a"]
    assert ports.index_readiness("synthetic-a", fingerprint("old manifest")).state == "stale"
    empty = sample(source)
    empty.sessions[0].turns = []
    assert_code("ARTIFACT_CORRUPT", lambda: ports.load_verified_index(empty))
