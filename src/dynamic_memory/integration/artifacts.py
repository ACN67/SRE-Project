"""Bind B's strict disk loader to A's public read-only service ports."""

from __future__ import annotations

from pathlib import Path
import json
import os
import re
import tempfile
import uuid

from dynamic_memory.contracts import BuildStatus, DatasetBundle, MemoryArtifact
from dynamic_memory.memory.artifact_store import ArtifactStoreError, MemoryArtifactStore, ReviewedMemoryArtifactStore
from dynamic_memory.pipeline.settings import AppSettings
from dynamic_memory.services.dynamic_memory_service import VerifiedMemoryLoad


_SAMPLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}\Z")


class ArtifactPorts:
    """One immutable source bundle and one B publication pointer per sample."""

    def __init__(
        self,
        root: Path,
        bundle: DatasetBundle,
        settings: AppSettings,
        *,
        production: bool = False,
        allowed_splits: tuple[str, ...] = ("main_dev",),
    ):
        self.root = Path(root).resolve()
        self.bundle = bundle
        self.settings = settings
        self._samples = {sample.sample_id for sample in bundle.samples}
        self.production = production is True
        self.allowed_splits = allowed_splits

    def store(self, sample_id: str) -> MemoryArtifactStore:
        if sample_id not in self._samples or not _SAMPLE.fullmatch(sample_id):
            raise ArtifactStoreError("SAMPLE_MISMATCH")
        store_class = ReviewedMemoryArtifactStore if self.production else MemoryArtifactStore
        return store_class(self.root / sample_id, self.bundle,
                           self.settings.extraction, self.settings.models,
                           allow_mock_publication=not self.production,
                           allowed_splits=self.allowed_splits)

    def load(self, sample_id: str) -> MemoryArtifact:
        artifact = self.store(sample_id).load()
        if artifact.manifest.sample_id != sample_id:
            raise ArtifactStoreError("SAMPLE_MISMATCH")
        return artifact

    def _receipt(self, sample_id: str) -> dict | None:
        path = self.store(sample_id).root / "integration_receipt.json"
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if (set(value) != {"sample_id", "build_id", "state"} or
                    value["sample_id"] != sample_id or
                    value["state"] not in ("complete", "partial", "failed") or
                    not isinstance(value["build_id"], str) or
                    not value["build_id"].strip()):
                raise ValueError
            return value
        except (OSError, ValueError, TypeError):
            raise ArtifactStoreError("ARTIFACT_CORRUPT") from None

    def _write_receipt(self, sample_id: str, build_id: str, state: str) -> None:
        store = self.store(sample_id)
        store.root.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".integration_", dir=store.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"sample_id": sample_id, "build_id": build_id,
                           "state": state}, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, store.root / "integration_receipt.json")
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def record_verified(self, artifact: MemoryArtifact) -> None:
        sample_id = artifact.manifest.sample_id
        checked = self.load(sample_id)
        if checked.source_manifest_fingerprint != artifact.source_manifest_fingerprint:
            raise ArtifactStoreError("ARTIFACT_STALE")
        self._write_receipt(sample_id, checked.manifest.build_id, "complete")

    def record_failed_attempt(self, sample_id: str, state: str) -> None:
        if state not in ("partial", "failed"):
            raise ValueError("invalid build attempt")
        # A complete published version remains current even if a new attempt fails.
        if (self.store(sample_id).root / "current.json").exists():
            return
        self._write_receipt(sample_id, "attempt_" + uuid.uuid4().hex, state)

    def build_status(self, sample_id: str) -> BuildStatus:
        store = self.store(sample_id)
        try:
            artifact = self.load(sample_id)
        except ArtifactStoreError as exc:
            receipt = self._receipt(sample_id)
            if receipt is not None and receipt["state"] == "complete" and exc.code in (
                    "ARTIFACT_CORRUPT", "ARTIFACT_STALE"):
                return BuildStatus(dataset_state="ready", memory_state=(
                    "corrupt" if exc.code == "ARTIFACT_CORRUPT" else "stale"),
                    partial_build=False, build_id=receipt["build_id"], error_code=exc.code)
            if receipt is not None and receipt["state"] in ("partial", "failed") and exc.code == "BUILD_NOT_FOUND":
                state = receipt["state"]
                return BuildStatus(dataset_state="ready", memory_state=state,
                    partial_build=True, build_id=receipt["build_id"],
                    error_code="ARTIFACT_INCOMPLETE" if state == "partial" else "MEMORY_BUILD_FAILED")
            if exc.code == "BUILD_NOT_FOUND":
                return BuildStatus(dataset_state="ready", memory_state="not_built",
                                   partial_build=False, error_code="BUILD_NOT_FOUND")
            # A corrupt or stale pointer cannot establish a trusted build ID.
            # Fail closed instead of inventing one to satisfy the DTO.
            raise
        return BuildStatus(dataset_state="ready", memory_state="complete",
                           partial_build=False, build_id=artifact.manifest.build_id)

    def verified(self, sample_id: str) -> VerifiedMemoryLoad:
        artifact = self.load(sample_id)
        return VerifiedMemoryLoad(artifact=artifact, sample_id=sample_id,
            dataset_snapshot_sha256=self.bundle.snapshot_sha256,
            source_manifest_fingerprint=artifact.source_manifest_fingerprint,
            provenance="b_verified_loader", verification_id=artifact.manifest.build_id)
