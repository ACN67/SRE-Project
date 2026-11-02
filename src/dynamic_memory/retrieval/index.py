"""Sample-local immutable indexes, backed by verified B MemoryArtifact inputs.

The caller must obtain production artifacts from B's strict loader. In-memory
validation here cannot establish disk/source provenance on B's behalf.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

from dynamic_memory.contracts import MemoryArtifact, MemoryRecord
from dynamic_memory.pipeline.settings import RetrievalSettings

INDEX_IMPLEMENTATION = "c-local-index-v2-history"
_DIGEST = r"^sha256:[0-9a-f]{64}$"


class RetrievalIndexError(ValueError):
    """Stable diagnostic code without source text, paths or provider details."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(f"{code}: retrieval index operation failed")


def fingerprint(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)


class EncoderIdentity(_FrozenModel):
    """An injected encoder promises these identities describe its actual model."""

    model_name: str = Field(min_length=1)
    model_revision: str = Field(min_length=1)
    model_fingerprint: str = Field(pattern=_DIGEST)
    encoding_fingerprint: str = Field(pattern=_DIGEST)


class IndexEncoder(Protocol):
    @property
    def identity(self) -> EncoderIdentity: ...

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...


class _IndexDocument(_FrozenModel):
    format_version: Literal[1] = 1
    implementation: Literal["c-local-index-v2-history"] = INDEX_IMPLEMENTATION
    sample_id: str = Field(min_length=1)
    source_manifest_fingerprint: str = Field(pattern=_DIGEST)
    settings_fingerprint: str = Field(pattern=_DIGEST)
    encoder_identity: EncoderIdentity
    retriever_fingerprint: str = Field(pattern=_DIGEST)
    memory_ids: tuple[str, ...]
    dimension: int = Field(ge=0)
    vectors: tuple[tuple[FiniteFloat, ...], ...]

    @model_validator(mode="after")
    def consistent_vectors(self):
        if self.memory_ids != tuple(sorted(set(self.memory_ids))):
            raise ValueError("memory_ids must be unique and sorted")
        if len(self.vectors) != len(self.memory_ids):
            raise ValueError("vector count mismatch")
        if not self.memory_ids:
            if self.dimension != 0:
                raise ValueError("empty index must have dimension zero")
        elif self.dimension == 0:
            raise ValueError("nonempty index requires positive dimension")
        for row in self.vectors:
            if len(row) != self.dimension or not math.isclose(math.hypot(*row), 1.0, abs_tol=1e-6):
                raise ValueError("vectors must have consistent dimension and unit norm")
        if self.retriever_fingerprint != _retriever_fingerprint(
            self.source_manifest_fingerprint, self.settings_fingerprint, self.encoder_identity,
        ):
            raise ValueError("retriever fingerprint mismatch")
        return self


class _IndexFile(_FrozenModel):
    payload: _IndexDocument
    payload_fingerprint: str = Field(pattern=_DIGEST)

    @model_validator(mode="after")
    def checksum(self):
        if self.payload_fingerprint != fingerprint(self.payload.model_dump(mode="json")):
            raise ValueError("index checksum mismatch")
        return self


def _retriever_fingerprint(source: str, settings: str, encoder: EncoderIdentity) -> str:
    return fingerprint({"implementation": INDEX_IMPLEMENTATION, "source_manifest": source,
                        "settings": settings, "encoder": encoder.model_dump(mode="json")})


def _reject_extra_fields(value: object) -> None:
    if isinstance(value, BaseModel):
        if set(value.__dict__) != set(type(value).model_fields) or value.__pydantic_extra__:
            raise ValueError("unexpected fields")
        for child in value.__dict__.values():
            _reject_extra_fields(child)
    elif isinstance(value, (tuple, list)):
        for child in value:
            _reject_extra_fields(child)


def _artifact_snapshot(artifact: MemoryArtifact) -> MemoryArtifact:
    try:
        if type(artifact) is not MemoryArtifact:
            raise ValueError("MemoryArtifact required")
        _reject_extra_fields(artifact)
        checked = MemoryArtifact.model_validate_json(artifact.model_dump_json())
    except Exception:
        raise RetrievalIndexError("ARTIFACT_CORRUPT") from None
    if checked.manifest.build_status != "complete" or checked.manifest.partial_build:
        raise RetrievalIndexError("ARTIFACT_INCOMPLETE")
    checked.require_complete_for_index()
    return checked.model_copy(update={"memories": tuple(sorted(checked.memories, key=lambda m: m.memory_id))})


def _encoder_snapshot(encoder: IndexEncoder) -> EncoderIdentity:
    try:
        identity = encoder.identity
        if type(identity) is not EncoderIdentity:
            raise ValueError("EncoderIdentity required")
        _reject_extra_fields(identity)
        return EncoderIdentity.model_validate_json(identity.model_dump_json())
    except RetrievalIndexError:
        raise
    except Exception:
        raise RetrievalIndexError("INDEX_CONFIGURATION_INVALID") from None


