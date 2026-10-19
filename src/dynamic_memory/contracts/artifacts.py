"""Shared verified-in-memory Memory artifact contract for the B-to-C handoff.

B's disk loader must additionally verify stored bytes, source turns, snapshot
identity and the current published build before exposing an artifact to C.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from .memory_id import validate_memory_id
from .models import ContractModel, MemoryRecord, SCHEMA_VERSION

SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")
FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _json_fingerprint(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def memory_set_fingerprint(memories: tuple[MemoryRecord, ...] | list[MemoryRecord]) -> str:
    """Canonical sorted in-memory records, distinct from hashes of JSONL bytes."""
    if not all(isinstance(record, MemoryRecord) for record in memories):
        raise ValueError("Memory set requires validated MemoryRecord objects")
    by_id = sorted(memories, key=lambda m: m.memory_id)
    if len({m.memory_id for m in by_id}) != len(by_id):
        raise ValueError("Memory set contains duplicate memory IDs")
    return _json_fingerprint([m.model_dump(mode="json") for m in by_id])


class FailedWindow(ContractModel):
    model_config = ConfigDict(frozen=True)
    extraction_window_id: str = Field(min_length=1)
    error_code: str = Field(min_length=1)


class MemoryManifest(ContractModel):
    """Format v1; the business DTO schema remains 2.1."""
    model_config = ConfigDict(frozen=True)

    manifest_format_version: Literal[1] = 1
    schema_version: Literal["2.1"] = SCHEMA_VERSION
    build_id: str = Field(min_length=1)
    sample_id: str = Field(min_length=1)
    created_at: datetime
    dataset_snapshot_sha256: str
    source_turns_fingerprint: str
    extraction_config_fingerprint: str
    extractor_version: str = Field(min_length=1)
    prompt_fingerprint: str
    backend: Literal["mock", "deepseek"]
    resolved_model_id: str | None
    build_status: Literal["complete", "partial", "failed"]
    partial_build: bool
    total_windows: int = Field(ge=0)
    successful_windows: int = Field(ge=0)
    failed_windows: tuple[FailedWindow, ...]
    memory_count: int = Field(ge=0)
    memories_jsonl_sha256: str
    memories_json_sha256: str | None = None
    memory_set_fingerprint: str

    @model_validator(mode="after")
    def consistent_manifest(self):
        for name in ("dataset_snapshot_sha256", "memories_jsonl_sha256"):
            if not SHA256_HEX.fullmatch(getattr(self, name)):
                raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")
        if self.memories_json_sha256 is not None and not SHA256_HEX.fullmatch(self.memories_json_sha256):
            raise ValueError("memories_json_sha256 must be a lowercase SHA-256 hex digest")
        for name in ("source_turns_fingerprint", "extraction_config_fingerprint", "prompt_fingerprint", "memory_set_fingerprint"):
            if not FINGERPRINT.fullmatch(getattr(self, name)):
                raise ValueError(f"{name} must be a sha256: fingerprint")
        if self.backend == "deepseek" and (not self.resolved_model_id or not self.resolved_model_id.strip()):
            raise ValueError("DeepSeek requires resolved_model_id")
        if self.backend == "mock" and self.resolved_model_id is not None:
            raise ValueError("Mock must not claim a real model ID")
        if self.successful_windows + len(self.failed_windows) != self.total_windows:
            raise ValueError("window counts do not agree")
        ids = [w.extraction_window_id for w in self.failed_windows]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate failed window ID")
        if self.partial_build != bool(self.failed_windows):
            raise ValueError("partial_build must indicate failed windows")
        expected_status = (
            "complete" if not self.failed_windows else
            "partial" if self.successful_windows else "failed"
        )
        if self.build_status != expected_status:
            raise ValueError("build_status differs from window counts")
        if self.build_status == "failed" and self.memory_count != 0:
            raise ValueError("failed builds cannot publish Memory")
        return self

    @property
    def source_manifest_fingerprint(self) -> str:
        """Deterministic manifest identity: includes build, versions, bytes and state."""
        return _json_fingerprint(self.model_dump(mode="json"))


class MemoryArtifact(ContractModel):
    """Validated in-memory pairing; not proof that bytes/source were loaded.

    Production callers MUST acquire this from B's strict load_memory_artifact,
    which verifies file hashes, SourceRefs, snapshot and the published pointer.
    """

    model_config = ConfigDict(frozen=True)
    manifest: MemoryManifest
    memories: tuple[MemoryRecord, ...]

    @model_validator(mode="after")
    def consistent_contents(self):
        manifest = self.manifest
        if len(self.memories) != manifest.memory_count:
            raise ValueError("manifest memory_count mismatch")
        for record in self.memories:
            if record.sample_id != manifest.sample_id or record.extractor_version != manifest.extractor_version:
                raise ValueError("Memory identity differs from manifest")
            validate_memory_id(record)
        if memory_set_fingerprint(self.memories) != manifest.memory_set_fingerprint:
            raise ValueError("memory_set_fingerprint mismatch")
        return self

    @property
    def source_manifest_fingerprint(self) -> str:
        return self.manifest.source_manifest_fingerprint

    def require_complete_for_index(self) -> None:
        """C's mandatory preflight, in addition to source/index version checks."""
        self.consistent_contents()
        if self.manifest.build_status != "complete" or self.manifest.partial_build:
            raise ValueError("ARTIFACT_INCOMPLETE: formal index requires a complete build")
