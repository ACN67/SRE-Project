"""Index identity, isolation, persistence and failure tests with synthetic data."""

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
import hashlib
import json

import pytest

from dynamic_memory.contracts import FailedWindow, MemoryArtifact, MemoryManifest, MemoryRecord, SourceRef, memory_set_fingerprint
from dynamic_memory.contracts.memory_id import compute_memory_id
from dynamic_memory.pipeline.settings import load_settings
from dynamic_memory.retrieval import EncoderIdentity, IndexManager, RetrievalIndexError, save_index
from dynamic_memory.retrieval.index import fingerprint


class FakeEncoder:
    def __init__(self):
        self.identity = EncoderIdentity(model_name="synthetic-model", model_revision="test-v1",
                                        model_fingerprint=fingerprint("synthetic-weights"),
                                        encoding_fingerprint=fingerprint("synthetic-encoding"))
        self.calls = []

    def embed_documents(self, texts):
        self.calls.append(list(texts))
        return [[0.6, 0.8] if "teacher" in text else [1.0, 0.0] for text in texts]


def artifact(*, sample="synthetic_001", count=2, status="complete", build="synthetic-build-v1"):
    memories = []
    for number in range(count):
        draft = MemoryRecord(memory_id="mem_" + "0" * 20, sample_id=sample,
                             content="Sam is a teacher." if number == 0 else "Sam likes football.",
                             fact_kind="state", assertion_type="fact", polarity="positive",
                             source_refs=[SourceRef(session_id="s1", dia_id=f"d{number}")],
                             extractor_version="synthetic-v1")
        memories.append(draft.model_copy(update={"memory_id": compute_memory_id(draft)}))
    failures = () if status == "complete" else (FailedWindow(extraction_window_id="failed", error_code="TEST_FAILURE"),)
    successful = 0 if status == "failed" else 1
    manifest = MemoryManifest(
        build_id=build, sample_id=sample, created_at=datetime(2026, 10, 26, tzinfo=timezone.utc),
        dataset_snapshot_sha256=hashlib.sha256(b"synthetic-snapshot").hexdigest(),
        source_turns_fingerprint=fingerprint("synthetic-turns"), extraction_config_fingerprint=fingerprint("synthetic-config"),
        extractor_version="synthetic-v1", prompt_fingerprint=fingerprint("synthetic-prompt"),
        backend="mock", resolved_model_id=None, build_status=status, partial_build=bool(failures),
        total_windows=successful + len(failures), successful_windows=successful, failed_windows=failures,
        memory_count=count, memories_jsonl_sha256=hashlib.sha256(b"synthetic-jsonl").hexdigest(),
        memory_set_fingerprint=memory_set_fingerprint(memories),
    )
    return MemoryArtifact(manifest=manifest, memories=tuple(memories))


def settings(**changes):
    return load_settings().retrieval.model_copy(update={"embedding_model": "synthetic-model", **changes})


def assert_code(code, action):
    with pytest.raises(RetrievalIndexError) as caught:
        action()
    assert caught.value.code == code


def test_build_preserves_id_text_vector_alignment_and_order_independence():
    source, encoder = artifact(), FakeEncoder()
    manager = IndexManager(encoder)
    index = manager.build_index(source, settings())
    assert index.memory_ids == tuple(sorted(m.memory_id for m in source.memories))
    assert encoder.calls == [[m.content for m in index.memories]]
    assert index.sample_id == source.manifest.sample_id
    assert index.source_manifest_fingerprint == source.source_manifest_fingerprint
    assert index.dimension == 2
    for record, vector in zip(index.memories, index.vectors):
        assert vector == ((0.6, 0.8) if "teacher" in record.content else (1.0, 0.0))
    reordered = source.model_copy(update={"memories": tuple(reversed(source.memories))})
    assert manager.build_index(reordered, settings()) == index


def test_save_reload_without_reencoding(tmp_path):
    source, encoder = artifact(), FakeEncoder()
    manager = IndexManager(encoder)
    index = manager.build_index(source, settings())
    path = tmp_path / "index.json"
    save_index(index, path)
    assert manager.load_index(path, source, settings()) == index
    assert len(encoder.calls) == 1
    assert "Sam is a teacher" not in path.read_text(encoding="utf-8")


def test_empty_index_does_not_invoke_encoder(tmp_path):
    source, encoder = artifact(count=0), FakeEncoder()
    manager = IndexManager(encoder)
    index = manager.build_index(source, settings())
    assert index.dimension == 0
    assert index.memory_ids == index.vectors == index.memories == ()
    path = tmp_path / "empty.json"
    save_index(index, path)
    assert manager.load_index(path, source, settings()) == index
    assert encoder.calls == []


@pytest.mark.parametrize("status,count", [("partial", 1), ("failed", 0)])
def test_incomplete_artifact_rejected_before_encoding(status, count):
    encoder = FakeEncoder()
    assert_code("ARTIFACT_INCOMPLETE", lambda: IndexManager(encoder).build_index(artifact(status=status, count=count), settings()))
    assert encoder.calls == []