@dataclass(frozen=True, slots=True)
class RetrievalIndex:
    """Serialized immutable snapshots; properties never expose mutable storage."""

    _document_json: str = field(repr=False)
    _artifact_json: str = field(repr=False)

    def __post_init__(self):
        try:
            document = _IndexDocument.model_validate_json(self._document_json)
            artifact = _artifact_snapshot(MemoryArtifact.model_validate_json(self._artifact_json))
            if (document.sample_id != artifact.manifest.sample_id or
                    document.source_manifest_fingerprint != artifact.source_manifest_fingerprint or
                    document.memory_ids != tuple(sorted(m.memory_id for m in artifact.memories))):
                raise ValueError("index/source mismatch")
        except Exception:
            raise RetrievalIndexError("INDEX_CORRUPT") from None

    @property
    def sample_id(self) -> str:
        return self._document().sample_id

    @property
    def source_manifest_fingerprint(self) -> str:
        return self._document().source_manifest_fingerprint

    @property
    def retriever_fingerprint(self) -> str:
        return self._document().retriever_fingerprint

    @property
    def dimension(self) -> int:
        return self._document().dimension

    @property
    def memory_ids(self) -> tuple[str, ...]:
        return self._document().memory_ids

    @property
    def vectors(self) -> tuple[tuple[float, ...], ...]:
        return self._document().vectors

    @property
    def memories(self) -> tuple[MemoryRecord, ...]:
        artifact = MemoryArtifact.model_validate_json(self._artifact_json)
        return tuple(sorted(artifact.memories, key=lambda m: m.memory_id))

    def _document(self) -> _IndexDocument:
        return _IndexDocument.model_validate_json(self._document_json)


class IndexManager:
    """Bind a versioned encoder once; expose the formal two-argument build API."""

    def __init__(self, encoder: IndexEncoder):
        self._encoder = encoder

    def _context(self, artifact: MemoryArtifact, settings: RetrievalSettings):
        artifact = _artifact_snapshot(artifact)
        try:
            if type(settings) is not RetrievalSettings:
                raise ValueError("RetrievalSettings required")
            _reject_extra_fields(settings)
            settings = RetrievalSettings.model_validate(settings.model_dump())
            identity = _encoder_snapshot(self._encoder)
            if not settings.embedding_model or settings.embedding_model != identity.model_name:
                raise ValueError("configured model differs from encoder")
        except RetrievalIndexError:
            raise
        except Exception:
            raise RetrievalIndexError("INDEX_CONFIGURATION_INVALID") from None
        return artifact, fingerprint(settings.model_dump(mode="json")), identity

    def build_index(self, artifact: MemoryArtifact, settings: RetrievalSettings) -> RetrievalIndex:
        artifact, settings_fp, identity = self._context(artifact, settings)
        memories = sorted(artifact.memories, key=lambda m: m.memory_id)
        try:
            raw_vectors = self._encoder.embed_documents([m.content for m in memories]) if memories else []
            vectors = tuple(tuple(row) for row in raw_vectors)
        except RetrievalIndexError:
            raise
        except Exception:
            raise RetrievalIndexError("EMBEDDING_FAILED") from None
        if _encoder_snapshot(self._encoder) != identity:
            raise RetrievalIndexError("INDEX_STALE")
        try:
            document = _IndexDocument(
                sample_id=artifact.manifest.sample_id,
                source_manifest_fingerprint=artifact.source_manifest_fingerprint,
                settings_fingerprint=settings_fp, encoder_identity=identity,
                retriever_fingerprint=_retriever_fingerprint(artifact.source_manifest_fingerprint, settings_fp, identity),
                memory_ids=tuple(m.memory_id for m in memories),
                dimension=len(vectors[0]) if vectors else 0, vectors=vectors,
            )
        except Exception:
            raise RetrievalIndexError("EMBEDDING_OUTPUT_INVALID") from None
        return RetrievalIndex(document.model_dump_json(), artifact.model_dump_json())

    def load_index(self, path: str | Path, artifact: MemoryArtifact, settings: RetrievalSettings) -> RetrievalIndex:
        """Require a fresh verified artifact and current encoder identity on every load."""
        artifact, settings_fp, identity = self._context(artifact, settings)
        try:
            file = _IndexFile.model_validate_json(Path(path).read_bytes())
        except FileNotFoundError:
            raise RetrievalIndexError("INDEX_NOT_FOUND") from None
        except OSError:
            raise RetrievalIndexError("INDEX_READ_FAILED") from None
        except Exception:
            raise RetrievalIndexError("INDEX_CORRUPT") from None
        document = file.payload
        if document.sample_id != artifact.manifest.sample_id:
            raise RetrievalIndexError("SAMPLE_MISMATCH")
        expected = _retriever_fingerprint(artifact.source_manifest_fingerprint, settings_fp, identity)
        if document.retriever_fingerprint != expected:
            raise RetrievalIndexError("INDEX_STALE")
        return RetrievalIndex(document.model_dump_json(), artifact.model_dump_json())


def save_index(index: RetrievalIndex, path: str | Path) -> None:
    """Atomically replace a single JSON file after fully validating its payload."""
    if type(index) is not RetrievalIndex:
        raise RetrievalIndexError("INDEX_CORRUPT")
    index.__post_init__()
    document = index._document()
    file = _IndexFile(payload=document, payload_fingerprint=fingerprint(document.model_dump(mode="json")))
    target = Path(path)
    temporary: Path | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                         prefix=".index-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(file.model_dump_json())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except OSError:
        raise RetrievalIndexError("INDEX_WRITE_FAILED") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # Preserve the original write failure; this owned temporary file
                # is never treated as a published index.
                pass
