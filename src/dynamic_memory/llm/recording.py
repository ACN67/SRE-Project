"""Versioned single-process call recording and opt-in SQLite replay."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import tempfile
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from pydantic import ValidationError

from .execution import GuardedLLMAdapter, LLMCallSummary, LLMExecutionError
from .mock import request_fingerprint
from .protocol import LLMRequest, LLMResponse, validate_response_for_request
from .structured_output import StructuredOutputError

_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*\Z")
_HASH = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GIT_SHA = re.compile(r"[0-9a-f]{40}\Z")
_FORMAT_VERSION = 1


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _fingerprint(request: LLMRequest) -> str:
    checked = LLMRequest.model_validate(request.model_dump(mode="python", warnings=False))
    return request_fingerprint(checked)


@dataclass(frozen=True, slots=True)
class CallIdentity:
    sample_id: str
    task: str
    scope_id: str
    method: str | None
    run_kind: str
    backend: str
    resolved_model_id: str | None
    data_fingerprint: str
    template_id: str
    template_version: str
    template_fingerprint: str
    schema_id: str | None
    schema_version: str | None
    config_fingerprint: str
    execution_policy_fingerprint: str
    retriever_fingerprint: str | None
    query_analysis_version: str | None
    git_sha: str

    def __post_init__(self) -> None:
        for name in ("sample_id", "scope_id", "template_id", "template_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _LABEL.fullmatch(value):
                raise ValueError("call identity requires nonblank safe identifiers")
        if self.task not in ("memory_extraction", "state_reasoning", "qa"):
            raise ValueError("call identity has invalid task")
        if self.run_kind not in ("development", "interactive", "evaluation"):
            raise ValueError("call identity has invalid run kind")
        if self.backend not in ("mock", "deepseek"):
            raise ValueError("call identity has invalid backend")
        if self.backend == "deepseek":
            if not isinstance(self.resolved_model_id, str) or not _LABEL.fullmatch(self.resolved_model_id):
                raise ValueError("real backend requires a resolved model ID")
        elif self.resolved_model_id is not None:
            raise ValueError("mock backend must use null resolved model ID")
        if self.task == "qa":
            if self.method not in ("full_text", "vector_rag", "dynamic"):
                raise ValueError("QA requires an explicit method")
        elif self.method is not None:
            raise ValueError("non-QA task requires null method")
        for name in ("data_fingerprint", "template_fingerprint", "config_fingerprint", "execution_policy_fingerprint"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _HASH.fullmatch(value):
                raise ValueError("call identity requires sha256 component fingerprints")
        if self.task in ("state_reasoning", "qa") and (self.task != "qa" or self.method != "full_text"):
            if not isinstance(self.retriever_fingerprint, str) or not _HASH.fullmatch(self.retriever_fingerprint):
                raise ValueError("retrieval-dependent call requires retriever fingerprint")
        elif self.retriever_fingerprint is not None:
            raise ValueError("non-retrieval call requires null retriever fingerprint")
        if self.task in ("state_reasoning", "qa"):
            if not isinstance(self.query_analysis_version, str) or not _LABEL.fullmatch(self.query_analysis_version):
                raise ValueError("query call requires analysis version")
        elif self.query_analysis_version is not None:
            raise ValueError("extraction requires null analysis version")
        if self.task != "qa" and (self.schema_id is None or self.schema_version is None):
            raise ValueError("JSON task requires schema identity and version")
        if (self.schema_id is None) != (self.schema_version is None):
            raise ValueError("schema ID and version must occur together")
        for value in (self.schema_id, self.schema_version):
            if value is not None and (not isinstance(value, str) or not _LABEL.fullmatch(value)):
                raise ValueError("schema identity must be a safe nonblank token")
        if not isinstance(self.git_sha, str) or not _GIT_SHA.fullmatch(self.git_sha):
            raise ValueError("git_sha must be a 40-character lowercase hex commit ID")


def call_cache_key(request: LLMRequest, identity: CallIdentity) -> str:
    if not isinstance(identity, CallIdentity) or not isinstance(request, LLMRequest):
        raise ValueError("expected CallIdentity and LLMRequest")
    if identity.task != request.task:
        raise ValueError("identity task differs from request")
    if identity.backend == "deepseek" and request.model_id is not None and identity.resolved_model_id != request.model_id:
        raise ValueError("resolved model differs from request")
    try:
        fingerprint = _fingerprint(request)
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise ValueError("request fingerprint validation failed") from None
    return _hash({"format_version": _FORMAT_VERSION, "identity": asdict(identity), "request_fingerprint": fingerprint})


class RecordingError(RuntimeError):
    def __init__(self, code: str, summary: LLMCallSummary | None = None) -> None:
        self.code = code
        self.summary = summary
        super().__init__(f"{code}: call recording failed")


class _StoreFailure(Exception):
    def __init__(self, code: str) -> None:
        self.code = code


class MemoryRecordingStore:
    """Default store: isolated to this object and never written to disk."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._cache: dict[str, str] = {}
        self._events: list[str] = []

    def read(self, key: str) -> str | None:
        with self._lock:
            return self._cache.get(key)

    def append(self, event: str) -> None:
        with self._lock:
            self._events.append(event)

    def save_success(self, key: str, blob: str, event: str) -> None:
        with self._lock:
            self._cache[key] = blob
            self._events.append(event)

    def events(self) -> tuple[dict, ...]:
        with self._lock:
            return tuple(json.loads(event) for event in self._events)


