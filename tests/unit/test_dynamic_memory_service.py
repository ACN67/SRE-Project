"""Synthetic read-only service ports and A-12 dispatch boundaries."""

from datetime import date, datetime, timezone
from hashlib import sha256

import pytest

from dynamic_memory.contracts import (AnswerResult, BuildStatus, DatasetBundle,
    MemoryArtifact, MemoryManifest, QueryAnalysis, QueryRequest,
    SourceRef, TimeIssue, memory_set_fingerprint)
from dynamic_memory.data.repository import SourceRepository
from dynamic_memory.contracts.memory_id import compute_memory_id
from dynamic_memory.pipeline.query_runner import QueryOrchestrator
from dynamic_memory.services import (DynamicMemoryService, IndexReadiness,
    ServiceError, VerifiedMemoryLoad)
from test_run_query_dispatch import SAMPLE, REF, memory, ports, source_repository


def fp(value):
    return "sha256:" + sha256(value.encode()).hexdigest()


def artifact(records=None, *, snapshot="a" * 64):
    records = (memory(),) if records is None else tuple(records)
    manifest = MemoryManifest(manifest_format_version=1, schema_version="2.1",
        build_id="synthetic-build", sample_id=SAMPLE, created_at=datetime(2026, 10, 27, tzinfo=timezone.utc),
        dataset_snapshot_sha256=snapshot, source_turns_fingerprint=fp("source"),
        extraction_config_fingerprint=fp("config"), extractor_version="synthetic-v1",
        prompt_fingerprint=fp("prompt"), backend="mock", resolved_model_id=None,
        build_status="complete", partial_build=False, total_windows=1 if records else 0,
        successful_windows=1 if records else 0, failed_windows=(), memory_count=len(records),
        memories_jsonl_sha256=sha256(b"synthetic-jsonl").hexdigest(),
        memory_set_fingerprint=memory_set_fingerprint(records))
    return MemoryArtifact(manifest=manifest, memories=records)


def complete_status():
    return BuildStatus(dataset_state="ready", memory_state="complete", partial_build=False,
                       build_id="synthetic-build")


def service(*, repo=None, status=None, loaded=None, index=None, query=None, dataset_state=None):
    repo = source_repository() if repo is None else repo
    item = artifact() if loaded is None else loaded
    return DynamicMemoryService(sources=repo,
        build_status=(lambda _sample: complete_status() if status is None else status),
        load_verified_artifact=lambda _sample: VerifiedMemoryLoad(item, SAMPLE,
            item.manifest.dataset_snapshot_sha256, item.source_manifest_fingerprint,
            "synthetic_fixture", "synthetic-loader-check"),
        index_readiness=lambda _sample, _fp: IndexReadiness(SAMPLE,
            repo.snapshot_sha256, item.source_manifest_fingerprint,
            "ready" if index is None else index, "synthetic-index"),
        query_orchestrator=QueryOrchestrator(**ports()) if query is None else query,
        dataset_state=dataset_state)


def assert_code(code, function, *args):
    with pytest.raises(ServiceError) as exc:
        function(*args)
    assert exc.value.code == code


def test_six_methods_complete_and_defensive_copies():
    api = service()
    assert api.list_samples() == [SAMPLE]
    assert api.get_build_status(SAMPLE) == complete_status()
    first = api.list_memories(SAMPLE)
    assert len(first) == 1 and first[0].memory_id == memory().memory_id
    first[0].content = "mutated by UI"
    assert api.list_memories(SAMPLE)[0].content != "mutated by UI"
    detail = api.get_memory(SAMPLE, memory().memory_id)
    detail.content = "mutated detail"
    assert api.get_memory(SAMPLE, memory().memory_id).content != "mutated detail"
    turn = api.get_turn(SAMPLE, REF)
    turn.text = "mutated turn"
    assert api.get_turn(SAMPLE, REF).text != "mutated turn"
    request = QueryRequest(query_id="synthetic_q1", sample_id=SAMPLE, question="Synthetic?")
    assert api.run_query(request, "full_text").method == "full_text"
    assert api.run_query(request, "vector_rag").answer_result.status == "answered"
    assert api.run_query(request, "dynamic").answer_result.status == "answered"
    assert_code("MEMORY_NOT_FOUND", api.get_memory, SAMPLE, "mem_" + "f" * 20)


