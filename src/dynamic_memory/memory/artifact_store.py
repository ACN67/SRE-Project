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

from pydantic import ValidationError

from dynamic_memory.contracts import (
    ConversationSample, DatasetBundle, MemoryArtifact, MemoryManifest, MemoryRecord,
    SourceRef, memory_set_fingerprint,
)
from dynamic_memory.contracts.memory_id import validate_memory_id
from dynamic_memory.memory.build import MemoryBuildResult
from dynamic_memory.memory.extraction import MEMORY_DRAFT_PROMPT
from dynamic_memory.memory.windows import WindowPlanningError, plan_extraction_windows
from dynamic_memory.pipeline.settings import ExtractionSettings, ModelsSettings

_BUILD_ID = re.compile(r"build_[0-9a-f]{32}\Z")
_POINTER = "current.json"


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
        return _fp({"extraction": self.extraction_settings.model_dump(mode="json"),
                    "models": self.models_settings.model_dump(mode="json"),
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
        if not isinstance(report, MemoryBuildResult):
            _fail("ARTIFACT_CORRUPT")
        if report.build_status == "failed":
            _fail("MEMORY_BUILD_FAILED")
        if report.build_status != "complete" or report.partial_build:
            _fail("ARTIFACT_INCOMPLETE")
        # B-07's local result is an offline-only integration seam.
        if not self.allow_mock_publication or self.models_settings.default_backend != "mock":
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
                       or item.rejected_fact_count for item in report.window_diagnostics)):
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
            backend="mock", resolved_model_id=None,
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
            if manifest.backend != "mock" or manifest.resolved_model_id is not None:
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
        except (OSError, ValidationError, ValueError, TypeError, AttributeError, RecursionError, UnicodeError):
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
