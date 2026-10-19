"""Local sentence embeddings; importing this module never loads a model."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dynamic_memory.contracts import MemoryRecord


class EmbeddingError(ValueError):
    """Invalid text, model configuration or encoder output."""


@dataclass(frozen=True)
class EmbeddingConfig:
    model_name: str
    revision: str
    batch_size: int = 32
    device: str = "cpu"
    local_files_only: bool = True
    query_prompt: str = ""
    document_prompt: str = ""

    def __post_init__(self):
        if not isinstance(self.model_name, str) or not self.model_name.strip():
            raise EmbeddingError("model_name is required")
        if not isinstance(self.revision, str) or not self.revision.strip():
            raise EmbeddingError("revision is required; use an immutable model commit for experiments")
        if type(self.batch_size) is not int or self.batch_size < 1:
            raise EmbeddingError("batch_size must be a positive integer")
        if not isinstance(self.device, str) or not self.device.strip():
            raise EmbeddingError("device is required")
        if type(self.local_files_only) is not bool:
            raise EmbeddingError("local_files_only must be boolean")
        if not all(isinstance(p, str) for p in (self.query_prompt, self.document_prompt)):
            raise EmbeddingError("prompts must be strings")


class SentenceTransformerEmbedder:
    """One reusable model instance, returning normalized plain Python vectors.

    Downloads require explicit local_files_only=False. No cloud inference API
    is used. Long inputs follow the selected model's token truncation policy.
    """

    def __init__(self, config: EmbeddingConfig):
        self._config = config
        self._model = None
        self._dimension: int | None = None

    @property
    def config(self) -> EmbeddingConfig:
        return self._config

    @property
    def configuration_fingerprint(self) -> str:
        """Encoding configuration identity, not a hash of local model weights."""
        payload = {"implementation": "sentence-transformers-v1", **asdict(self.config)}
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def _load_model(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise EmbeddingError("Install the embedding extra: pip install -e '.[embedding]'") from exc
            try:
                self._model = SentenceTransformer(
                    self.config.model_name, revision=self.config.revision,
                    device=self.config.device, local_files_only=self.config.local_files_only,
                    trust_remote_code=False,
                )
            except (OSError, ValueError) as exc:
                raise EmbeddingError("Cannot load embedding model; check its path, revision and local cache") from exc
        return self._model

    def _encode(self, texts: list[str], prompt: str) -> list[list[float]]:
        if not isinstance(texts, list) or any(not isinstance(t, str) or not t.strip() for t in texts):
            raise EmbeddingError("texts must be a list of nonempty strings")
        if not texts:
            return []
        output = self._load_model().encode(
            texts, prompt=prompt, batch_size=self.config.batch_size,
            normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False,
        )
        try:
            vectors = [[float(value) for value in row] for row in output]
        except (TypeError, ValueError, OverflowError) as exc:
            raise EmbeddingError("Model returned invalid vectors") from exc
        if len(vectors) != len(texts):
            raise EmbeddingError("Model returned an incorrect vector count")
        dimension = len(vectors[0])
        if dimension == 0 or (self._dimension is not None and dimension != self._dimension):
            raise EmbeddingError("Model returned an incorrect vector dimension")
        normalized = []
        for row in vectors:
            if len(row) != dimension or not all(math.isfinite(value) for value in row):
                raise EmbeddingError("Model returned inconsistent or nonfinite vectors")
            norm = math.hypot(*row)
            if not math.isfinite(norm) or norm == 0:
                raise EmbeddingError("Model returned an invalid vector norm")
            normalized.append([value / norm for value in row])
        self._dimension = dimension
        return normalized

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._encode(texts, self.config.document_prompt)

    def embed_query(self, question: str) -> list[float]:
        return self._encode([question], self.config.query_prompt)[0]

    def embed_memories(self, memories: list[MemoryRecord]) -> list[list[float]]:
        """Encode content verbatim; vector i belongs to memories[i]."""
        return self.embed_documents([memory.content for memory in memories])