def test_bare_list_and_mutated_artifact_are_rejected():
    source, encoder = artifact(), FakeEncoder()
    manager = IndexManager(encoder)
    assert_code("ARTIFACT_CORRUPT", lambda: manager.build_index(list(source.memories), settings()))
    source.memories[0].content = "tampered fact"
    assert_code("ARTIFACT_CORRUPT", lambda: manager.build_index(source, settings()))
    assert encoder.calls == []


def test_index_isolated_from_input_and_returned_memory_mutations():
    source = artifact()
    index = IndexManager(FakeEncoder()).build_index(source, settings())
    before = index.memories
    source.memories[0].content = "input changed"
    index.memories[0].content = "returned copy changed"
    assert index.memories == before
    with pytest.raises(FrozenInstanceError):
        index._artifact_json = "bad"


@pytest.mark.parametrize("changed", [
    {"embedding_model": None}, {"embedding_model": "other-model"}, {"base_top_k": 0},
])
def test_invalid_settings_rejected(changed):
    assert_code("INDEX_CONFIGURATION_INVALID", lambda: IndexManager(FakeEncoder()).build_index(artifact(), settings(**changed)))


@pytest.mark.parametrize("rows", [[], [[0., 0.], [1., 0.]], [[float('nan'), 1.], [1., 0.]],
                                  [[1., 0.], [1.]], [[2., 0.], [1., 0.]], [[True, False], [1., 0.]]])
def test_invalid_vectors_rejected(rows):
    encoder = FakeEncoder()
    encoder.embed_documents = lambda texts: rows
    assert_code("EMBEDDING_OUTPUT_INVALID", lambda: IndexManager(encoder).build_index(artifact(), settings()))


def test_encoder_failure_is_sanitized():
    encoder = FakeEncoder()
    def fail(texts):
        raise RuntimeError("private-provider-text")
    encoder.embed_documents = fail
    assert_code("EMBEDDING_FAILED", lambda: IndexManager(encoder).build_index(artifact(), settings()))


@pytest.mark.parametrize("field", ["model_revision", "model_fingerprint", "encoding_fingerprint"])
def test_encoder_change_invalidates_saved_index(tmp_path, field):
    source, encoder = artifact(), FakeEncoder()
    manager = IndexManager(encoder)
    path = tmp_path / "index.json"
    save_index(manager.build_index(source, settings()), path)
    value = "test-v2" if field == "model_revision" else fingerprint("changed")
    encoder.identity = encoder.identity.model_copy(update={field: value})
    assert_code("INDEX_STALE", lambda: manager.load_index(path, source, settings()))


def test_source_settings_and_sample_changes_rejected(tmp_path):
    source, manager = artifact(), IndexManager(FakeEncoder())
    path = tmp_path / "index.json"
    save_index(manager.build_index(source, settings()), path)
    assert_code("INDEX_STALE", lambda: manager.load_index(path, artifact(build="new-build"), settings()))
    assert_code("INDEX_STALE", lambda: manager.load_index(path, source, settings(base_top_k=7)))
    assert_code("SAMPLE_MISMATCH", lambda: manager.load_index(path, artifact(sample="other"), settings()))


def test_model_change_during_encoding_rejected():
    encoder = FakeEncoder()
    def encode(texts):
        encoder.identity = encoder.identity.model_copy(update={"model_revision": "changed"})
        return [[1., 0.] for _ in texts]
    encoder.embed_documents = encode
    assert_code("INDEX_STALE", lambda: IndexManager(encoder).build_index(artifact(), settings()))


@pytest.mark.parametrize("mutation", ["checksum", "duplicate", "dimension", "nan", "version", "unknown-field"])
def test_corrupt_files_rejected(tmp_path, mutation):
    source, manager = artifact(), IndexManager(FakeEncoder())
    path = tmp_path / "index.json"
    save_index(manager.build_index(source, settings()), path)
    data = json.loads(path.read_text(encoding="utf-8"))
    payload = data["payload"]
    if mutation == "checksum":
        payload["vectors"][0] = [0., 1.]
    elif mutation == "duplicate":
        payload["memory_ids"][1] = payload["memory_ids"][0]
    elif mutation == "dimension":
        payload["dimension"] = 3
    elif mutation == "nan":
        payload["vectors"][0][0] = float("nan")
    elif mutation == "version":
        payload["format_version"] = 99
    else:
        payload["gold"] = "must not be accepted"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert_code("INDEX_CORRUPT", lambda: manager.load_index(path, source, settings()))


def test_missing_file_and_failed_replace_preserve_previous_index(tmp_path, monkeypatch):
    source, manager = artifact(), IndexManager(FakeEncoder())
    path = tmp_path / "index.json"
    assert_code("INDEX_NOT_FOUND", lambda: manager.load_index(path, source, settings()))
    index = manager.build_index(source, settings())
    save_index(index, path)
    before = path.read_bytes()
    def fail_replace(*args):
        raise OSError("synthetic interruption")
    monkeypatch.setattr("dynamic_memory.retrieval.index.os.replace", fail_replace)
    assert_code("INDEX_WRITE_FAILED", lambda: save_index(index, path))
    assert path.read_bytes() == before
    assert list(tmp_path.glob(".index-*.tmp")) == []
