"""Synthetic, offline integration of the single A-09 runtime."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
from threading import Event, Lock
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ConfigDict

from dynamic_memory.llm import (
    MEMORY_EXTRACTION_PROMPT, QA_PROMPT, STATE_REASONING_PROMPT,
    LLMResponse, PromptTemplate, validate_structured_output,
)
from dynamic_memory.llm.execution import LLMExecutionError
from dynamic_memory.llm.recording import SQLiteRecordingStore
from dynamic_memory.pipeline.runtime import RuntimeConfigurationError, create_runtime
from dynamic_memory.pipeline.settings import load_settings


SHA = "a" * 40  # synthetic fixture, never used as a production commit identity


def fp(value: str) -> str:
    return "sha256:" + sha256(value.encode()).hexdigest()


def prepared(runtime, task="qa", context="synthetic context"):
    if task == "qa":
        return runtime.make_request(QA_PROMPT, {"context": context, "question": "synthetic question"})
    if task == "memory_extraction":
        return runtime.make_request(MEMORY_EXTRACTION_PROMPT, {"conversation": context})
    return runtime.make_request(STATE_REASONING_PROMPT, {"candidate_context": context, "question": "synthetic question"})


def identity(runtime, call, *, method="full_text", scope="synthetic_query", data="synthetic_data", schema=None):
    task = call.request.task
    return runtime.make_identity(
        call, sample_id="synthetic_sample", scope_id=scope, run_kind="development",
        method=method if task == "qa" else None, data_fingerprint=fp(data),
        schema_id=schema if task != "qa" else None,
        schema_version="synthetic-v1" if task != "qa" else None,
        retriever_fingerprint=fp("synthetic_index") if task == "state_reasoning" or method in ("vector_rag", "dynamic") and task == "qa" else None,
        query_analysis_version="synthetic-analysis-v1" if task != "memory_extraction" else None,
        git_sha=SHA,
    )


def test_default_shared_runtime_and_three_prompt_budgets(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-secret-must-not-be-read")
    settings = load_settings()
    runtime = create_runtime(settings)
    assert runtime.backend == "mock" and runtime.resolved_model_id is None
    assert runtime.adapter is runtime.guarded._adapter
    assert runtime.guarded is runtime.recorder._executor
    assert runtime.settings == settings
    assert runtime.settings is not settings
    for task, expected in (("memory_extraction", (4096, 4096, "json")),
                           ("state_reasoning", (8192, 2048, "json")),
                           ("qa", (32768, 256, "text"))):
        call = prepared(runtime, task)
        assert (call.request.settings.max_input_tokens, call.request.settings.max_output_tokens,
                call.request.output_mode) == expected
        assert call.request.model_id is None
    call = prepared(runtime)
    first = runtime.execute(call, identity(runtime, call))
    second = runtime.execute(call, identity(runtime, call))
    assert (first.cache_status, second.cache_status) == ("miss", "hit")
    assert first.summary.attempt_count == 1 and second.summary.attempt_count == 0
    assert second.response.usage is None
    assert len(runtime.recorder.store.events()) == 2


class Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mock: bool
    synthetic: bool
    task: str
    request_fingerprint: str


def test_json_validator_controls_cache_and_failures():
    runtime = create_runtime(load_settings())
    call = prepared(runtime, "memory_extraction")
    key = identity(runtime, call, schema="synthetic_draft")
    assert runtime.execute(call, key).cache_status == "skip"
    validator = lambda req, resp: validate_structured_output(req, resp, Draft)
    assert runtime.execute(call, key, validator=validator).cache_status == "miss"
    assert runtime.execute(call, key, validator=validator).cache_status == "hit"
    with pytest.raises(Exception):
        runtime.execute(call, key, validator=lambda _req, _resp: (_ for _ in ()).throw(ValueError("private")))
    assert len(runtime.recorder.store.events()) == 4


def test_three_qa_methods_share_generation_and_isolate_keys():
    runtime = create_runtime(load_settings())
    calls = [prepared(runtime, context=f"synthetic {method}") for method in ("full_text", "vector_rag", "dynamic")]
    methods = ("full_text", "vector_rag", "dynamic")
    assert len({call.rendered.template_fingerprint for call in calls}) == 1
    assert len({call.request.messages[0] for call in calls}) == 1
    assert len({call.request.settings for call in calls}) == 1
    keys = [runtime.execute(call, identity(runtime, call, method=method)).cache_key for call, method in zip(calls, methods)]
    assert len(set(keys)) == 3
    state = prepared(runtime, "state_reasoning")
    assert runtime.execute(state, identity(runtime, state, schema="synthetic_state")).cache_status == "skip"
    assert len(runtime.recorder.store.events()) == 4


def test_identity_preflight_rejects_conflicts_without_provider_call():
    runtime = create_runtime(load_settings())
    call = prepared(runtime)
    good = identity(runtime, call)
    for bad in (replace(good, backend="deepseek", resolved_model_id="synthetic-model"),
                replace(good, config_fingerprint=fp("wrong")),
                replace(good, template_fingerprint=fp("wrong"))):
        with pytest.raises(RuntimeConfigurationError) as exc:
            runtime.execute(call, bad)
        assert exc.value.code == "IDENTITY_MISMATCH"
    with pytest.raises(RuntimeConfigurationError):
        runtime.make_identity(call, sample_id="bad id", scope_id="synthetic_query",
                              run_kind="development", method="full_text", data_fingerprint=fp("data"),
                              schema_id=None, schema_version=None, retriever_fingerprint=None,
                              query_analysis_version="v1", git_sha=SHA)
    assert runtime.recorder.store.events() == ()


def test_cache_key_tracks_scope_data_template_analysis_and_method():
    runtime = create_runtime(load_settings())
    call = prepared(runtime)
    original = identity(runtime, call)
    variants = (
        replace(original, scope_id="other_query"),
        replace(original, data_fingerprint=fp("other_data")),
        replace(original, query_analysis_version="synthetic-analysis-v2"),
        replace(original, method="vector_rag", retriever_fingerprint=fp("synthetic_index")),
    )
    keys = [runtime.execute(call, item).cache_key for item in (original, *variants)]
    assert len(set(keys)) == len(keys)
    newer = PromptTemplate(template_id="qa_shared", version="synthetic-v2", task="qa",
                           system_template=QA_PROMPT.system_template,
                           user_template=QA_PROMPT.user_template,
                           required_variables=QA_PROMPT.required_variables)
    changed = runtime.make_request(newer, {"context": "synthetic context", "question": "synthetic question"})
    assert runtime.execute(changed, identity(runtime, changed)).cache_key not in keys


def test_request_and_model_identity_cannot_be_rebound():
    runtime = create_runtime(load_settings())
    call = prepared(runtime)
    altered = replace(call, request=call.request.model_copy(update={"model_id": "synthetic-model"}))
    with pytest.raises(RuntimeConfigurationError, match="REQUEST_MISMATCH"):
        runtime.execute(altered, identity(runtime, call))
    with pytest.raises(RuntimeConfigurationError, match="BACKEND_CONFLICT"):
        create_runtime(load_settings(), backend="deepseek", synthetic_adapter=object())


def test_deepseek_is_separately_opted_in_with_offline_fake():
    settings = load_settings()
    disabled = create_runtime(settings, backend="deepseek", model_id="synthetic-model")
    call = prepared(disabled)
    with pytest.raises(LLMExecutionError) as exc:
        disabled.execute(call, identity(disabled, call))
    assert exc.value.code == "API_DISABLED"
    for kwargs, code in (({"backend": "deepseek", "api_enabled": True}, "MODEL_REQUIRED"),
                         ({"backend": "deepseek", "api_enabled": True, "model_id": "synthetic-model"}, "CREDENTIAL_REQUIRED"),
                         ({"backend": "mock", "api_enabled": True}, "BACKEND_CONFLICT"),
                         ({"backend": "deepseek", "model_id": " "}, "MODEL_REQUIRED")):
        with pytest.raises(RuntimeConfigurationError) as error:
            create_runtime(settings, **kwargs)
        assert error.value.code == code

    class FakeCompletions:
        calls = 0

        def create(self, **kwargs):
            self.calls += 1
            assert kwargs["model"] == "synthetic-model"
            return SimpleNamespace(model="synthetic-model", usage=None,
                                   choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="synthetic answer"))])

    completions = FakeCompletions()
    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    runtime = create_runtime(settings, backend="deepseek", api_enabled=True,
                             model_id="synthetic-model", deepseek_client=fake_client)
    call = prepared(runtime)
    assert call.request.model_id == "synthetic-model"
    assert runtime.execute(call, identity(runtime, call)).response.model_id == "synthetic-model"
    assert completions.calls == 1


def test_fingerprints_follow_settings_and_explicit_estimator_version():
    settings = load_settings()
    original = create_runtime(settings)
    changed = settings.model_copy(deep=True)
    changed.retrieval.base_top_k = 9
    assert create_runtime(changed).config_fingerprint != original.config_fingerprint
    estimate = lambda _messages: 5
    with pytest.raises(RuntimeConfigurationError, match="ESTIMATOR_VERSION_REQUIRED"):
        create_runtime(settings, token_estimator=estimate)
    one = create_runtime(settings, token_estimator=estimate, estimator_version="synthetic-v1")
    two = create_runtime(settings, token_estimator=estimate, estimator_version="synthetic-v2")
    assert one.execution_policy_fingerprint != two.execution_policy_fingerprint
    assert create_runtime(settings, strict_budget=True).execution_policy_fingerprint != original.execution_policy_fingerprint
    with pytest.raises(LLMExecutionError, match="INPUT_BUDGET_UNKNOWN"):
        strict = create_runtime(settings, strict_budget=True)
        call = prepared(strict)
        strict.execute(call, identity(strict, call))


def test_sqlite_replay_with_complete_identity(tmp_path):
    settings = load_settings()
    path = tmp_path / "synthetic.sqlite3"
    first = create_runtime(settings, store=SQLiteRecordingStore(path))
    call = prepared(first)
    assert first.execute(call, identity(first, call)).cache_status == "miss"
    second = create_runtime(settings, store=SQLiteRecordingStore(path))
    call = prepared(second)
    assert second.execute(call, identity(second, call)).cache_status == "hit"
    assert second.execute(call, identity(second, call, data="changed_data")).cache_status == "miss"


def test_shared_guard_limits_distinct_paths_to_two():
    entered = Event()
    release = Event()
    lock = Lock()

    class SyntheticAdapter:
        active = 0
        peak = 0
        calls = 0

        def generate(self, request):
            with lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
                self.calls += 1
                if self.active == 2:
                    entered.set()
            release.wait(5)
            with lock:
                self.active -= 1
            return LLMResponse(output_mode=request.output_mode,
                               raw_output="{}" if request.output_mode == "json" else "synthetic answer")

    adapter = SyntheticAdapter()
    runtime = create_runtime(load_settings(), synthetic_adapter=adapter)
    tasks = [("memory_extraction", None), ("state_reasoning", None),
             ("qa", "full_text"), ("qa", "vector_rag"), ("qa", "dynamic")]

    def run(index, task, method):
        call = prepared(runtime, task, context=f"synthetic {index}")
        return runtime.execute(call, identity(runtime, call, method=method, scope=f"scope_{index}",
                                                      schema="synthetic_schema" if task != "qa" else None))

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(run, i, task, method) for i, (task, method) in enumerate(tasks)]
        try:
            assert entered.wait(3)
        finally:
            release.set()
        results = [future.result(timeout=6) for future in futures]
    assert len(results) == 5 and adapter.calls == 5 and adapter.peak == 2
