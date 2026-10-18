"""Explicit, reusable composition of the offline-first LLM infrastructure."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from dynamic_memory.llm.deepseek import DeepSeekLLMAdapter
from dynamic_memory.llm.execution import GuardedLLMAdapter, TokenEstimator
from dynamic_memory.llm.mock import MockLLMAdapter
from dynamic_memory.llm.prompts import PromptTemplate, RenderedPrompt, build_prompt_request, render_prompt
from dynamic_memory.llm.protocol import LLMAdapter, LLMRequest, LLMResponse
from dynamic_memory.llm.recording import (
    CallIdentity, MemoryRecordingStore, RecordedCallResult, RecordedLLMExecutor,
    SQLiteRecordingStore,
)

from .settings import AppSettings, load_settings

_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*\Z")


class RuntimeConfigurationError(ValueError):
    """A sanitized construction or identity error with a stable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"{code}: invalid LLM runtime configuration")


def _hash(value: object) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PreparedCall:
    rendered: RenderedPrompt = field(repr=False)
    request: LLMRequest = field(repr=False)
    variables: tuple[tuple[str, str], ...] = field(repr=False)


class LLMRuntime:
    """Own one adapter, guard and recorder for the lifetime of an upper layer."""

    __slots__ = ("_settings", "_backend", "_model_id", "_adapter", "_guarded", "_recorder", "_config_fp", "_policy_fp")

    def __init__(self, *, settings: AppSettings, backend: str, model_id: str | None,
                 adapter: LLMAdapter, guarded: GuardedLLMAdapter, recorder: RecordedLLMExecutor,
                 config_fingerprint: str, execution_policy_fingerprint: str) -> None:
        object.__setattr__(self, "_settings", settings)
        object.__setattr__(self, "_backend", backend)
        object.__setattr__(self, "_model_id", model_id)
        object.__setattr__(self, "_adapter", adapter)
        object.__setattr__(self, "_guarded", guarded)
        object.__setattr__(self, "_recorder", recorder)
        object.__setattr__(self, "_config_fp", config_fingerprint)
        object.__setattr__(self, "_policy_fp", execution_policy_fingerprint)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("LLMRuntime references are read-only")

    @property
    def settings(self) -> AppSettings:
        return self._settings.model_copy(deep=True)

    @property
    def backend(self) -> str:
        return self._backend

    @property
    def resolved_model_id(self) -> str | None:
        return self._model_id

    @property
    def adapter(self) -> LLMAdapter:
        return self._adapter

    @property
    def guarded(self) -> GuardedLLMAdapter:
        return self._guarded

    @property
    def recorder(self) -> RecordedLLMExecutor:
        return self._recorder

    @property
    def config_fingerprint(self) -> str:
        return self._config_fp

    @property
    def execution_policy_fingerprint(self) -> str:
        return self._policy_fp

    def make_request(self, template: PromptTemplate, variables: Mapping[str, str]) -> PreparedCall:
        rendered = render_prompt(template, variables)
        # A-06 otherwise falls back to models.model_id; Mock must have no real model identity.
        models = self._settings.models.model_copy(update={"model_id": self._model_id})
        request = build_prompt_request(rendered, models, model_id=self._model_id)
        return PreparedCall(rendered, request, tuple(sorted(variables.items())))

    def _check_prepared(self, prepared: PreparedCall) -> None:
        if not isinstance(prepared, PreparedCall) or not isinstance(prepared.rendered, RenderedPrompt) or not isinstance(prepared.request, LLMRequest):
            raise RuntimeConfigurationError("INVALID_REQUEST")
        try:
            expected = self.make_request(prepared.rendered.template, dict(prepared.variables))
        except (ValueError, TypeError):
            raise RuntimeConfigurationError("REQUEST_MISMATCH") from None
        if expected != prepared or prepared.request.model_id != self._model_id:
            raise RuntimeConfigurationError("REQUEST_MISMATCH")

    def make_identity(self, prepared: PreparedCall, *, sample_id: str, scope_id: str,
                      run_kind: str, method: str | None, data_fingerprint: str,
                      schema_id: str | None, schema_version: str | None,
                      retriever_fingerprint: str | None, query_analysis_version: str | None,
                      git_sha: str) -> CallIdentity:
        self._check_prepared(prepared)
        try:
            return CallIdentity(
                sample_id=sample_id, task=prepared.request.task, scope_id=scope_id,
                method=method, run_kind=run_kind, backend=self._backend,
                resolved_model_id=self._model_id, data_fingerprint=data_fingerprint,
                template_id=prepared.rendered.template_id,
                template_version=prepared.rendered.version,
                template_fingerprint=prepared.rendered.template_fingerprint,
                schema_id=schema_id, schema_version=schema_version,
                config_fingerprint=self._config_fp, execution_policy_fingerprint=self._policy_fp,
                retriever_fingerprint=retriever_fingerprint,
                query_analysis_version=query_analysis_version, git_sha=git_sha,
            )
        except (ValueError, TypeError):
            raise RuntimeConfigurationError("INVALID_IDENTITY") from None

    def execute(self, prepared: PreparedCall, identity: CallIdentity, *,
                validator: Callable[[LLMRequest, LLMResponse], object] | None = None) -> RecordedCallResult:
        self._check_prepared(prepared)
        if not isinstance(identity, CallIdentity) or any((
            identity.task != prepared.request.task,
            identity.backend != self._backend,
            identity.resolved_model_id != self._model_id,
            identity.template_id != prepared.rendered.template_id,
            identity.template_version != prepared.rendered.version,
            identity.template_fingerprint != prepared.rendered.template_fingerprint,
            identity.config_fingerprint != self._config_fp,
            identity.execution_policy_fingerprint != self._policy_fp,
        )):
            raise RuntimeConfigurationError("IDENTITY_MISMATCH")
        return self._recorder.execute(prepared.request, identity, validator=validator)


