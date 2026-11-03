"""B-08 offline Memory artifact publication and strict disk/source verification."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from dynamic_memory.contracts import (
    ConversationSample, DatasetBundle, MemoryArtifact, MemoryManifest, MemoryRecord,
    SourceRef, ProductionProvenance, memory_set_fingerprint,
)
from dynamic_memory.contracts.memory_id import validate_memory_id
from dynamic_memory.contracts import MemoryBuildResult
from dynamic_memory.data.repository import SourceRepository
from dynamic_memory.memory.extraction import MEMORY_DRAFT_PROMPT, WindowExtractionResult
from dynamic_memory.memory.normalization import merge_reviewed_memories, normalize_memories
from dynamic_memory.memory.review_workflow import VerifiedReview, _fp as _review_fp
from dynamic_memory.memory.windows import WindowPlanningError, plan_extraction_windows
from dynamic_memory.pipeline.settings import ExtractionSettings, ModelsSettings

_BUILD_ID = re.compile(r"build_[0-9a-f]{32}\Z")
_POINTER = "current.json"
_OFFICIAL_LOCOMO_SHA = "79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4"


class WindowReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    extraction_window_id: str = Field(min_length=1)
    source_refs_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    approved_fact_count: int = Field(ge=0)
    excluded_fact_count: int = Field(default=0, ge=0)
    decision: str = Field(pattern=r"^approved$")


class ReviewAttestation(BaseModel):
    """Caller-supplied independent review receipt; never inferred from model output."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    run_kind: str = Field(pattern=r"^(offline_test|real)$")
    reviewer_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
    execution_policy_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    reviewed_memory_set_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    review_log_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    review_authority_fingerprint: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    windows: tuple[WindowReview, ...] = Field(min_length=1)


