"""B-08 offline publication and strict reload at the artifact store seam."""

import json
from hashlib import sha256

import pytest

from dynamic_memory.contracts import ConversationSample, ConversationSession, DatasetBundle, MemoryRecord, SourceRef, SourceTurn
from dynamic_memory.contracts.memory_id import compute_memory_id
from dynamic_memory.memory.artifact_store import ArtifactStoreError, MemoryArtifactStore
from dynamic_memory.memory.build import MemoryBuildResult, WindowDiagnostic
from dynamic_memory.memory.windows import plan_extraction_windows
from dynamic_memory.pipeline.settings import load_settings
from test_memory_build import _approved, _setup


def _context(tmp_path):
    sample = ConversationSample(sample_id="sample_1", source_order_1_based=1, sessions=[
        ConversationSession(session_id="session_1", turns=[SourceTurn(
            sample_id="sample_1", session_id="session_1", dia_id="d1", speaker="Ada",
            text="Ada moved to Paris.",
        )]),
    ])
    bundle = DatasetBundle(snapshot_sha256="a" * 64, samples=[sample],
                           split_by_sample_id={"sample_1": "main_dev"})
    settings = load_settings()
    store = MemoryArtifactStore(tmp_path, bundle, settings.extraction, settings.models,
                                allow_mock_publication=True)
    fields = dict(memory_id="mem_" + "0" * 20, sample_id="sample_1", content="Ada moved to Paris.",
                  entity="Ada", attribute="residence", value="Paris", fact_kind="event",
                  assertion_type="fact", polarity="positive", source_refs=[SourceRef(session_id="session_1", dia_id="d1")],
                  extractor_version="b07-v1")
    fields["memory_id"] = compute_memory_id(MemoryRecord(**fields))
    memory = MemoryRecord(**fields)
    window = plan_extraction_windows(sample, settings.extraction, reserved_prompt_tokens=1800)[0]
    diagnostic = WindowDiagnostic(window.extraction_window_id, window.source_refs,
                                  "success", None, None, 1, 1, 0, 1, "review")
    report = MemoryBuildResult("sample_1", (memory,), "complete", False, 1, 1, (diagnostic,), "b07-v1")
    return store, report


def test_publish_roundtrip_and_stable_bytes(tmp_path):
    store, report = _context(tmp_path)
    first = store.publish(report)
    loaded = store.load()
    assert loaded.memories == report.memories
    assert loaded.manifest.schema_version == "2.1"
    assert loaded.manifest.dataset_snapshot_sha256 == "a" * 64
    assert loaded.manifest.memory_count == 1
    assert store.import_memories() == list(report.memories)
    loaded.require_complete_for_index()
    assert first.manifest.memories_jsonl_sha256 == loaded.manifest.memories_jsonl_sha256
    assert store.publish(report).manifest.memories_jsonl_sha256 == first.manifest.memories_jsonl_sha256


def test_partial_and_failed_cannot_replace_complete(tmp_path):
    store, report = _context(tmp_path)
    previous = store.publish(report)
    failed = WindowDiagnostic("window_2", (), "failed", "MODEL_TIMEOUT", None, 0, 0, 0, 0, "extraction")
    partial = MemoryBuildResult("sample_1", report.memories, "partial", True, 2, 1,
                                report.window_diagnostics + (failed,), "b07-v1")
    with pytest.raises(ArtifactStoreError, match="ARTIFACT_INCOMPLETE"):
        store.publish(partial)
    all_failed = MemoryBuildResult("sample_1", (), "failed", True, 1, 0, (failed,), "b07-v1")
    with pytest.raises(ArtifactStoreError, match="MEMORY_BUILD_FAILED"):
        store.publish(all_failed)
    assert store.load().source_manifest_fingerprint == previous.source_manifest_fingerprint


def test_empty_complete_build_is_publishable(tmp_path):
    settings = load_settings()
    sample = ConversationSample(sample_id="sample_1", source_order_1_based=1, sessions=[])
    bundle = DatasetBundle(snapshot_sha256="a" * 64, samples=[sample],
                           split_by_sample_id={"sample_1": "main_dev"})
    store = MemoryArtifactStore(tmp_path, bundle, settings.extraction, settings.models,
                                allow_mock_publication=True)
    empty = MemoryBuildResult("sample_1", (), "complete", False, 0, 0, (), "b07-v1")
    artifact = store.publish(empty)
    assert artifact.memories == ()
    assert store.import_memories() == []


def _current_dir(tmp_path):
    pointer = json.loads((tmp_path / "current.json").read_text(encoding="utf-8"))
    return tmp_path / "builds" / pointer["build_id"]


@pytest.mark.parametrize("name,replacement", [
    ("memories.jsonl", b"not json\n"),
    ("memories.json", b"{broken"),
    ("manifest.json", b"{}"),
])
def test_tampering_is_rejected(name, replacement, tmp_path):
    store, report = _context(tmp_path)
    store.publish(report)
    (_current_dir(tmp_path) / name).write_bytes(replacement)
    with pytest.raises(ArtifactStoreError, match="ARTIFACT_CORRUPT"):
        store.load()


def test_pointer_corruption_or_missing_target_fails_closed(tmp_path):
    store, report = _context(tmp_path)
    store.publish(report)
    pointer = tmp_path / "current.json"
    pointer.write_text('{"build_id":"../../escape","manifest_sha256":"bad"}', encoding="utf-8")
    with pytest.raises(ArtifactStoreError, match="ARTIFACT_CORRUPT"):
        store.load()
    pointer.write_text('{"build_id":"build_00000000000000000000000000000000",'
                       '"manifest_sha256":"' + '0' * 64 + '"}', encoding="utf-8")
    with pytest.raises(ArtifactStoreError, match="ARTIFACT_CORRUPT"):
        store.load()