def test_complete_zero_vs_not_built_and_all_failure_states():
    zero = service(loaded=artifact(()))
    assert zero.get_build_status(SAMPLE).memory_state == "complete"
    assert zero.list_memories(SAMPLE) == []
    assert_code("MEMORY_NOT_FOUND", zero.get_memory, SAMPLE, memory().memory_id)
    states = {"not_built": (False, None, "BUILD_NOT_FOUND"),
              "partial": (True, "b", "ARTIFACT_INCOMPLETE"),
              "failed": (True, "b", "MEMORY_BUILD_FAILED"),
              "stale": (False, "b", "ARTIFACT_STALE"),
              "corrupt": (False, "b", "ARTIFACT_CORRUPT")}
    for name, (partial, build_id, code) in states.items():
        status = BuildStatus(dataset_state="ready", memory_state=name,
                             partial_build=partial, build_id=build_id, error_code=code)
        api = service(status=status)
        assert api.get_build_status(SAMPLE) == status
        assert_code(code, api.list_memories, SAMPLE)
        assert_code(code, api.get_memory, SAMPLE, memory().memory_id)
        assert api.get_turn(SAMPLE, REF).dia_id == REF.dia_id


def test_unloaded_and_known_load_failure_never_become_empty_list():
    unloaded = SourceRepository()
    api = DynamicMemoryService(sources=unloaded)
    assert_code("DATA_NOT_LOADED", api.list_samples)
    assert_code("DATA_NOT_LOADED", api.get_turn, SAMPLE, REF)
    failed = DynamicMemoryService(sources=unloaded, dataset_state=lambda: "load_error")
    assert_code("DATA_LOAD_FAILED", failed.list_samples)
    assert_code("DATA_LOAD_FAILED", failed.get_turn, SAMPLE, REF)
    with_status = DynamicMemoryService(sources=unloaded,
        dataset_state=lambda: "load_error",
        build_status=lambda _sample: BuildStatus(dataset_state="load_error", memory_state="not_built",
            partial_build=False, error_code="DATA_LOAD_FAILED"))
    assert with_status.get_build_status(SAMPLE).dataset_state == "load_error"
    assert_code("DATA_LOAD_FAILED", with_status.list_memories, SAMPLE)
    assert_code("BUILD_STATUS_UNAVAILABLE", api.get_build_status, SAMPLE)


def test_loaded_truly_empty_dataset_returns_empty_list():
    empty = SourceRepository(DatasetBundle(snapshot_sha256="a" * 64, samples=[], split_by_sample_id={}))
    assert DynamicMemoryService(sources=empty).list_samples() == []


def test_source_identity_no_build_and_instance_isolation():
    api = service(status=BuildStatus(dataset_state="ready", memory_state="not_built",
                                     partial_build=False, error_code="BUILD_NOT_FOUND"))
    assert_code("SAMPLE_NOT_FOUND", api.get_turn, "other", REF)
    assert_code("SOURCE_NOT_FOUND", api.get_turn, SAMPLE, SourceRef(session_id="s1", dia_id="absent"))
    assert api.get_turn(SAMPLE, REF).sample_id == SAMPLE
    separate = DynamicMemoryService(sources=SourceRepository())
    assert_code("DATA_NOT_LOADED", separate.list_samples)
    assert api.list_samples() == [SAMPLE]


@pytest.mark.parametrize("change,code", [
    ("snapshot", "ARTIFACT_STALE"), ("wrong_sample", "ARTIFACT_CORRUPT"),
    ("fingerprint", "ARTIFACT_CORRUPT"), ("wrong_ref", "ARTIFACT_CORRUPT"),
    ("duplicate", "ARTIFACT_CORRUPT"),
])
def test_artifact_attestation_and_in_memory_validation_fail_closed(change, code):
    item = artifact()
    if change == "snapshot":
        item = artifact(snapshot="b" * 64)
    elif change == "wrong_sample":
        wrong = item.manifest.model_copy(update={"sample_id": "other"})
        item = item.model_copy(update={"manifest": wrong})
    elif change == "fingerprint":
        wrong = item.manifest.model_copy(update={"memory_set_fingerprint": fp("wrong")})
        item = item.model_copy(update={"manifest": wrong})
    elif change == "wrong_ref":
        draft = item.memories[0].model_copy(update={"source_refs": [SourceRef(session_id="s1", dia_id="absent")]})
        wrong = draft.model_copy(update={"memory_id": compute_memory_id(draft)})
        item = artifact((wrong,))
    elif change == "duplicate":
        item = item.model_copy(update={"memories": item.memories + item.memories})
    assert_code(code, service(loaded=item).list_memories, SAMPLE)