class ArtifactStoreError(ValueError):
    """Sanitized error: never embeds raw Memory, dialogue, manifest or credentials."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"{code}: Memory artifact unavailable")


def _fail(code: str) -> None:
    raise ArtifactStoreError(code) from None


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _fp(value: object) -> str:
    return "sha256:" + _sha(_json_bytes(value))


def _write(path: Path, payload: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


class MemoryArtifactStore:
    """One snapshot/config identity, with a single atomically switched build pointer.

    This store deliberately publishes only offline Mock results. A production
    writer needs a trusted evidence reviewer and the shared build-result DTO.
    """

    def __init__(self, root: Path, bundle: DatasetBundle,
                 extraction_settings: ExtractionSettings, models_settings: ModelsSettings,
                 *, allow_mock_publication: bool = False,
                 reserved_prompt_tokens: int = 1800) -> None:
        if not isinstance(root, (str, Path)) or not isinstance(bundle, DatasetBundle):
            _fail("ARTIFACT_CORRUPT")
        if not isinstance(extraction_settings, ExtractionSettings) or not isinstance(models_settings, ModelsSettings):
            _fail("ARTIFACT_CORRUPT")
        try:
            self.bundle = DatasetBundle.model_validate(bundle.model_dump(mode="python", warnings="error"))
            self.extraction_settings = ExtractionSettings.model_validate(
                extraction_settings.model_dump(mode="python", warnings="error"))
            self.models_settings = ModelsSettings.model_validate(models_settings.model_dump(mode="python", warnings="error"))
        except (ValidationError, ValueError, TypeError, AttributeError):
            _fail("ARTIFACT_CORRUPT")
        self.root = Path(root)
        self.allow_mock_publication = allow_mock_publication is True
        if (isinstance(reserved_prompt_tokens, bool)
                or not isinstance(reserved_prompt_tokens, int)
                or not 0 < reserved_prompt_tokens < self.extraction_settings.window_max_input_tokens):
            _fail("ARTIFACT_CORRUPT")
        self.reserved_prompt_tokens = reserved_prompt_tokens

    def _sample(self, sample_id: str) -> ConversationSample:
        sample = next((item for item in self.bundle.samples if item.sample_id == sample_id), None)
        if sample is None:
            _fail("ARTIFACT_STALE")
        return sample

    def _source_identity(self, sample: ConversationSample) -> str:
        return _fp(sample.model_dump(mode="json", warnings="error"))

    def _config_identity(self) -> str:
        models = self.models_settings.model_dump(mode="json")
        if not self.models_settings.extraction_omit_max_tokens:
            models.pop("extraction_omit_max_tokens")  # Old Mock manifests remain readable.
        return _fp({"extraction": self.extraction_settings.model_dump(mode="json"),
                    "models": models,
                    "planner_prompt_reserve": self.reserved_prompt_tokens,
                    "draft_template_fingerprint": MEMORY_DRAFT_PROMPT.template_fingerprint})

    def _checked_records(self, report: MemoryBuildResult, sample: ConversationSample) -> tuple[MemoryRecord, ...]:
        sources = {(session.session_id, turn.dia_id) for session in sample.sessions for turn in session.turns}
        records: list[MemoryRecord] = []
        try:
            for supplied in report.memories:
                if not isinstance(supplied, MemoryRecord):
                    _fail("ARTIFACT_CORRUPT")
                record = MemoryRecord.model_validate(supplied.model_dump(mode="python", warnings="error"))
                validate_memory_id(record)
                if record.sample_id != sample.sample_id or record.extractor_version != report.extractor_version:
                    _fail("ARTIFACT_CORRUPT")
                if any((ref.session_id, ref.dia_id) not in sources for ref in record.source_refs):
                    _fail("ARTIFACT_CORRUPT")
                records.append(record)
            memory_set_fingerprint(records)
        except ArtifactStoreError:
            raise
        except (ValidationError, ValueError, TypeError, AttributeError):
            _fail("ARTIFACT_CORRUPT")
        return tuple(sorted(records, key=lambda item: item.memory_id))

    def publish(self, report: MemoryBuildResult) -> MemoryArtifact:
        # Preserve the original offline-only writer. Production uses a distinct
        # reviewed entry point with an independently supplied attestation.
        if not self.allow_mock_publication or self.models_settings.default_backend != "mock":
            _fail("ARTIFACT_INCOMPLETE")
        return self._publish_checked(report, backend="mock", provenance=None, review_receipt=None)

    def _publish_checked(self, report: MemoryBuildResult, *, backend: str,
                         provenance: ProductionProvenance | None,
                         review_receipt: bytes | None) -> MemoryArtifact:
        if not isinstance(report, MemoryBuildResult):
            _fail("ARTIFACT_CORRUPT")
        if report.build_status == "failed":
            _fail("MEMORY_BUILD_FAILED")
        if report.build_status != "complete" or report.partial_build:
            _fail("ARTIFACT_INCOMPLETE")
        sample = self._sample(report.sample_id)
        try:
            windows = plan_extraction_windows(sample, self.extraction_settings,
                                              reserved_prompt_tokens=self.reserved_prompt_tokens)
        except WindowPlanningError:
            _fail("ARTIFACT_INCOMPLETE")
        expected = [(window.extraction_window_id, window.source_refs) for window in windows]
        observed = [(item.extraction_window_id, item.source_refs) for item in report.window_diagnostics]
        if (observed != expected or report.total_windows != len(windows)
                or report.successful_windows != len(windows)
                or any(item.status != "success" or item.error_code is not None
                       or (backend == "mock" and item.rejected_fact_count)
                       for item in report.window_diagnostics)):
            _fail("ARTIFACT_INCOMPLETE")
        records = self._checked_records(report, sample)
        rows = [item.model_dump(mode="json", warnings="error") for item in records]
        jsonl = b"".join(_json_bytes(row) for row in rows)
        exported = _json_bytes(rows)
        build_id = "build_" + uuid.uuid4().hex
        manifest = MemoryManifest(
            build_id=build_id, sample_id=report.sample_id, created_at=datetime.now(timezone.utc),
            dataset_snapshot_sha256=self.bundle.snapshot_sha256,
            source_turns_fingerprint=self._source_identity(sample),
            extraction_config_fingerprint=self._config_identity(),
            extractor_version=report.extractor_version,
            prompt_fingerprint=MEMORY_DRAFT_PROMPT.template_fingerprint,
            backend=backend, resolved_model_id="deepseek-flash" if backend == "deepseek" else None,
            production_provenance=provenance,
            build_status="complete", partial_build=False,
            total_windows=report.total_windows, successful_windows=report.successful_windows,
            failed_windows=(), memory_count=len(records),
            memories_jsonl_sha256=_sha(jsonl), memories_json_sha256=_sha(exported),
            memory_set_fingerprint=memory_set_fingerprint(records),
        )
        self.root.mkdir(parents=True, exist_ok=True)
        builds = self.root / "builds"
        builds.mkdir(exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".staging_", dir=self.root))
        target = builds / build_id
        pointer_tmp: Path | None = None
        try:
            _write(staging / "memories.jsonl", jsonl)
            _write(staging / "memories.json", exported)
            if review_receipt is not None:
                _write(staging / "review_receipt.json", review_receipt)
            manifest_bytes = _json_bytes(manifest.model_dump(mode="json", warnings="error"))
            _write(staging / "manifest.json", manifest_bytes)
            artifact = self._load_build(staging, sample, allow_partial=False)
            os.replace(staging, target)
            pointer = {"build_id": build_id, "manifest_sha256": _sha(manifest_bytes)}
            fd, name = tempfile.mkstemp(prefix=".pointer_", dir=self.root)
            pointer_tmp = Path(name)
            with os.fdopen(fd, "wb") as stream:
                stream.write(_json_bytes(pointer))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(pointer_tmp, self.root / _POINTER)
            return artifact
        except ArtifactStoreError:
            raise
        except (OSError, ValueError, TypeError, ValidationError):
            _fail("ARTIFACT_WRITE_FAILED")
        finally:
            if staging.exists():
                shutil.rmtree(staging)
            if pointer_tmp is not None and pointer_tmp.exists():
                pointer_tmp.unlink()

    def _load_build(self, directory: Path, sample: ConversationSample,
                    *, allow_partial: bool) -> MemoryArtifact:
        try:
            manifest_bytes = (directory / "manifest.json").read_bytes()
            manifest = MemoryManifest.model_validate_json(manifest_bytes)
            if manifest.sample_id != sample.sample_id:
                _fail("ARTIFACT_STALE")
            if (manifest.dataset_snapshot_sha256 != self.bundle.snapshot_sha256
                    or manifest.source_turns_fingerprint != self._source_identity(sample)
                    or manifest.extraction_config_fingerprint != self._config_identity()
                    or manifest.prompt_fingerprint != MEMORY_DRAFT_PROMPT.template_fingerprint):
                _fail("ARTIFACT_STALE")
            if manifest.backend != self._expected_backend():
                _fail("ARTIFACT_STALE")
            if manifest.backend == "mock" and (manifest.resolved_model_id is not None or
                                               manifest.production_provenance is not None):
                _fail("ARTIFACT_STALE")
            if manifest.backend == "deepseek" and (manifest.resolved_model_id != "deepseek-flash" or
                                                   manifest.production_provenance is None or
                                                   manifest.production_provenance.thinking != "enabled" or
                                                   manifest.production_provenance.reasoning_effort != "high"):
                _fail("ARTIFACT_STALE")
            if manifest.backend == "deepseek":
                receipt_bytes = (directory / "review_receipt.json").read_bytes()
                receipt = ReviewAttestation.model_validate_json(receipt_bytes)
                provenance = manifest.production_provenance
                if ("sha256:" + _sha(receipt_bytes) != provenance.review_fingerprint or
                    receipt.run_kind != provenance.run_kind or
                    receipt.reviewer_id != provenance.reviewer_id or
                    receipt.execution_policy_fingerprint != provenance.execution_policy_fingerprint or
                    receipt.review_authority_fingerprint != provenance.review_authority_fingerprint or
                    receipt.reviewed_memory_set_fingerprint != manifest.memory_set_fingerprint or
                    len(receipt.windows) != manifest.total_windows):
                    _fail("ARTIFACT_CORRUPT")
                planned = plan_extraction_windows(sample, self.extraction_settings,
                                                   reserved_prompt_tokens=self.reserved_prompt_tokens)
                if (_fp([{"id": w.extraction_window_id,
                          "refs": [r.model_dump(mode="json") for r in w.source_refs]} for w in planned])
                        != provenance.window_plan_fingerprint or
                    tuple(item.extraction_window_id for item in receipt.windows)
                        != tuple(item.extraction_window_id for item in planned) or
                    any(item.source_refs_fingerprint != _fp([r.model_dump(mode="json") for r in window.source_refs])
                        for item, window in zip(receipt.windows, planned))):
                    _fail("ARTIFACT_STALE")
            if manifest.build_status == "failed":
                _fail("MEMORY_BUILD_FAILED")
            if manifest.build_status != "complete" and not allow_partial:
                _fail("ARTIFACT_INCOMPLETE")
            jsonl = (directory / "memories.jsonl").read_bytes()
            exported = (directory / "memories.json").read_bytes()
            if _sha(jsonl) != manifest.memories_jsonl_sha256 or _sha(exported) != manifest.memories_json_sha256:
                _fail("ARTIFACT_CORRUPT")
            rows = [json.loads(line) for line in jsonl.splitlines()]
            if json.loads(exported) != rows or len(rows) != manifest.memory_count:
                _fail("ARTIFACT_CORRUPT")
            records = tuple(MemoryRecord.model_validate(row) for row in rows)
            for record in records:
                validate_memory_id(record)
            if list(records) != sorted(records, key=lambda item: item.memory_id):
                _fail("ARTIFACT_CORRUPT")
            sources = {(session.session_id, turn.dia_id) for session in sample.sessions for turn in session.turns}
            if any((ref.session_id, ref.dia_id) not in sources for record in records for ref in record.source_refs):
                _fail("ARTIFACT_CORRUPT")
            return MemoryArtifact(manifest=manifest, memories=records)
        except ArtifactStoreError:
            raise
        except (OSError, ValidationError, ValueError, TypeError, AttributeError, RecursionError, UnicodeError, WindowPlanningError):
            _fail("ARTIFACT_CORRUPT")

    def load(self, *, allow_partial: bool = False) -> MemoryArtifact:
        try:
            pointer = json.loads((self.root / _POINTER).read_bytes())
            if not isinstance(pointer, dict) or set(pointer) != {"build_id", "manifest_sha256"}:
                _fail("ARTIFACT_CORRUPT")
            build_id, digest = pointer["build_id"], pointer["manifest_sha256"]
            if not isinstance(build_id, str) or not _BUILD_ID.fullmatch(build_id):
                _fail("ARTIFACT_CORRUPT")
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                _fail("ARTIFACT_CORRUPT")
            directory = self.root / "builds" / build_id
            manifest_bytes = (directory / "manifest.json").read_bytes()
            if _sha(manifest_bytes) != digest:
                _fail("ARTIFACT_CORRUPT")
            manifest = MemoryManifest.model_validate_json(manifest_bytes)
            if manifest.build_id != build_id:
                _fail("ARTIFACT_CORRUPT")
            return self._load_build(directory, self._sample(manifest.sample_id), allow_partial=allow_partial)
        except ArtifactStoreError:
            raise
        except FileNotFoundError:
            _fail("BUILD_NOT_FOUND" if not (self.root / _POINTER).exists() else "ARTIFACT_CORRUPT")
        except (OSError, ValidationError, ValueError, TypeError, RecursionError, UnicodeError):
            _fail("ARTIFACT_CORRUPT")

    def import_memories(self) -> list[MemoryRecord]:
        artifact = self.load()
        artifact.require_complete_for_index()
        return list(artifact.memories)

    def _expected_backend(self) -> str:
        return "mock"


class ReviewedMemoryArtifactStore(MemoryArtifactStore):
    """Strict opt-in non-Mock writer/reader; legacy Mock path is unchanged."""

    def _expected_backend(self) -> str:
        return "deepseek"

    def publish_reviewed(self, report: MemoryBuildResult,
                         attestation: ReviewAttestation,
                         *, verified_review: VerifiedReview | None = None) -> MemoryArtifact:
        if not isinstance(report, MemoryBuildResult) or not isinstance(attestation, ReviewAttestation):
            _fail("REVIEW_REQUIRED")
        try:
            receipt = ReviewAttestation.model_validate(attestation.model_dump(mode="python"))
            models = self.models_settings
            if (models.model_id != "deepseek-flash" or models.thinking != "enabled" or
                models.reasoning_effort != "high" or models.max_retries != 0 or
                models.max_concurrent_api_requests != 1 or models.api_enabled_by_default or
                models.default_backend != "mock"):
                _fail("ARTIFACT_STALE")
            if report.build_status != "complete" or report.partial_build:
                _fail("ARTIFACT_INCOMPLETE")
            if receipt.reviewed_memory_set_fingerprint != memory_set_fingerprint(report.memories):
                _fail("REVIEW_MISMATCH")
            if len(receipt.windows) != report.total_windows:
                _fail("REVIEW_MISMATCH")
            for reviewed, diagnostic in zip(receipt.windows, report.window_diagnostics):
                if (reviewed.extraction_window_id != diagnostic.extraction_window_id or
                    reviewed.source_refs_fingerprint != _fp([r.model_dump(mode="json") for r in diagnostic.source_refs]) or
                    reviewed.approved_fact_count != diagnostic.accepted_fact_count or
                    reviewed.excluded_fact_count != diagnostic.rejected_fact_count or
                    reviewed.decision != "approved"):
                    _fail("REVIEW_MISMATCH")
            if receipt.run_kind == "real":
                if (self.bundle.snapshot_sha256 != _OFFICIAL_LOCOMO_SHA or
                    self.bundle.split_by_sample_id.get(report.sample_id) != "main_dev" or
                    receipt.reviewer_id == "offline-test"):
                    _fail("REVIEW_MISMATCH")
                if (type(verified_review) is not VerifiedReview or
                    verified_review.authority.allowed_run_kind != "real" or
                    verified_review.queue.sample_id != report.sample_id or
                    verified_review.queue.run_fingerprint != verified_review.approval.run_fingerprint or
                    receipt.reviewer_id != verified_review.approval.reviewer_id or
                    receipt.review_log_fingerprint != verified_review.approval_fingerprint or
                    receipt.review_authority_fingerprint != _review_fp(
                        verified_review.authority.model_dump(mode="json")) or
                    tuple(w.window_id for w in verified_review.queue.windows) !=
                        tuple(d.extraction_window_id for d in report.window_diagnostics) or
                    any(len(w.facts) != d.input_fact_count for w, d in zip(
                        verified_review.queue.windows, report.window_diagnostics))):
                    _fail("REVIEW_MISMATCH")
                sample = self._sample(report.sample_id)
                planned = plan_extraction_windows(sample, self.extraction_settings,
                                                  reserved_prompt_tokens=self.reserved_prompt_tokens)
                if len(planned) != len(verified_review.queue.windows):
                    _fail("REVIEW_MISMATCH")
                repository = SourceRepository(self.bundle)
                rebuilt = []
                for window, queued in zip(planned, verified_review.queue.windows):
                    if (queued.window_id != window.extraction_window_id or
                        queued.source_refs_fingerprint != _fp([r.model_dump(mode="json")
                                                                for r in window.source_refs])):
                        _fail("REVIEW_MISMATCH")
                    allowed = {(r.session_id, r.dia_id) for r in window.source_refs}
                    for fact in queued.facts:
                        if (not {(r.session_id, r.dia_id) for r in fact.draft.source_refs}.issubset(allowed) or
                            fact.cited_turns != tuple(repository.get_turn(report.sample_id, ref)
                                                     for ref in sorted(fact.draft.source_refs,
                                                        key=lambda item: (item.session_id, item.dia_id)))):
                            _fail("REVIEW_MISMATCH")
                    result = WindowExtractionResult(report.sample_id, window.extraction_window_id,
                        tuple(item.draft for item in queued.facts),
                        MEMORY_DRAFT_PROMPT.template_fingerprint, 0, "review")
                    selected = verified_review.filter_for_window(window.extraction_window_id)(result)
                    rebuilt.extend(normalize_memories(report.sample_id, [selected], repository,
                        extractor_version=report.extractor_version,
                        evidence_checker=verified_review.checker_for_window(window.extraction_window_id)))
                if memory_set_fingerprint(merge_reviewed_memories(rebuilt)) != memory_set_fingerprint(report.memories):
                    _fail("REVIEW_MISMATCH")
            elif not report.sample_id.startswith("synthetic"):
                _fail("REVIEW_MISMATCH")
            sample = self._sample(report.sample_id)
            windows = plan_extraction_windows(sample, self.extraction_settings,
                                               reserved_prompt_tokens=self.reserved_prompt_tokens)
            plan_fp = _fp([{"id": w.extraction_window_id,
                            "refs": [r.model_dump(mode="json") for r in w.source_refs]} for w in windows])
            provenance = ProductionProvenance(
                run_kind=receipt.run_kind, thinking="enabled", reasoning_effort="high",
                execution_policy_fingerprint=receipt.execution_policy_fingerprint,
                window_plan_fingerprint=plan_fp,
                review_fingerprint=_fp(receipt.model_dump(mode="json")),
                reviewer_id=receipt.reviewer_id, review_status="approved",
                review_authority_fingerprint=receipt.review_authority_fingerprint,
            )
        except ArtifactStoreError:
            raise
        except (ValidationError, ValueError, TypeError, AttributeError, WindowPlanningError):
            _fail("REVIEW_MISMATCH")
        return self._publish_checked(report, backend="deepseek", provenance=provenance,
                                     review_receipt=_json_bytes(receipt.model_dump(mode="json")))
