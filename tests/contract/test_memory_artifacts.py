"""G-02: B->C typed manifest/MemoryArtifact identity, offline-only."""
from datetime import datetime, timezone
from hashlib import sha256

import pytest
from pydantic import ValidationError

from dynamic_memory.contracts import (
    FailedWindow, MemoryArtifact, MemoryManifest, MemoryRecord,
    SourceRef, memory_set_fingerprint,
)
from dynamic_memory.contracts.memory_id import compute_memory_id


def fp(value):
    return "sha256:" + sha256(value.encode("utf-8")).hexdigest()


def sha(value):
    return sha256(value.encode("utf-8")).hexdigest()


def memory(sample="synthetic_001"):
    fields = {
        "memory_id": "mem_00000000000000000000",
        "sample_id": sample,
        "content": "Synthetic memory for contract testing.",
        "fact_kind": "event",
        "assertion_type": "fact",
        "polarity": "positive",
        "source_refs": [SourceRef(session_id="s1", dia_id="d1")],
        "extractor_version": "synthetic-v1",
    }
    fields["memory_id"] = compute_memory_id(MemoryRecord(**fields))
    return MemoryRecord(**fields)


def manifest(memories, *, status="complete", failures=(), successful_windows=1, **overrides):
    data = {
        "manifest_format_version": 1,
        "schema_version": "2.1",
        "build_id": "build_test_001",
        "sample_id": "synthetic_001",
        "created_at": datetime(2026, 10, 19, tzinfo=timezone.utc),
        "dataset_snapshot_sha256": sha("synthetic-data"),
        "source_turns_fingerprint": fp("synthetic-source-turns"),
        "extraction_config_fingerprint": fp("synthetic-config"),
        "extractor_version": "synthetic-v1",
        "prompt_fingerprint": fp("synthetic-template"),
        "backend": "mock",
        "resolved_model_id": None,
        "build_status": status,
        "partial_build": bool(failures),
        "total_windows": successful_windows + len(failures),
        "successful_windows": successful_windows,
        "failed_windows": list(failures),
        "memory_count": len(memories),
        "memories_jsonl_sha256": sha("synthetic-jsonl"),
        "memory_set_fingerprint": memory_set_fingerprint(memories),
    }
    data.update(overrides)
    return MemoryManifest(**data)


def test_artifact_binding_and_manifest_fingerprint_is_deterministic():
    item = memory()
    m = manifest([item])
    artifact = MemoryArtifact(manifest=m, memories=(item,))
    assert artifact.source_manifest_fingerprint == m.source_manifest_fingerprint
    assert artifact.source_manifest_fingerprint.startswith("sha256:")
    assert len(artifact.source_manifest_fingerprint) == 71
    assert MemoryArtifact.model_validate(artifact.model_dump()).source_manifest_fingerprint == artifact.source_manifest_fingerprint
    assert artifact.manifest.schema_version == "2.1"
    artifact.require_complete_for_index()
    changed = m.model_copy(update={"build_id": "build_test_002"})
    assert changed.source_manifest_fingerprint != m.source_manifest_fingerprint
    m2 = m.model_copy(update={"prompt_fingerprint": fp("new-template")})
    assert m2.source_manifest_fingerprint != m.source_manifest_fingerprint


@pytest.mark.parametrize("change", [
    {"schema_version": "2.2"},
    {"manifest_format_version": 2},
    {"dataset_snapshot_sha256": "abcd"},
    {"prompt_fingerprint": sha("missing-prefix")},
    {"backend": "deepseek"},
    {"resolved_model_id": "invalid-mock-model"},
    {"total_windows": 99},
    {"partial_build": True},
    {"build_status": "partial"},
    {"memory_count": -1},
    {"memories_jsonl_sha256": "invalid"},
    {"failed_windows": [FailedWindow(extraction_window_id="w1", error_code="TIMEOUT")]},
])
def test_manifest_rejects_wrong_metadata_and_state(change):
    m = manifest([memory()])
    with pytest.raises(ValidationError):
        MemoryManifest.model_validate({**m.model_dump(), **change})


def test_nonempty_partial_and_failed_artifacts_never_formal_index():
    failed = FailedWindow(extraction_window_id="window_002", error_code="MODEL_TIMEOUT")
    record = memory()
    part = MemoryArtifact(
        manifest=manifest([record], status="partial", failures=[failed]), memories=(record,)
    )
    assert part.manifest.partial_build
    with pytest.raises(ValueError, match="ARTIFACT_INCOMPLETE"):
        part.require_complete_for_index()

    empty = ()
    failed_m = manifest(empty, status="failed", failures=[failed], successful_windows=0)
    failed_artifact = MemoryArtifact(manifest=failed_m, memories=empty)
    with pytest.raises(ValueError, match="ARTIFACT_INCOMPLETE"):
        failed_artifact.require_complete_for_index()


def test_complete_zero_memories_and_zero_windows_are_legitimate():
    m = manifest((), successful_windows=0)
    empty = MemoryArtifact(manifest=m, memories=())
    empty.require_complete_for_index()
    assert m.memory_count == m.total_windows == 0
    assert not m.partial_build


@pytest.mark.parametrize("mutation", ["wrong_sample", "count", "fingerprint", "bad_id", "duplicate"])
def test_artifact_rejects_unbound_or_invalid_memories(mutation):
    item = memory()
    m = manifest([item])
    if mutation == "wrong_sample":
        records = (memory("another_sample"),)
    elif mutation == "count":
        records = ()
    elif mutation == "fingerprint":
        m = m.model_copy(update={"memory_set_fingerprint": fp("wrong")})
        records = (item,)
    elif mutation == "bad_id":
        records = (item.model_copy(update={"content": "Not the original."}),)
    else:
        m = manifest([item]).model_copy(update={"memory_count": 2})
        records = (item, item)
    with pytest.raises((ValidationError, ValueError)):
        MemoryArtifact(manifest=m, memories=records)


def test_memory_set_fingerprint_order_independence_and_uniqueness():
    first = memory("synthetic_001")
    fields = first.model_dump()
    fields["content"] = "Other independent fact"
    item = MemoryRecord(**fields)
    second = item.model_copy(update={"memory_id": compute_memory_id(item)})
    assert memory_set_fingerprint((first, second)) == memory_set_fingerprint((second, first))
    with pytest.raises(ValueError, match="duplicate"):
        memory_set_fingerprint((first, first))


def test_manifest_requires_real_model_only_for_deepseek():
    m = manifest([memory()])
    real = MemoryManifest.model_validate({
        **m.model_dump(), "backend": "deepseek", "resolved_model_id": "synthetic-test-model",
    })
    assert real.resolved_model_id == "synthetic-test-model"
    with pytest.raises(ValidationError):
        MemoryManifest.model_validate({**m.model_dump(), "backend": "deepseek", "resolved_model_id": None})