def create_runtime(
    settings: AppSettings | None = None, *, config_dir: str | Path | None = None,
    backend: Literal["mock", "deepseek"] | None = None, api_enabled: bool = False,
    model_id: str | None = None, api_key: str | None = None, deepseek_client: Any | None = None,
    synthetic_adapter: LLMAdapter | None = None, mock_presets: Mapping[str, LLMResponse] | None = None,
    mock_strict: bool = False, token_estimator: TokenEstimator | None = None,
    estimator_version: str | None = None, strict_budget: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    store: MemoryRecordingStore | SQLiteRecordingStore | None = None,
    cache_enabled: bool = True,
) -> LLMRuntime:
    if settings is not None and config_dir is not None:
        raise RuntimeConfigurationError("SETTINGS_CONFLICT")
    if settings is None:
        settings = load_settings(config_dir)
    if not isinstance(settings, AppSettings):
        raise RuntimeConfigurationError("INVALID_SETTINGS")
    try:
        snapshot = AppSettings.model_validate(settings.model_dump(mode="python", warnings=False))
    except (ValidationError, ValueError, TypeError):
        raise RuntimeConfigurationError("INVALID_SETTINGS") from None
    selected = snapshot.models.default_backend if backend is None else backend
    if selected not in ("mock", "deepseek") or not isinstance(api_enabled, bool):
        raise RuntimeConfigurationError("INVALID_BACKEND")
    if token_estimator is not None and (not isinstance(estimator_version, str) or not _LABEL.fullmatch(estimator_version)):
        raise RuntimeConfigurationError("ESTIMATOR_VERSION_REQUIRED")
    if token_estimator is None and estimator_version is not None:
        raise RuntimeConfigurationError("ESTIMATOR_VERSION_UNUSED")
    resolved = snapshot.models.model_id if model_id is None else model_id
    if selected == "mock":
        if api_enabled or api_key is not None or deepseek_client is not None or model_id is not None:
            raise RuntimeConfigurationError("BACKEND_CONFLICT")
        if synthetic_adapter is not None and (isinstance(synthetic_adapter, (DeepSeekLLMAdapter, GuardedLLMAdapter, RecordedLLMExecutor)) or not callable(getattr(synthetic_adapter, "generate", None))):
            raise RuntimeConfigurationError("INVALID_SYNTHETIC_ADAPTER")
        if synthetic_adapter is not None and (mock_presets is not None or mock_strict):
            raise RuntimeConfigurationError("BACKEND_CONFLICT")
        adapter = synthetic_adapter if synthetic_adapter is not None else MockLLMAdapter(mock_presets, strict=mock_strict)
        resolved = None
    else:
        if synthetic_adapter is not None or mock_presets is not None or mock_strict:
            raise RuntimeConfigurationError("BACKEND_CONFLICT")
        if resolved is not None and (not isinstance(resolved, str) or not _LABEL.fullmatch(resolved)):
            raise RuntimeConfigurationError("MODEL_REQUIRED")
        if api_enabled and resolved is None:
            raise RuntimeConfigurationError("MODEL_REQUIRED")
        if api_enabled and deepseek_client is None and (not isinstance(api_key, str) or not api_key.strip()):
            raise RuntimeConfigurationError("CREDENTIAL_REQUIRED")
        adapter = DeepSeekLLMAdapter(
            api_enabled=api_enabled, client=deepseek_client, api_key=api_key,
            model_id=resolved, request_timeout_seconds=snapshot.models.request_timeout_seconds,
        )
    if store is not None and not isinstance(store, (MemoryRecordingStore, SQLiteRecordingStore)):
        raise RuntimeConfigurationError("INVALID_STORE")
    try:
        guarded = GuardedLLMAdapter(adapter, snapshot.models, token_estimator=token_estimator,
                                    strict_budget=strict_budget, sleep=sleep)
        recorder = RecordedLLMExecutor(guarded, store=store, cache_enabled=cache_enabled)
    except (ValueError, TypeError):
        raise RuntimeConfigurationError("INVALID_POLICY") from None
    return LLMRuntime(
        settings=snapshot, backend=selected, model_id=resolved, adapter=adapter,
        guarded=guarded, recorder=recorder,
        config_fingerprint=_hash(snapshot.model_dump(mode="json")),
        execution_policy_fingerprint=_hash({
            "version": "a09-v1", "backend": selected, "model_id": resolved,
            "api_enabled": api_enabled, "request_timeout_seconds": snapshot.models.request_timeout_seconds,
            "max_retries": snapshot.models.max_retries,
            "max_concurrent_api_requests": snapshot.models.max_concurrent_api_requests,
            "strict_budget": strict_budget, "estimator_version": estimator_version,
            "backoff_initial_seconds": 0.1, "backoff_max_seconds": 1.0,
            "acquire_timeout_seconds": snapshot.models.request_timeout_seconds,
        }),
    )