def test_staging_write_failure_preserves_previous_publication(tmp_path, monkeypatch):
    import dynamic_memory.memory.artifact_store as module

    store, report = _context(tmp_path)
    previous = store.publish(report)
    original_write = module._write
    calls = 0

    def failing_write(path, payload):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic secret detail")
        original_write(path, payload)

    monkeypatch.setattr(module, "_write", failing_write)
    with pytest.raises(ArtifactStoreError, match="ARTIFACT_WRITE_FAILED") as caught:
        store.publish(report)
    assert "secret detail" not in str(caught.value)
    assert store.load().source_manifest_fingerprint == previous.source_manifest_fingerprint


def test_pointer_switch_failure_preserves_previous_publication(tmp_path, monkeypatch):
    import dynamic_memory.memory.artifact_store as module

    store, report = _context(tmp_path)
    previous = store.publish(report)
    original_replace = module.os.replace

    def failing_replace(source, target):
        if str(target).endswith("current.json"):
            raise OSError("synthetic pointer failure")
        return original_replace(source, target)

    monkeypatch.setattr(module.os, "replace", failing_replace)
    with pytest.raises(ArtifactStoreError, match="ARTIFACT_WRITE_FAILED"):
        store.publish(report)
    assert store.load().source_manifest_fingerprint == previous.source_manifest_fingerprint


def test_mock_b07_report_can_publish_and_roundtrip(tmp_path):
    sample, builder, _ = _setup(checker=_approved)
    report = builder.build_memories_with_report(sample)
    bundle = DatasetBundle(snapshot_sha256="a" * 64, samples=[sample],
                           split_by_sample_id={"sample_1": "main_dev"})
    store = MemoryArtifactStore(tmp_path, bundle, builder.extraction_settings, builder.models_settings,
                                allow_mock_publication=True)
    published = store.publish(report)
    assert published.memories == tuple(builder.build_memories(sample))
    assert store.load().source_manifest_fingerprint == published.source_manifest_fingerprint


def test_publication_uses_the_builders_planner_reserve_in_identity(tmp_path):
    sample, builder, _ = _setup(checker=_approved, reserved_prompt_tokens=1700)
    report = builder.build_memories_with_report(sample)
    bundle = DatasetBundle(snapshot_sha256="a" * 64, samples=[sample],
                           split_by_sample_id={"sample_1": "main_dev"})
    store = MemoryArtifactStore(
        tmp_path, bundle, builder.extraction_settings, builder.models_settings,
        allow_mock_publication=True, reserved_prompt_tokens=1700,
    )

    artifact = store.publish(report)

    assert store.load().source_manifest_fingerprint == artifact.source_manifest_fingerprint
    incompatible = MemoryArtifactStore(
        tmp_path / "wrong-reserve", bundle, builder.extraction_settings, builder.models_settings,
        allow_mock_publication=True, reserved_prompt_tokens=1800,
    )
    with pytest.raises(ArtifactStoreError, match="ARTIFACT_INCOMPLETE"):
        incompatible.publish(report)


def test_mock_publication_requires_explicit_offline_opt_in(tmp_path):
    store, report = _context(tmp_path)
    store.allow_mock_publication = False
    with pytest.raises(ArtifactStoreError, match="ARTIFACT_INCOMPLETE"):
        store.publish(report)
    assert not (tmp_path / "current.json").exists()


def test_forged_complete_with_missing_window_is_rejected(tmp_path):
    store, report = _context(tmp_path)
    forged = MemoryBuildResult("sample_1", report.memories, "complete", False, 0, 0, (), "b07-v1")
    with pytest.raises(ArtifactStoreError, match="ARTIFACT_INCOMPLETE"):
        store.publish(forged)
    assert not (tmp_path / "current.json").exists()


@pytest.mark.parametrize("mutation", ["schema_version", "missing_field", "memory_id"])
def test_rehashed_but_invalid_artifact_is_rejected(mutation, tmp_path):
    store, report = _context(tmp_path)
    store.publish(report)
    directory = _current_dir(tmp_path)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if mutation == "schema_version":
        manifest["schema_version"] = "2.2"
    elif mutation == "missing_field":
        manifest.pop("source_turns_fingerprint")
    else:
        rows = json.loads((directory / "memories.json").read_text(encoding="utf-8"))
        rows[0]["memory_id"] = "mem_" + "0" * 20
        jsonl = (json.dumps(rows[0], ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
        exported = (json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
        (directory / "memories.jsonl").write_bytes(jsonl)
        (directory / "memories.json").write_bytes(exported)
        manifest["memories_jsonl_sha256"] = sha256(jsonl).hexdigest()
        manifest["memories_json_sha256"] = sha256(exported).hexdigest()
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    (directory / "manifest.json").write_bytes(manifest_bytes)
    pointer = json.loads((tmp_path / "current.json").read_text(encoding="utf-8"))
    pointer["manifest_sha256"] = sha256(manifest_bytes).hexdigest()
    (tmp_path / "current.json").write_text(json.dumps(pointer), encoding="utf-8")
    with pytest.raises(ArtifactStoreError, match="ARTIFACT_CORRUPT"):
        store.load()

