"""Offline encoder boundary tests: no model downloads or inference claims."""

import math
import sys
from types import SimpleNamespace

import pytest

from dynamic_memory.retrieval import EmbeddingConfig, EmbeddingError, SentenceTransformerEmbedder


class FakeModel:
    def __init__(self, output=None):
        self.calls = []
        self.output = output

    def encode(self, texts, **kwargs):
        self.calls.append((texts, kwargs))
        return self.output if self.output is not None else [[3, 4] for _ in texts]


def encoder(monkeypatch, output=None, **config):
    loads = []
    model = FakeModel(output)

    def factory(*args, **kwargs):
        loads.append((args, kwargs))
        return model

    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=factory))
    embedder = SentenceTransformerEmbedder(EmbeddingConfig("fixture-model", "fixture-v1", **config))
    return embedder, model, loads


def test_lazy_loading_offline_defaults_and_model_reuse(monkeypatch):
    embedder, model, loads = encoder(monkeypatch)
    assert not loads
    assert embedder.embed_documents([]) == []
    assert not loads
    assert embedder.embed_query("Where does Sam work?") == pytest.approx([0.6, 0.8])
    embedder.embed_documents(["Sam works in Paris."])
    assert len(loads) == 1
    assert loads[0][1] == dict(revision="fixture-v1", device="cpu", local_files_only=True, trust_remote_code=False)
    assert model.calls[0][1]["normalize_embeddings"] is True


def test_memories_preserve_order_content_and_separate_prompts(monkeypatch):
    embedder, model, _ = encoder(monkeypatch, query_prompt="query: ", document_prompt="passage: ")
    memories = [SimpleNamespace(content="First fact"), SimpleNamespace(content="Second fact")]
    assert len(embedder.embed_memories(memories)) == 2
    assert model.calls[0][0] == ["First fact", "Second fact"]
    assert model.calls[0][1]["prompt"] == "passage: "
    embedder.embed_query("Question")
    assert model.calls[1][1]["prompt"] == "query: "


@pytest.mark.parametrize("texts", [[""], ["   "], [None], "single string"])
def test_invalid_text_rejected_before_loading(monkeypatch, texts):
    embedder, _, loads = encoder(monkeypatch)
    with pytest.raises(EmbeddingError):
        embedder.embed_documents(texts)
    assert not loads


@pytest.mark.parametrize("output", [[], [[0, 0]], [[math.nan, 1]], [[math.inf, 1]], [[1]], [["bad", 2]]])
def test_bad_model_output_rejected(monkeypatch, output):
    embedder, _, _ = encoder(monkeypatch, output)
    # Establish the dimension before testing a potentially incompatible result.
    embedder._model = FakeModel()
    embedder.embed_query("initial")
    embedder._model = FakeModel(output)
    with pytest.raises(EmbeddingError):
        embedder.embed_query("next")


def test_ragged_vectors_rejected(monkeypatch):
    embedder, _, _ = encoder(monkeypatch, [[1, 2], [1]])
    with pytest.raises(EmbeddingError):
        embedder.embed_documents(["one", "two"])


@pytest.mark.parametrize("overrides", [dict(model_name=""), dict(revision=""), dict(batch_size=0), dict(batch_size=True), dict(query_prompt=None), dict(local_files_only="false")])
def test_config_validation(overrides):
    values = dict(model_name="fixture-model", revision="fixture-v1")
    values.update(overrides)
    with pytest.raises(EmbeddingError):
        EmbeddingConfig(**values)


def test_config_identity_changes_with_model_revision_and_text_encoding():
    def fingerprint(**kwargs):
        return SentenceTransformerEmbedder(EmbeddingConfig("fixture-model", **kwargs)).configuration_fingerprint

    first = fingerprint(revision="v1")
    assert first == fingerprint(revision="v1")
    assert first != fingerprint(revision="v2")
    assert first != fingerprint(revision="v1", query_prompt="query: ")


def test_missing_optional_dependency_has_actionable_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    embedder = SentenceTransformerEmbedder(EmbeddingConfig("fixture-model", "v1"))
    with pytest.raises(EmbeddingError, match="embedding"):
        embedder.embed_query("question")