class SQLiteRecordingStore:
    """Opt-in local transactional store under ignored results/ or OS temp."""

    def __init__(self, path: str | Path) -> None:
        if not isinstance(path, (str, Path)):
            raise ValueError("SQLite cache path must be explicit")
        target = Path(path).resolve()
        results_root = Path(__file__).resolve().parents[3] / "results"
        temp_root = Path(tempfile.gettempdir()).resolve()
        if target.suffix != ".sqlite3" or not (target.is_relative_to(results_root) or target.is_relative_to(temp_root)):
            raise ValueError("SQLite cache path must be under ignored results/ or OS temp")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise RecordingError("STORE_INIT_FAILED") from None
        self.path = target
        try:
            with self._connect() as connection:
                connection.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, blob TEXT NOT NULL)")
                connection.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL)")
        except sqlite3.Error:
            raise RecordingError("STORE_INIT_FAILED") from None

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def read(self, key: str) -> str | None:
        try:
            with self._connect() as connection:
                row = connection.execute("SELECT blob FROM cache WHERE key = ?", (key,)).fetchone()
                return None if row is None else row[0]
        except sqlite3.Error:
            raise _StoreFailure("CACHE_READ_FAILED") from None

    def append(self, event: str) -> None:
        try:
            with self._connect() as connection:
                connection.execute("INSERT INTO events(event) VALUES (?)", (event,))
        except sqlite3.Error:
            raise _StoreFailure("LOG_WRITE_FAILED") from None

    def save_success(self, key: str, blob: str, event: str) -> None:
        try:
            with self._connect() as connection:
                connection.execute("INSERT OR REPLACE INTO cache(key, blob) VALUES (?, ?)", (key, blob))
                connection.execute("INSERT INTO events(event) VALUES (?)", (event,))
        except sqlite3.Error:
            raise _StoreFailure("STORE_WRITE_FAILED") from None

    def events(self) -> tuple[dict, ...]:
        try:
            with self._connect() as connection:
                rows = connection.execute("SELECT event FROM events ORDER BY id").fetchall()
            return tuple(json.loads(row[0]) for row in rows)
        except (sqlite3.Error, ValueError):
            raise _StoreFailure("LOG_READ_FAILED") from None


@dataclass(frozen=True, slots=True)
class RecordedCallResult:
    response: LLMResponse = field(repr=False)
    summary: LLMCallSummary
    cache_status: str
    cache_key: str
    source_summary: LLMCallSummary | None = None


def _empty_summary() -> LLMCallSummary:
    return LLMCallSummary(0, 0, (), None, False, None, None, 0)


