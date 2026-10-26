"""Verify local model file changes invalidate identity without loading a model."""

import pytest

from dynamic_memory.retrieval import EmbeddingConfig, LocalIndexEncoder, RetrievalIndexError


def model_dir(tmp_path):
    root = tmp_path / "model"
    root.mkdir()
    (root / "config.json").write_text('{}', encoding="utf-8")
    (root / "model.safetensors").write_bytes(b"synthetic-weight-file")
    return root


def test_stable_identity_and_prompt_changes(tmp_path):
    root = model_dir(tmp_path)
    config = EmbeddingConfig(str(root), "synthetic-v1")
    a, b = LocalIndexEncoder(config), LocalIndexEncoder(config)
    assert a.identity == b.identity
    c = LocalIndexEncoder(EmbeddingConfig(str(root), "synthetic-v1", query_prompt="query: "))
    assert a.identity.encoding_fingerprint != c.identity.encoding_fingerprint
    assert a.identity.model_fingerprint == c.identity.model_fingerprint


@pytest.mark.parametrize("filename", ["model.safetensors", "config.json", "tokenizer.json"])
def test_model_or_tokenizer_changes_reject_cached_encoder(tmp_path, filename):
    root = model_dir(tmp_path)
    encoder = LocalIndexEncoder(EmbeddingConfig(str(root), "synthetic-v1"))
    old = encoder.identity
    (root / filename).write_bytes(b"changed")
    with pytest.raises(RetrievalIndexError, match="INDEX_STALE"):
        encoder.embed_documents(["must not load model"])
    fresh = LocalIndexEncoder(EmbeddingConfig(str(root), "synthetic-v1"))
    assert fresh.identity.model_fingerprint != old.model_fingerprint


def test_absent_or_empty_model_directory(tmp_path):
    for root in (tmp_path, tmp_path / "missing"):
        with pytest.raises(RetrievalIndexError, match="EMBEDDING_MODEL_UNAVAILABLE"):
            LocalIndexEncoder(EmbeddingConfig(str(root), "synthetic-v1"))