def test_missing_ports_and_wrong_status_provider_are_errors():
    repo = source_repository()
    api = DynamicMemoryService(sources=repo)
    assert_code("BUILD_STATUS_UNAVAILABLE", api.get_build_status, SAMPLE)
    assert_code("BUILD_STATUS_UNAVAILABLE", api.list_memories, SAMPLE)
    bad = DynamicMemoryService(sources=repo, build_status=lambda _sample: "complete")
    assert_code("BUILD_STATUS_INVALID", bad.get_build_status, SAMPLE)
    missing_loader = DynamicMemoryService(sources=repo, build_status=lambda _sample: complete_status())
    assert_code("ARTIFACT_LOADER_UNAVAILABLE", missing_loader.list_memories, SAMPLE)
    assert_code("DATASET_STATE_INVALID", DynamicMemoryService(
        sources=repo, dataset_state=lambda: "load_error").list_samples)


def test_query_error_vs_normal_no_evidence_and_full_text_without_build():
    request = QueryRequest(query_id="synthetic_q1", sample_id=SAMPLE, question="Synthetic?")
    unavailable = service(status=BuildStatus(dataset_state="ready", memory_state="not_built",
        partial_build=False, error_code="BUILD_NOT_FOUND"))
    assert unavailable.run_query(request, "full_text").answer_result.status == "answered"
    for method in ("vector_rag", "dynamic"):
        response = unavailable.run_query(request, method)
        assert response.answer_result.status == "error"
        assert response.answer_result.error.code == "BUILD_NOT_FOUND"
        assert response.retrieved is response.resolution is None
    for state, partial, build_id, code in (
        ("partial", True, "b", "ARTIFACT_INCOMPLETE"),
        ("failed", True, "b", "MEMORY_BUILD_FAILED"),
        ("stale", False, "b", "ARTIFACT_STALE"),
        ("corrupt", False, "b", "ARTIFACT_CORRUPT"),
    ):
        broken = service(status=BuildStatus(dataset_state="ready", memory_state=state,
            partial_build=partial, build_id=build_id, error_code=code))
        response = broken.run_query(request, "dynamic")
        assert response.answer_result.status == "error" and response.answer_result.error.code == code
    for state, code in (("stale", "INDEX_STALE"), ("not_found", "INDEX_NOT_FOUND")):
        response = service(index=state).run_query(request, "dynamic")
        assert response.answer_result.status == "error" and response.answer_result.error.code == code
    config = ports()
    config["retrieve"] = lambda req, _analysis, _index: __import__(
        "dynamic_memory.contracts", fromlist=["CandidateSet"]).CandidateSet(
            query_id=req.query_id, sample_id=req.sample_id, base_top_k=8,
            max_candidates=12, retriever_fingerprint="synthetic-index", items=[])
    config["load_candidates"] = lambda _candidates, _index: []
    config["resolve"] = lambda req, _analysis, _memories: __import__(
        "dynamic_memory.contracts", fromlist=["ResolutionResult"]).ResolutionResult(query_id=req.query_id)
    normal = service(query=QueryOrchestrator(**config))
    assert normal.run_query(request, "vector_rag").answer_result.status == "no_evidence"
    assert normal.run_query(request, "dynamic").answer_result.status == "no_evidence"


def test_unknown_query_port_failure_is_sanitized():
    request = QueryRequest(query_id="synthetic_q1", sample_id=SAMPLE, question="Synthetic?")
    api = service(query=QueryOrchestrator(analyze_query=lambda _request: (_ for _ in ()).throw(
        RuntimeError("SECRET_GOLD private")), get_sample=source_repository().get_sample))
    response = api.run_query(request, "full_text")
    assert response.answer_result.status == "error"
    assert "SECRET_GOLD" not in response.model_dump_json()


def test_browsing_never_calls_index_or_query_and_turn_needs_no_build():
    repo = source_repository()
    item = artifact()
    events = []
    api = DynamicMemoryService(sources=repo,
        build_status=lambda _sample: (events.append("status"), complete_status())[1],
        load_verified_artifact=lambda _sample: (events.append("load"), VerifiedMemoryLoad(
            item, SAMPLE, repo.snapshot_sha256, item.source_manifest_fingerprint,
            "synthetic_fixture", "synthetic-check"))[1],
        index_readiness=lambda *_: (_ for _ in ()).throw(AssertionError("index accessed")))
    assert api.list_samples() == [SAMPLE]
    assert api.get_turn(SAMPLE, REF).dia_id == REF.dia_id
    assert events == []
    assert api.list_memories(SAMPLE)[0].sample_id == SAMPLE
    assert events == ["status", "load"]