class RecordedLLMExecutor:
    """One logical call per invocation; per-key single-flight when caching."""

    def __init__(
        self,
        executor: GuardedLLMAdapter,
        *,
        store: MemoryRecordingStore | SQLiteRecordingStore | None = None,
        cache_enabled: bool = True,
    ) -> None:
        if not isinstance(executor, GuardedLLMAdapter) or not isinstance(cache_enabled, bool):
            raise ValueError("expected a GuardedLLMAdapter and bool cache_enabled")
        self._executor = executor
        self.store = store if store is not None else MemoryRecordingStore()
        if not all(callable(getattr(self.store, name, None)) for name in ("read", "append", "save_success")):
            raise ValueError("store must support read, append and save_success")
        self._cache_enabled = cache_enabled
        self._registry_lock = Lock()
        self._key_locks: dict[str, tuple[Lock, int]] = {}

    def _lock_for(self, key: str) -> Lock:
        with self._registry_lock:
            lock, count = self._key_locks.get(key, (Lock(), 0))
            self._key_locks[key] = (lock, count + 1)
            return lock

    def _unlock_for(self, key: str, lock: Lock) -> None:
        lock.release()
        with self._registry_lock:
            _, count = self._key_locks[key]
            if count == 1:
                del self._key_locks[key]
            else:
                self._key_locks[key] = (lock, count - 1)

    def execute(
        self,
        request: LLMRequest,
        identity: CallIdentity,
        *,
        validator: Callable[[LLMRequest, LLMResponse], object] | None = None,
    ) -> RecordedCallResult:
        started = time.monotonic()
        call_id = uuid.uuid4().hex
        timestamp = datetime.now(timezone.utc).isoformat()
        try:
            key = call_cache_key(request, identity)
            fingerprint = _fingerprint(request)
        except (ValueError, TypeError, ValidationError, UnicodeError):
            raise RecordingError("INVALID_IDENTITY", _empty_summary()) from None
        if validator is not None and not callable(validator):
            raise RecordingError("INVALID_VALIDATOR", _empty_summary()) from None
        if validator is not None and identity.schema_id is None:
            raise RecordingError("INVALID_IDENTITY", _empty_summary()) from None
        cacheable = self._cache_enabled and (request.output_mode == "text" or validator is not None)
        lock = self._lock_for(key) if cacheable else None
        if lock is not None:
            lock.acquire()
        try:
            return self._execute_locked(request, identity, key, fingerprint, validator, cacheable, started, call_id, timestamp)
        finally:
            if lock is not None:
                self._unlock_for(key, lock)

    def _execute_locked(self, request, identity, key, fingerprint, validator, cacheable, started, call_id, timestamp):
        def event(status, cache_status, summary, error_code=None):
            return _canonical({
                "call_id": call_id, "timestamp": timestamp,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                "identity": asdict(identity), "request_fingerprint": fingerprint,
                "cache_key": key, "cache_status": cache_status, "status": status,
                "error_code": error_code, "summary": asdict(summary), "estimated_cost": None,
            })

        def append(status, cache_status, summary, error_code=None):
            try:
                self.store.append(event(status, cache_status, summary, error_code))
            except Exception:
                raise RecordingError("LOG_WRITE_FAILED", summary) from None

        if cacheable:
            try:
                blob = self.store.read(key)
            except Exception:
                append("error", "corrupt", _empty_summary(), "CACHE_READ_FAILED")
                raise RecordingError("CACHE_READ_FAILED", _empty_summary()) from None
            if blob is not None:
                try:
                    package = json.loads(blob)
                    checksum = package.pop("checksum")
                    if checksum != _hash(package) or package["format_version"] != _FORMAT_VERSION or package["key"] != key or package["identity"] != asdict(identity) or package["request_fingerprint"] != fingerprint:
                        raise ValueError("invalid cache package")
                    response = LLMResponse.model_validate(package["response"])
                    _check_response(request, identity, response, validator)
                    source = package["source_summary"]
                    source["failure_codes"] = tuple(source["failure_codes"])
                    source_summary = LLMCallSummary(**source)
                except Exception:
                    append("error", "corrupt", _empty_summary(), "CACHE_CORRUPT")
                    raise RecordingError("CACHE_CORRUPT", _empty_summary()) from None
                zero = _empty_summary()
                append("success", "hit", zero)
                return RecordedCallResult(response, zero, "hit", key, source_summary)

        try:
            executed = self._executor.execute(request)
        except LLMExecutionError as exc:
            append("error", "miss" if cacheable else "skip", exc.summary, exc.code)
            raise
        try:
            _check_response(request, identity, executed.response, validator)
        except StructuredOutputError as exc:
            append("error", "miss" if cacheable else "skip", executed.summary, exc.code)
            raise RecordingError(exc.code, executed.summary) from None
        except Exception:
            append("error", "miss" if cacheable else "skip", executed.summary, "VALIDATION_FAILED")
            raise RecordingError("VALIDATION_FAILED", executed.summary) from None

        status = "miss" if cacheable else "skip"
        if cacheable:
            package = {
                "format_version": _FORMAT_VERSION, "key": key,
                "identity": asdict(identity), "request_fingerprint": fingerprint,
                "response": executed.response.model_dump(mode="json"),
                "source_summary": asdict(executed.summary),
            }
            package["checksum"] = _hash(package)
            try:
                self.store.save_success(key, _canonical(package), event("success", status, executed.summary))
            except Exception:
                raise RecordingError("STORE_WRITE_FAILED", executed.summary) from None
        else:
            append("success", status, executed.summary)
        return RecordedCallResult(executed.response, executed.summary, status, key)


def _check_response(request, identity, response, validator):
    if not isinstance(response, LLMResponse):
        raise ValueError("response type is invalid")
    checked = LLMResponse.model_validate(response.model_dump(mode="python", warnings=False))
    validate_response_for_request(request, checked)
    if identity.backend == "deepseek" and checked.model_id != identity.resolved_model_id:
        raise ValueError("response model differs from resolved backend model")
    if identity.backend == "mock" and checked.model_id is not None:
        raise ValueError("mock identity differs from response model")
    if checked.finish_reason is not None and checked.finish_reason != "stop":
        raise ValueError("response is incomplete")
    if validator is not None:
        validator(request, checked)
