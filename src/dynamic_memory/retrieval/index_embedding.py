"""Local-file encoder with verified model and encoding identity for indexes."""

from dataclasses import asdict, replace
import hashlib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .embedding import EmbeddingConfig, SentenceTransformerEmbedder
from .index import EncoderIdentity, RetrievalIndexError, fingerprint


def _model_fingerprint(root: Path) -> str:
    """Hash every local model file, including tokenizers, pooling and weights."""
    try:
        files = sorted(path for path in root.rglob("*") if path.is_file())
        if not files:
            raise ValueError("empty model directory")
        entries = []
        for path in files:
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            entries.append((path.relative_to(root).as_posix(), digest.hexdigest()))
        return fingerprint(entries)
    except (OSError, ValueError):
        raise RetrievalIndexError("EMBEDDING_MODEL_UNAVAILABLE") from None


class LocalIndexEncoder:
    """Freeze a local model snapshot; reject later edits rather than reuse stale weights.

    Use a dedicated model directory. This encoder never downloads model files.
    The directory must remain unchanged for the lifetime of the instance.
    """

    def __init__(self, config: EmbeddingConfig):
        root = Path(config.model_name).resolve()
        if not root.is_dir():
            raise RetrievalIndexError("EMBEDDING_MODEL_UNAVAILABLE")
        self._root = root
        effective_config = replace(config, model_name=str(root), local_files_only=True)
        libraries = {}
        for package in ("sentence-transformers", "transformers", "torch"):
            try:
                libraries[package] = version(package)
            except PackageNotFoundError:
                libraries[package] = "unavailable"
        self._identity = EncoderIdentity(
            model_name=config.model_name, model_revision=config.revision,
            model_fingerprint=_model_fingerprint(root),
            encoding_fingerprint=fingerprint({"implementation": "c-local-index-encoder-v1",
                                              "config": asdict(effective_config), "libraries": libraries,
                                              "text": "MemoryRecord.content verbatim", "normalized": True}),
        )
        # Bind to the resolved path so changing the caller's cwd cannot change models.
        self._embedder = SentenceTransformerEmbedder(effective_config)

    @property
    def identity(self) -> EncoderIdentity:
        if _model_fingerprint(self._root) != self._identity.model_fingerprint:
            raise RetrievalIndexError("INDEX_STALE")
        return self._identity

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.identity
        vectors = self._embedder.embed_documents(texts)
        self.identity
        return vectors

    def embed_query(self, question: str) -> list[float]:
        self.identity
        vector = self._embedder.embed_query(question)
        self.identity
        return vector
