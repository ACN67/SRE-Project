"""Read-only IC-012 service over explicit B/C/A ports; no Gold access."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
import re
from typing import Literal

from pydantic import BaseModel

from dynamic_memory.contracts import (AnswerResult, BuildStatus, ErrorInfo,
    MemoryArtifact, MemoryRecord, QueryRequest, QueryResponse, SourceRef, SourceTurn,
    validate_response_context)
from dynamic_memory.data.repository import SourceRepository, SourceRepositoryError
from dynamic_memory.pipeline.query_runner import QueryOrchestrator


class ServiceError(ValueError):
    """Stable content-free diagnostic for the UI and composition root."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(f"{code}: read-only service unavailable")


@dataclass(frozen=True, slots=True)
class VerifiedMemoryLoad:
    """Attestation from a trusted injected loader, not a disk-proof by itself.

    Formal composition MUST supply this from B's strict loader after checking
    published build, byte hashes, source turns and snapshot. A rechecks the
    in-memory public DTO; only synthetic fixtures are exercised in A-18.
    """

    artifact: MemoryArtifact
    sample_id: str
    dataset_snapshot_sha256: str
    source_manifest_fingerprint: str
    provenance: Literal["synthetic_fixture", "b_verified_loader"]
    verification_id: str


@dataclass(frozen=True, slots=True)
class IndexReadiness:
    sample_id: str
    dataset_snapshot_sha256: str
    source_manifest_fingerprint: str
    state: Literal["ready", "not_found", "stale", "corrupt"]
    index_fingerprint: str | None = None


_SOURCE_CODES = frozenset({"DATA_NOT_LOADED", "DATA_FORMAT_INVALID", "SAMPLE_NOT_FOUND",
                           "SOURCE_NOT_FOUND", "SAMPLE_MISMATCH"})
_MEMORY_CODES = {
    "not_built": "BUILD_NOT_FOUND", "partial": "ARTIFACT_INCOMPLETE",
    "failed": "MEMORY_BUILD_FAILED", "stale": "ARTIFACT_STALE",
    "corrupt": "ARTIFACT_CORRUPT",
}
_INDEX_CODES = {"not_found": "INDEX_NOT_FOUND", "stale": "INDEX_STALE",
                "corrupt": "INDEX_STALE"}
_MEMORY_ID = re.compile(r"mem_[0-9a-f]{20}\Z")


def _public_fields_only(value: object) -> None:
    if isinstance(value, BaseModel):
        if set(value.__dict__) != set(type(value).model_fields) or value.__pydantic_extra__:
            raise ServiceError("ARTIFACT_CORRUPT")
        for child in value.__dict__.values():
            _public_fields_only(child)
    elif isinstance(value, (tuple, list)):
        for child in value:
            _public_fields_only(child)
    elif isinstance(value, dict):
        for child in value.values():
            _public_fields_only(child)