def test_service_binds_orchestrator_to_own_source_when_ids_match_across_snapshots():
    owned = source_repository()
    own_sample = owned.get_sample(SAMPLE)
    foreign_sample = own_sample.model_copy(deep=True)
    foreign_sample.sessions[0].turns[0].text = "FOREIGN_REPOSITORY_PAYLOAD"
    foreign = SourceRepository(DatasetBundle(snapshot_sha256="b" * 64,
        samples=[foreign_sample], split_by_sample_id={SAMPLE: "main_dev"}))
    foreign_reads = []

    def foreign_lookup(sample_id):
        foreign_reads.append(sample_id)
        return foreign.get_sample(sample_id)

    config = ports()
    config["get_sample"] = foreign_lookup
    config["answer_full_text"] = lambda _req, _analysis, sample: AnswerResult(
        status="answered", answer=sample.sessions[0].turns[0].text,
        context_source_refs=[REF], evidence_refs=[REF])
    supplied = QueryOrchestrator(**config)
    request = QueryRequest(query_id="synthetic_q1", sample_id=SAMPLE, question="Synthetic?")
    assert supplied.run_query(request, "full_text").answer_result.answer == "FOREIGN_REPOSITORY_PAYLOAD"
    assert foreign_reads == [SAMPLE]

    api = DynamicMemoryService(sources=owned, query_orchestrator=supplied)
    response = api.run_query(request, "full_text")
    assert response.answer_result.status == "answered"
    assert response.answer_result.answer == own_sample.sessions[0].turns[0].text
    assert foreign_reads == [SAMPLE]
    assert supplied.run_query(request, "full_text").answer_result.answer == "FOREIGN_REPOSITORY_PAYLOAD"
    assert foreign_reads == [SAMPLE, SAMPLE]


def test_early_time_conflict_precedes_build_and_index_for_all_three_methods():
    calls = []
    request = QueryRequest(query_id="synthetic_time", sample_id=SAMPLE,
                           question="Synthetic conflicting time?", as_of=date(2023, 5, 1))

    def conflicting_analysis(given):
        calls.append("analyze")
        return QueryAnalysis(
            query_id=given.query_id, sample_id=given.sample_id,
            query_intent="historical_state", time_mode="conflict",
            time_issue=TimeIssue(code="QUERY_TIME_CONFLICT", message="Synthetic conflict",
                                 explicit_as_of="2023-05-01", question_time="2024-01-01"))

    def forbidden_build(_sample):
        calls.append("build")
        raise AssertionError("build must not be checked for a time conflict")

    def forbidden_loader(_sample):
        calls.append("artifact")
        raise AssertionError("artifact must not be loaded for a time conflict")

    def forbidden_index(*_args):
        calls.append("index")
        raise AssertionError("index must not be checked for a time conflict")

    api = DynamicMemoryService(sources=source_repository(), build_status=forbidden_build,
        load_verified_artifact=forbidden_loader, index_readiness=forbidden_index,
        query_orchestrator=QueryOrchestrator(**ports(analyzer=conflicting_analysis)))
    for method in ("full_text", "vector_rag", "dynamic"):
        response = api.run_query(request, method)
        assert response.answer_result.status == "time_uncertain"
        assert response.answer_result.error is None
        assert response.retrieved is response.resolution is None
        assert response.query_analysis.time_issue.code == "QUERY_TIME_CONFLICT"
    assert calls == ["analyze"] * 3


def test_analyzer_failure_does_not_become_missing_memory_error():
    calls = []

    def failed_analysis(_request):
        calls.append("analyze")
        raise RuntimeError("SECRET_GOLD synthetic failure")

    def forbidden_build(_sample):
        calls.append("build")
        raise AssertionError("build should not run after analyzer failure")

    api = DynamicMemoryService(sources=source_repository(), build_status=forbidden_build,
        query_orchestrator=QueryOrchestrator(**ports(analyzer=failed_analysis)))
    request = QueryRequest(query_id="synthetic_q1", sample_id=SAMPLE, question="Synthetic?")
    result = api.run_query(request, "dynamic")
    assert result.answer_result.status == "error"
    assert result.answer_result.error.code == "ANALYZER_FAILURE"
    assert "SECRET_GOLD" not in result.model_dump_json()
    assert calls == ["analyze"]
