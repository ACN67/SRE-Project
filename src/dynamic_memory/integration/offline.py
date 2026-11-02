"""Explicit synthetic preparation and read-only B/C/A/E composition.

This module never selects a production provider or downloads a model.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from dynamic_memory.baselines.full_text import FullTextAnswerer, FullTextProvenance
from dynamic_memory.contracts import SourceRef
from dynamic_memory.data.loader import load_conversations
from dynamic_memory.data.repository import SourceRepository
from dynamic_memory.data.snapshot import freeze_split_config, inspect_snapshot
from dynamic_memory.llm import CallIdentity, GuardedLLMAdapter, LLMResponse, MockLLMAdapter, RecordedLLMExecutor, request_fingerprint
from dynamic_memory.memory.build import MemoryBuilder
from dynamic_memory.memory.extraction import MEMORY_DRAFT_PROMPT, build_extraction_request
from dynamic_memory.memory.normalization import EvidenceAssessment
from dynamic_memory.memory.windows import plan_extraction_windows
from dynamic_memory.pipeline.query_runner import QueryOrchestrator
from dynamic_memory.pipeline.runtime import create_runtime
from dynamic_memory.pipeline.settings import load_settings
from dynamic_memory.retrieval import EncoderIdentity, IndexManager, VectorAnswerProvenance, QUERY_ANALYSIS_VERSION, analyze_query, save_index
from dynamic_memory.retrieval.index import fingerprint
from dynamic_memory.retrieval.service_ports import RetrievalServicePorts
from dynamic_memory.services.dynamic_memory_service import DynamicMemoryService, IndexReadiness
from dynamic_memory.web.bindings import WebBindings

from .artifacts import ArtifactPorts


class OfflineConfigurationError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def state_root(value: str | Path | None = None) -> Path:
    raw = value if value is not None else os.environ.get("SRE_OFFLINE_STATE")
    if not raw:
        raise OfflineConfigurationError("OFFLINE_STATE_REQUIRED")
    if not Path(raw).is_absolute():
        raise OfflineConfigurationError("OFFLINE_STATE_INVALID")
    root = Path(raw).resolve()
    if root == Path.cwd().resolve() or root.is_relative_to(Path(__file__).resolve().parents[3]):
        raise OfflineConfigurationError("OFFLINE_STATE_INVALID")
    return root


def _paths(root: Path):
    return root / "synthetic_locomo.json", root / "split.yaml"


def _bundle(root: Path):
    source, split = _paths(root)
    if not source.is_file() or not split.is_file():
        raise OfflineConfigurationError("OFFLINE_SNAPSHOT_NOT_PREPARED")
    return load_conversations(str(source), str(split))


def prepare_source(root: Path) -> None:
    root = state_root(root)
    root.mkdir(parents=True, exist_ok=True)
    source, split = _paths(root)
    if source.exists() or split.exists():
        raise OfflineConfigurationError("OFFLINE_SOURCE_EXISTS")
    samples = []
    for i in range(10):
        samples.append({"sample_id": f"synthetic-{i}", "conversation": {
            "session_1": [{"dia_id": "d1", "speaker": "Ada", "text": f"Ada keeps a blue notebook in sample {i}."}],
            "session_2": [{"dia_id": "d1", "speaker": "Ada", "text": f"Ada plans a visit in sample {i}."}],
        }, "qa": [{"question": "What does Ada keep?",
                   "answer": "Synthetic notebook answer", "category": 1}]})
    source.write_text(json.dumps(samples, ensure_ascii=False), encoding="utf-8")
    split.write_text("snapshot_sha256: null\nsample_ids: null\ngroups:\n"
        "  main_dev: {source_order_1_based: [1, 2, 3, 4], sample_ids: null}\n"
        "  extra_dev: {source_order_1_based: [5, 6, 7, 8], sample_ids: null}\n"
        "  test: {source_order_1_based: [9, 10], sample_ids: null}\n", encoding="utf-8")
    freeze_split_config(inspect_snapshot(source), split, apply=True)


class FixedEncoder:
    """Deterministic offline test encoder; never a semantic model."""
    identity = EncoderIdentity(model_name="offline-fixed-v1", model_revision="1",
        model_fingerprint=fingerprint("offline-fixed-model-v1"),
        encoding_fingerprint=fingerprint("offline-fixed-encoding-v1"))

    def embed_documents(self, texts):
        return [[1.0, 0.0] for _ in texts]

    def embed_query(self, question):
        return [1.0, 0.0]


class SyntheticQA:
    """Only supplies a labeled test answer, with citations copied from context."""
    def generate(self, request):
        body = request.messages[-1].content
        if "Return one JSON object as text" in body:
            context = body.split("Context data:\n", 1)[1].split("\n\nQuestion data:", 1)[0]
            memories = json.loads(context)
            refs = memories[0]["source_refs"] if memories else []
            raw = json.dumps({"status": "answered" if refs else "no_evidence",
                "answer": "Synthetic offline answer." if refs else None,
                "evidence_refs": refs[:1], "uncertainty": None if refs else "No synthetic evidence."})
        else:
            raw = "Synthetic offline answer."
        return LLMResponse(output_mode="text", raw_output=raw, finish_reason="stop")


def _builder(repository, settings, sample):
    windows = plan_extraction_windows(sample, settings.extraction, reserved_prompt_tokens=1800)
    presets = {}
    for window in windows:
        turn = repository.get_turn(sample.sample_id, window.source_refs[0])
        fact = {"content": turn.text, "entity": "Ada", "attribute": "note",
            "value": turn.text, "fact_kind": "state", "assertion_type": "fact", "polarity": "positive",
            "recorded_at": None, "event_time": None,
            "source_refs": [window.source_refs[0].model_dump()]}
        request = build_extraction_request(window, settings.models)
        presets[request_fingerprint(request)] = LLMResponse(output_mode="json",
            raw_output=json.dumps({"facts": [fact]}))
    executor = RecordedLLMExecutor(GuardedLLMAdapter(
        MockLLMAdapter(presets, strict=True), settings.models, sleep=lambda _: None))
    def identity(window):
        digest = lambda value: "sha256:" + hashlib.sha256(value).hexdigest()
        return CallIdentity(sample_id=window.sample_id, task="memory_extraction",
            scope_id=window.extraction_window_id, method=None, run_kind="development",
            backend="mock", resolved_model_id=None,
            data_fingerprint=digest(repository.snapshot_sha256.encode()),
            template_id=MEMORY_DRAFT_PROMPT.template_id,
            template_version=MEMORY_DRAFT_PROMPT.version,
            template_fingerprint=MEMORY_DRAFT_PROMPT.template_fingerprint,
            schema_id="atomic_fact_draft", schema_version="1",
            config_fingerprint=digest(settings.extraction.model_dump_json().encode()),
            execution_policy_fingerprint=digest(settings.models.model_dump_json().encode()),
            retriever_fingerprint=None, query_analysis_version=None, git_sha="0" * 40)
    def checker(draft, turns):
        if len(turns) == 1 and draft.content == turns[0].text and draft.source_refs == [
            SourceRef(session_id=turns[0].session_id, dia_id=turns[0].dia_id)]:
            return EvidenceAssessment("supported", "EXACT_SYNTHETIC_SOURCE")
        return EvidenceAssessment("unclear", "SYNTHETIC_SOURCE_MISMATCH")
    return MemoryBuilder(repository, executor, settings.models, settings.extraction,
        identity, extractor_version="offline-synthetic-v1", evidence_checker=checker)


def prepare_artifacts(root: Path, sample_ids: tuple[str, ...] = ("synthetic-0", "synthetic-1")):
    root = state_root(root)
    bundle = _bundle(root)
    settings = load_settings()
    repository = SourceRepository(bundle)
    ports = ArtifactPorts(root / "artifacts", bundle, settings)
    result = {}
    for sample_id in sample_ids:
        sample = repository.get_sample(sample_id)
        store = ports.store(sample_id)
        if (store.root / "current.json").exists():
            raise OfflineConfigurationError("OFFLINE_ARTIFACT_EXISTS")
        report = _builder(repository, settings, sample).build_memories_with_report(sample)
        if report.build_status != "complete":
            ports.record_failed_attempt(sample_id, report.build_status)
            raise OfflineConfigurationError("OFFLINE_BUILD_FAILED")
        published = store.publish(report)
        loaded = ports.load(sample_id)
        if loaded.source_manifest_fingerprint != published.source_manifest_fingerprint:
            raise OfflineConfigurationError("OFFLINE_ARTIFACT_MISMATCH")
        ports.record_verified(loaded)
        result[sample_id] = loaded.manifest.build_id
    return result


def _index_settings(settings):
    return settings.retrieval.model_copy(update={"embedding_model": FixedEncoder.identity.model_name})


def index_path(root: Path, sample_id: str) -> Path:
    if not sample_id.startswith("synthetic-") or not sample_id[10:].isdigit():
        raise OfflineConfigurationError("SAMPLE_MISMATCH")
    return root / "indexes" / f"{sample_id}.json"


def prepare_indexes(root: Path, sample_ids: tuple[str, ...] = ("synthetic-0", "synthetic-1")):
    root = state_root(root)
    bundle = _bundle(root)
    settings = load_settings()
    artifacts = ArtifactPorts(root / "artifacts", bundle, settings)
    manager = IndexManager(FixedEncoder())
    result = {}
    for sample_id in sample_ids:
        path = index_path(root, sample_id)
        if path.exists():
            raise OfflineConfigurationError("OFFLINE_INDEX_EXISTS")
        index = manager.build_index(artifacts.load(sample_id), _index_settings(settings))
        path.parent.mkdir(parents=True, exist_ok=True)
        save_index(index, path)
        manager.load_index(path, artifacts.load(sample_id), _index_settings(settings))
        result[sample_id] = index.retriever_fingerprint
    return result


def create_system(root: Path, *, resolve=None, answer_dynamic=None) -> tuple[DynamicMemoryService, QueryOrchestrator]:
    root = state_root(root)
    bundle = _bundle(root)
    settings = load_settings()
    repository = SourceRepository(bundle)
    artifacts = ArtifactPorts(root / "artifacts", bundle, settings)
    runtime = create_runtime(settings=settings, synthetic_adapter=SyntheticQA())
    ports = RetrievalServicePorts(encoder=FixedEncoder(), settings=_index_settings(settings),
        load_artifact=artifacts.load, index_path=lambda sample: index_path(root, sample),
        readiness_factory=IndexReadiness, runtime=runtime, synthetic_test_mode=True,
        provenance_factory=lambda artifact: VectorAnswerProvenance(
            "sha256:" + artifact.manifest.dataset_snapshot_sha256,
            QUERY_ANALYSIS_VERSION, "0" * 40, "development"))
    full = FullTextAnswerer(runtime, FullTextProvenance(
        "sha256:" + bundle.snapshot_sha256, QUERY_ANALYSIS_VERSION,
        "0" * 40, "development"), synthetic_test_mode=True)
    runner = QueryOrchestrator(analyze_query=analyze_query,
        answer_full_text=full, load_verified_index=ports.load_verified_index,
        retrieve=ports.retrieve, retrieve_raw_topk=ports.retrieve_raw_topk,
        load_candidates=ports.load_candidates, answer_vector_rag=ports.answer_vector_rag,
        resolve=resolve, answer_dynamic=answer_dynamic)
    service = DynamicMemoryService(sources=repository, build_status=artifacts.build_status,
        load_verified_artifact=artifacts.verified, index_readiness=ports.index_readiness,
        query_orchestrator=runner)
    return service, runner.with_source_lookup(repository.get_sample)


def create_service(root: Path, *, resolve=None, answer_dynamic=None) -> DynamicMemoryService:
    return create_system(root, resolve=resolve, answer_dynamic=answer_dynamic)[0]


def create_web_bindings() -> WebBindings:
    if os.environ.get("SRE_INTEGRATION_PROFILE") != "offline_synthetic":
        raise OfflineConfigurationError("OFFLINE_PROFILE_REQUIRED")
    return WebBindings(service=create_service(state_root()),
                       label="Offline synthetic B/C/A/E", synthetic=True,
                       dynamic_available=False)
