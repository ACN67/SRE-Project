"""LoCoMo-shaped synthetic snapshot through real B-01..B-08 code and Mock dependencies."""

from __future__ import annotations

import hashlib
import json

import pytest

from dynamic_memory.contracts import DatasetBundle, MemoryArtifact, SourceRef
from dynamic_memory.contracts.memory_id import validate_memory_id
from dynamic_memory.data.loader import load_conversations
from dynamic_memory.data.repository import SourceRepository
from dynamic_memory.data.snapshot import freeze_split_config, inspect_snapshot
from dynamic_memory.llm import (
    CallIdentity, GuardedLLMAdapter, LLMResponse, MockLLMAdapter,
    RecordedLLMExecutor, request_fingerprint,
)
from dynamic_memory.memory.artifact_store import ArtifactStoreError, MemoryArtifactStore
from dynamic_memory.memory.build import MemoryBuildError, MemoryBuilder
from dynamic_memory.memory.extraction import MEMORY_DRAFT_PROMPT, build_extraction_request
from dynamic_memory.memory.normalization import EvidenceAssessment
from dynamic_memory.memory.windows import plan_extraction_windows
from dynamic_memory.pipeline.settings import load_settings


GOLD_MARKER = "GOLD_SENTINEL_NEVER_USE"


def _sha(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _snapshot(tmp_path):
    items = []
    for index in range(10):
        sessions = {
            "session_1": [{"dia_id": "d1", "speaker": "Ada", "text": "Ada lives in Paris.",
                           "blip_caption": "Ada beside a map." if index == 0 else None}],
        }
        if index == 0:
            sessions["session_2"] = [{"dia_id": "d1", "speaker": "Ada", "text": "Ada plans to visit Lyon."}]
        items.append({"sample_id": f"synthetic-{index}", "conversation": sessions,
                      "qa": {"answer": GOLD_MARKER, "evidence": GOLD_MARKER},
                      "observation": GOLD_MARKER, "session_summary": GOLD_MARKER})
    source = tmp_path / "synthetic_locomo_shape.json"
    source.write_bytes(json.dumps(items, ensure_ascii=False).encode("utf-8"))
    split = tmp_path / "data_split.yaml"
    split.write_text(
        "snapshot_sha256: null\nsample_ids: null\ngroups:\n"
        "  main_dev: {source_order_1_based: [1, 2, 3, 4], sample_ids: null}\n"
        "  extra_dev: {source_order_1_based: [5, 6, 7, 8], sample_ids: null}\n"
        "  test: {source_order_1_based: [9, 10], sample_ids: null}\n", encoding="utf-8",
    )
    freeze_split_config(inspect_snapshot(source), split, apply=True)
    bundle = load_conversations(str(source), str(split))
    return bundle, bundle.samples[0]


def _runtime(bundle, sample, *, rejected_session=None, checker=None):
    settings = load_settings()
    repository = SourceRepository(bundle)
    windows = plan_extraction_windows(sample, settings.extraction, reserved_prompt_tokens=1800)
    presets = {}
    for window in windows:
        source = repository.get_turn(sample.sample_id, window.source_refs[0])
        fact = {
            "content": source.text, "entity": "Ada", "attribute": "location" if window.session_id == "session_1" else "travel_plan",
            "value": "Paris" if window.session_id == "session_1" else "Lyon",
            "fact_kind": "state", "assertion_type": "fact" if window.session_id == "session_1" else "plan",
            "polarity": "positive", "recorded_at": None, "event_time": None,
            "source_refs": [window.source_refs[0].model_dump()],
        }
        raw = "{}" if window.session_id == rejected_session else json.dumps({"facts": [fact]})
        request = build_extraction_request(window, settings.models)
        assert GOLD_MARKER not in request.model_dump_json()
        presets[request_fingerprint(request)] = LLMResponse(output_mode="json", raw_output=raw)
    executor = RecordedLLMExecutor(GuardedLLMAdapter(MockLLMAdapter(presets, strict=True), settings.models,
                                                      sleep=lambda _: None))
    data_fp = _sha(bundle.snapshot_sha256.encode())
    config_fp = _sha(settings.extraction.model_dump_json().encode())
    policy_fp = _sha(settings.models.model_dump_json().encode())

    def identity(window):
        return CallIdentity(
            sample_id=window.sample_id, task="memory_extraction", scope_id=window.extraction_window_id,
            method=None, run_kind="development", backend="mock", resolved_model_id=None,
            data_fingerprint=data_fp, template_id=MEMORY_DRAFT_PROMPT.template_id,
            template_version=MEMORY_DRAFT_PROMPT.version,
            template_fingerprint=MEMORY_DRAFT_PROMPT.template_fingerprint,
            schema_id="atomic_fact_draft", schema_version="1", config_fingerprint=config_fp,
            execution_policy_fingerprint=policy_fp, retriever_fingerprint=None,
            query_analysis_version=None, git_sha="0" * 40,
        )

    def exact_source_checker(draft, turns):
        if len(turns) == 1 and draft.content == turns[0].text and draft.source_refs == [
            SourceRef(session_id=turns[0].session_id, dia_id=turns[0].dia_id)
        ]:
            return EvidenceAssessment("supported", "EXACT_SYNTHETIC_SOURCE")
        return EvidenceAssessment("unclear", "SYNTHETIC_SOURCE_MISMATCH")

    builder = MemoryBuilder(repository, executor, settings.models, settings.extraction, identity,
                            extractor_version="b09-synthetic-v1",
                            evidence_checker=exact_source_checker if checker is None else checker)
    return builder, windows, settings


def test_snapshot_to_mock_artifact_and_b_side_consumer(tmp_path):
    bundle, sample = _snapshot(tmp_path)
    assert bundle.split_by_sample_id[sample.sample_id] == "main_dev"
    builder, windows, settings = _runtime(bundle, sample)
    report = builder.build_memories_with_report(sample)
    assert report.build_status == "complete" and report.total_windows == len(windows) == 2
    assert len(report.memories) == 2
    assert {ref.session_id for memory in report.memories for ref in memory.source_refs} == {"session_1", "session_2"}
    for memory in report.memories:
        validate_memory_id(memory)
        assert all(builder.repository.get_turn(sample.sample_id, ref).text == memory.content for ref in memory.source_refs)
    store = MemoryArtifactStore(tmp_path / "artifacts", bundle, settings.extraction, settings.models,
                                allow_mock_publication=True)
    published = store.publish(report)
    loaded = store.load()
    assert isinstance(loaded, MemoryArtifact)
    loaded.require_complete_for_index()  # B-side contract only; C currently has no callable index entry.
    assert loaded.memories == report.memories
    assert loaded.manifest.dataset_snapshot_sha256 == bundle.snapshot_sha256
    assert len(store.import_memories()) == 2
    assert GOLD_MARKER not in (tmp_path / "artifacts" / "current.json").read_text(encoding="utf-8")
    assert GOLD_MARKER not in loaded.model_dump_json()
    again = builder.build_memories_with_report(sample)
    assert [m.memory_id for m in again.memories] == [m.memory_id for m in report.memories]
    assert store.publish(again).manifest.memory_set_fingerprint == published.manifest.memory_set_fingerprint


def test_partial_failure_does_not_replace_previous_artifact(tmp_path):
    bundle, sample = _snapshot(tmp_path)
    builder, _, settings = _runtime(bundle, sample)
    store = MemoryArtifactStore(tmp_path / "artifacts", bundle, settings.extraction, settings.models,
                                allow_mock_publication=True)
    previous = store.publish(builder.build_memories_with_report(sample))
    partial_builder, _, _ = _runtime(bundle, sample, rejected_session="session_2")
    partial = partial_builder.build_memories_with_report(sample)
    assert partial.build_status == "partial" and partial.partial_build
    assert partial.successful_windows == 1 and partial.total_windows == 2
    assert partial.window_diagnostics[1].error_code is not None
    with pytest.raises(MemoryBuildError, match="MEMORY_BUILD_INCOMPLETE"):
        partial_builder.build_memories(sample)
    with pytest.raises(ArtifactStoreError, match="ARTIFACT_INCOMPLETE"):
        store.publish(partial)
    assert store.load().source_manifest_fingerprint == previous.source_manifest_fingerprint


def test_missing_evidence_reviewer_is_failed_not_empty_success(tmp_path):
    bundle, sample = _snapshot(tmp_path)
    builder, _, settings = _runtime(bundle, sample)
    builder.evidence_checker = None
    report = builder.build_memories_with_report(sample)
    assert report.build_status == "failed" and report.partial_build
    assert not report.memories
    assert all(item.error_code == "EVIDENCE_UNCLEAR" and item.reason_code == "NO_CHECKER"
               for item in report.window_diagnostics)
    with pytest.raises(MemoryBuildError, match="MEMORY_BUILD_FAILED"):
        builder.build_memories(sample)
    store = MemoryArtifactStore(tmp_path / "artifacts", bundle, settings.extraction, settings.models,
                                allow_mock_publication=True)
    with pytest.raises(ArtifactStoreError, match="MEMORY_BUILD_FAILED"):
        store.publish(report)
    assert not (tmp_path / "artifacts" / "current.json").exists()