class DynamicMemoryService:
    """One initialized instance for E browsing and offline query execution.

    No port is invented at runtime. Build/index attestation is a trust boundary:
    only the configured B/C loaders can establish disk and index provenance.
    """

    def __init__(self, *, sources: SourceRepository,
                 build_status: Callable[[str], BuildStatus] | None = None,
                 load_verified_artifact: Callable[[str], VerifiedMemoryLoad] | None = None,
                 query_orchestrator: QueryOrchestrator | None = None,
                 index_readiness: Callable[[str, str], IndexReadiness] | None = None,
                 dataset_state: Callable[[], str] | None = None) -> None:
        if type(sources) is not SourceRepository:
            raise ServiceError("SOURCE_PORT_INVALID")
        if query_orchestrator is not None and type(query_orchestrator) is not QueryOrchestrator:
            raise ServiceError("QUERY_PORT_INVALID")
        for port in (build_status, load_verified_artifact, index_readiness, dataset_state):
            if port is not None and not callable(port):
                raise ServiceError("SERVICE_PORT_INVALID")
        self._sources = sources
        self._build_status = build_status
        self._load_verified_artifact = load_verified_artifact
        self._query_orchestrator = (query_orchestrator.with_source_lookup(sources.get_sample)
                                    if query_orchestrator is not None else None)
        self._index_readiness = index_readiness
        self._dataset_state_port = dataset_state

    def _source_call(self, fn, *args):
        try:
            return fn(*args)
        except SourceRepositoryError as exc:
            raise ServiceError(exc.code if exc.code in _SOURCE_CODES else "SOURCE_FAILED") from None
        except Exception:
            raise ServiceError("SOURCE_FAILED") from None

    def _dataset_state(self) -> str:
        if self._dataset_state_port is None:
            try:
                self._sources.snapshot_sha256
                return "ready"
            except SourceRepositoryError as exc:
                if exc.code == "DATA_NOT_LOADED":
                    return "unloaded"
                raise ServiceError("SOURCE_FAILED") from None
        try:
            state = self._dataset_state_port()
            if state not in ("ready", "unloaded", "load_error"):
                raise ValueError
            loaded = True
            try:
                self._sources.snapshot_sha256
            except SourceRepositoryError as exc:
                if exc.code != "DATA_NOT_LOADED":
                    raise ValueError
                loaded = False
            if (state == "ready") != loaded:
                raise ValueError
            return state
        except Exception:
            raise ServiceError("DATASET_STATE_INVALID") from None

    def _ready(self) -> str:
        state = self._dataset_state()
        if state != "ready":
            raise ServiceError("DATA_LOAD_FAILED" if state == "load_error" else "DATA_NOT_LOADED")
        return self._source_call(lambda: self._sources.snapshot_sha256)

    def list_samples(self) -> list[str]:
        self._ready()
        return list(self._source_call(self._sources.list_samples))

    def get_turn(self, sample_id: str, ref: SourceRef) -> SourceTurn:
        self._ready()
        return deepcopy(self._source_call(self._sources.get_turn, sample_id, ref))

    def _status_and_artifact(self, sample_id: str) -> tuple[BuildStatus, MemoryArtifact | None]:
        state = self._dataset_state()
        if not isinstance(sample_id, str) or not sample_id.strip():
            raise ServiceError("DATA_FORMAT_INVALID")
        if state == "ready":
            self._source_call(self._sources.get_sample, sample_id)
        if self._build_status is None:
            raise ServiceError("BUILD_STATUS_UNAVAILABLE")
        try:
            raw = self._build_status(sample_id)
            if type(raw) is not BuildStatus:
                raise ValueError
            _public_fields_only(raw)
            status = BuildStatus.model_validate(raw.model_dump(mode="python"), strict=True)
        except Exception:
            raise ServiceError("BUILD_STATUS_INVALID") from None
        if status.dataset_state != state:
            raise ServiceError("BUILD_STATUS_INVALID")
        if state != "ready" or status.memory_state != "complete":
            return status, None
        return status, self._artifact(sample_id, status)

    def _artifact(self, sample_id: str, status: BuildStatus) -> MemoryArtifact:
        if self._load_verified_artifact is None:
            raise ServiceError("ARTIFACT_LOADER_UNAVAILABLE")
        try:
            loaded = self._load_verified_artifact(sample_id)
            if (type(loaded) is not VerifiedMemoryLoad or
                    loaded.provenance not in ("synthetic_fixture", "b_verified_loader") or
                    not isinstance(loaded.verification_id, str) or not loaded.verification_id.strip() or
                    type(loaded.artifact) is not MemoryArtifact):
                raise ServiceError("ARTIFACT_UNVERIFIED")
            _public_fields_only(loaded.artifact)
            artifact = MemoryArtifact.model_validate(loaded.artifact.model_dump(mode="python"), strict=True)
            artifact.require_complete_for_index()
            snapshot = self._ready()
            if (loaded.sample_id != sample_id or artifact.manifest.sample_id != sample_id or
                    loaded.dataset_snapshot_sha256 != snapshot or
                    artifact.manifest.dataset_snapshot_sha256 != snapshot):
                raise ServiceError("ARTIFACT_STALE")
            if (loaded.source_manifest_fingerprint != artifact.source_manifest_fingerprint or
                    artifact.manifest.build_id != status.build_id):
                raise ServiceError("ARTIFACT_CORRUPT")
            for memory in artifact.memories:
                for ref in memory.source_refs:
                    turn = self._source_call(self._sources.get_turn, sample_id, ref)
                    if (turn.sample_id, turn.session_id, turn.dia_id) != (sample_id, ref.session_id, ref.dia_id):
                        raise ServiceError("ARTIFACT_CORRUPT")
            return artifact
        except ServiceError as exc:
            if exc.code in ("ARTIFACT_STALE", "ARTIFACT_LOADER_UNAVAILABLE", "ARTIFACT_UNVERIFIED"):
                raise
            raise ServiceError("ARTIFACT_CORRUPT") from None
        except Exception:
            raise ServiceError("ARTIFACT_CORRUPT") from None

    def get_build_status(self, sample_id: str) -> BuildStatus:
        status, _artifact = self._status_and_artifact(sample_id)
        return status.model_copy(deep=True)

    def list_memories(self, sample_id: str) -> list[MemoryRecord]:
        self._ready()
        status, artifact = self._status_and_artifact(sample_id)
        if status.dataset_state != "ready":
            raise ServiceError("DATA_LOAD_FAILED" if status.dataset_state == "load_error" else "DATA_NOT_LOADED")
        if status.memory_state != "complete":
            raise ServiceError(_MEMORY_CODES[status.memory_state])
        assert artifact is not None
        return [deepcopy(record) for record in artifact.memories]

    def get_memory(self, sample_id: str, memory_id: str) -> MemoryRecord:
        records = self.list_memories(sample_id)
        if not isinstance(memory_id, str) or not _MEMORY_ID.fullmatch(memory_id):
            raise ServiceError("MEMORY_ID_INVALID")
        for record in records:
            if record.memory_id == memory_id:
                return record
        raise ServiceError("MEMORY_NOT_FOUND")

    def _index_ready(self, sample_id: str, artifact: MemoryArtifact) -> None:
        if self._index_readiness is None:
            raise ServiceError("INDEX_NOT_FOUND")
        try:
            info = self._index_readiness(sample_id, artifact.source_manifest_fingerprint)
            if type(info) is not IndexReadiness or info.state not in ("ready", "not_found", "stale", "corrupt"):
                raise ValueError
            if (info.sample_id != sample_id or info.dataset_snapshot_sha256 != self._ready() or
                    info.source_manifest_fingerprint != artifact.source_manifest_fingerprint):
                raise ServiceError("INDEX_STALE")
            if info.state != "ready":
                raise ServiceError(_INDEX_CODES[info.state])
            if not isinstance(info.index_fingerprint, str) or not info.index_fingerprint.strip():
                raise ServiceError("INDEX_STALE")
        except ServiceError:
            raise
        except Exception:
            raise ServiceError("INDEX_STALE") from None

    @staticmethod
    def _error(request: QueryRequest, method: str, code: str) -> QueryResponse:
        return QueryResponse(query_id=request.query_id, sample_id=request.sample_id,
            question=request.question, method=method, query_analysis=None,
            retrieved=None, resolution=None,
            answer_result=AnswerResult(status="error", error=ErrorInfo(
                code=code, message="Required service component is unavailable.")))

    def _checked_response(self, request: QueryRequest, method: str,
                          response: QueryResponse, artifact: MemoryArtifact | None,
                          *, early_stop: bool) -> QueryResponse:
        _public_fields_only(response)
        result = QueryResponse.model_validate(response.model_dump(mode="python"), strict=True)
        if (result.query_id, result.sample_id, result.question, result.method) != (
                request.query_id, request.sample_id, request.question, method):
            raise ValueError("response identity mismatch")
        if early_stop:
            if (result.retrieved is not None or result.resolution is not None or
                    result.answer_result.status not in ("error", "time_uncertain")):
                raise ValueError("invalid early-stop response")
            validate_response_context(result, [], [])
        else:
            sample = self._source_call(self._sources.get_sample, request.sample_id)
            turns = [turn for session in sample.sessions for turn in session.turns]
            validate_response_context(result, list(artifact.memories) if artifact is not None else [], turns)
        return QueryResponse.model_validate_json(result.model_dump_json())

    def run_query(self, request: QueryRequest, method: str = "dynamic") -> QueryResponse:
        if method not in ("full_text", "vector_rag", "dynamic"):
            raise ServiceError("INVALID_METHOD")
        if type(request) is not QueryRequest:
            raise ServiceError("QUERY_REQUEST_INVALID")
        try:
            _public_fields_only(request)
            checked = QueryRequest.model_validate(request.model_dump(mode="python"), strict=True)
        except Exception:
            raise ServiceError("QUERY_REQUEST_INVALID") from None
        if self._query_orchestrator is None:
            return self._error(checked, method, "COMPONENT_UNAVAILABLE")
        try:
            prepared = self._query_orchestrator.prepare_query(checked)
            if prepared.must_early_stop:
                response = self._query_orchestrator.run_prepared(prepared, method)
                return self._checked_response(checked, method, response, None, early_stop=True)
        except Exception:
            return self._error(checked, method, "QUERY_EXECUTION_FAILED")

        artifact = None
        if method == "full_text":
            try:
                self._ready()
                self._source_call(self._sources.get_sample, checked.sample_id)
            except ServiceError as exc:
                return self._error(checked, method, exc.code)
        else:
            try:
                status, artifact = self._status_and_artifact(checked.sample_id)
                if status.dataset_state != "ready":
                    raise ServiceError("DATA_LOAD_FAILED" if status.dataset_state == "load_error" else "DATA_NOT_LOADED")
                if status.memory_state != "complete":
                    raise ServiceError(_MEMORY_CODES[status.memory_state])
                assert artifact is not None
                self._index_ready(checked.sample_id, artifact)
            except ServiceError as exc:
                return self._error(checked, method, exc.code)
        try:
            response = self._query_orchestrator.run_prepared(prepared, method)
            return self._checked_response(checked, method, response, artifact, early_stop=False)
        except Exception:
            return self._error(checked, method, "QUERY_EXECUTION_FAILED")
