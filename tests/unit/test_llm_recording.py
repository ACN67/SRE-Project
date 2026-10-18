"""Synthetic local call recording and cache tests; no enabled network adapter."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
from threading import Event, Lock
import json
import sqlite3

import pytest
from pydantic import BaseModel, ConfigDict

from dynamic_memory.llm import (
    CallIdentity, DeepSeekAdapterError, GuardedLLMAdapter, LLMMessage, LLMResponse,
    MemoryRecordingStore, QA_PROMPT, MEMORY_EXTRACTION_PROMPT, RecordedLLMExecutor,
    RecordingError, SQLiteRecordingStore, build_prompt_request, call_cache_key,
    render_prompt, validate_structured_output,
)
from dynamic_memory.llm.execution import LLMExecutionError
from dynamic_memory.pipeline.settings import load_settings


class Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[str]


def digest(value):
    return "sha256:" + sha256(value.encode()).hexdigest()


def request(task="qa", content="synthetic-secret"):
    template = QA_PROMPT if task == "qa" else MEMORY_EXTRACTION_PROMPT
    fields = {name: content for name in template.required_variables}
    return build_prompt_request(render_prompt(template, fields), load_settings().models)


def identity(task="qa", **changes):
    template = QA_PROMPT if task == "qa" else MEMORY_EXTRACTION_PROMPT
    values = dict(
        sample_id="synthetic_sample", task=task, scope_id="synthetic_scope",
        method="full_text" if task == "qa" else None, run_kind="development",
        backend="mock", resolved_model_id=None, data_fingerprint=digest("data"),
        template_id=template.template_id, template_version=template.version,
        template_fingerprint=template.template_fingerprint,
        schema_id=None if task == "qa" else "tests.Draft",
        schema_version=None if task == "qa" else "v1",
        config_fingerprint=digest("config"), execution_policy_fingerprint=digest("policy"),
        retriever_fingerprint=None, query_analysis_version="analysis-v1" if task == "qa" else None,
        git_sha="a" * 40,
    )
    values.update(changes)
    return CallIdentity(**values)


def validate_draft(call, response):
    return validate_structured_output(call, response, Draft)


class Fake:
    def __init__(self, usage=None, raw=None, error=None):
        self.calls = 0
        self.usage = usage
        self.raw = raw
        self.error = error

    def generate(self, call):
        self.calls += 1
        if self.error is not None:
            raise self.error
        raw = self.raw or ("Synthetic answer" if call.output_mode == "text" else '{"items":[]}')
        return LLMResponse(output_mode=call.output_mode, raw_output=raw, usage=self.usage)


def recorder(fake, store=None, cache_enabled=True):
    guarded = GuardedLLMAdapter(fake, load_settings().models, sleep=lambda _delay: None)
    return RecordedLLMExecutor(guarded, store=store, cache_enabled=cache_enabled)


def test_miss_hit_and_log_use_current_not_historical_usage():
    fake = Fake(usage={"prompt_tokens": 12, "completion_tokens": 3})
    recorded = recorder(fake)
    call, ctx = request(), identity()
    first = recorded.execute(call, ctx)
    second = recorded.execute(call, ctx)
    assert fake.calls == 1 and first.cache_status == "miss" and second.cache_status == "hit"
    assert first.response.raw_output == second.response.raw_output
    assert first.response is not second.response
    assert first.summary.attempt_count == 1 and first.summary.prompt_tokens_known == 12
    assert second.summary.attempt_count == 0 and second.summary.prompt_tokens_known is None
    assert second.source_summary.prompt_tokens_known == 12
    events = recorded.store.events()
    assert [item["cache_status"] for item in events] == ["miss", "hit"]
    assert [item["summary"]["attempt_count"] for item in events] == [1, 0]
    assert events[1]["summary"]["prompt_tokens_known"] is None
    assert events[0]["estimated_cost"] is None
    assert events[0]["timestamp"].endswith("+00:00")
    assert events[0]["identity"]["sample_id"] == ctx.sample_id
    serialized = json.dumps(events)
    assert "synthetic-secret" not in serialized and "Synthetic answer" not in serialized
    assert "input_value" not in serialized and "API_KEY" not in serialized


@pytest.mark.parametrize("change", [
    {"sample_id": "another_sample"}, {"scope_id": "another_scope"},
    {"run_kind": "interactive"}, {"run_kind": "evaluation"},
    {"method": "vector_rag", "retriever_fingerprint": digest("index")},
    {"method": "dynamic", "retriever_fingerprint": digest("index")},
    {"backend": "deepseek", "resolved_model_id": "deepseek-chat"},
    {"data_fingerprint": digest("new_data")},
    {"template_id": "qa_custom"}, {"template_version": "demo-v2"},
    {"template_fingerprint": digest("new_prompt")},
    {"schema_id": "tests.QA" , "schema_version": "v2"},
    {"config_fingerprint": digest("new_config")},
    {"execution_policy_fingerprint": digest("new_policy")},
    {"query_analysis_version": "analysis-v2"},
    {"git_sha": "b" * 40},
])
def test_changing_any_identity_component_changes_cache_key(change):
    call = request()
    assert call_cache_key(call, identity()) != call_cache_key(call, identity(**change))


def test_request_text_and_settings_change_cache_key():
    call, ctx = request(), identity()
    changed_text = call.model_copy(update={"messages": (LLMMessage(role="user", content="other text"),)})
    changed_settings = call.model_copy(update={"settings": call.settings.model_copy(update={"temperature": 0.2})})
    assert len({call_cache_key(item, ctx) for item in (call, changed_text, changed_settings)}) == 3
    fake = Fake()
    recorded = recorder(fake)
    assert recorded.execute(call, ctx).cache_status == "miss"
    assert recorded.execute(changed_text, ctx).cache_status == "miss"
    assert fake.calls == 2


@pytest.mark.parametrize("change", [
    {"sample_id": " "}, {"task": "bad"}, {"backend": "bad"},
    {"backend": "deepseek", "resolved_model_id": None},
    {"method": "bad"}, {"data_fingerprint": "unknown"},
    {"schema_id": "only_id"}, {"query_analysis_version": None},
    {"git_sha": "bad"},
])
def test_invalid_identity_is_rejected_before_provider(change):
    with pytest.raises(ValueError):
        identity(**change)


def test_identity_task_and_model_mismatch_cannot_make_broad_key():
    fake = Fake()
    recorded = recorder(fake)
    with pytest.raises(RecordingError) as info:
        recorded.execute(request(), identity(task="memory_extraction"), validator=validate_draft)
    assert info.value.code == "INVALID_IDENTITY" and fake.calls == 0
    deepseek_id = identity(backend="deepseek", resolved_model_id="deepseek-chat")
    call = request().model_copy(update={"model_id": "different-model"})
    with pytest.raises(RecordingError) as info:
        recorded.execute(call, deepseek_id)
    assert info.value.code == "INVALID_IDENTITY" and fake.calls == 0


def test_json_requires_validator_before_cache_and_rechecks_on_hit():
    fake = Fake()
    recorded = recorder(fake)
    call, ctx = request("memory_extraction"), identity("memory_extraction")
    first = recorded.execute(call, ctx, validator=validate_draft)
    assert first.cache_status == "miss" and first.summary.attempt_count == 1
    first.response.parsed_json["items"].append("mutated")
    second = recorded.execute(call, ctx, validator=validate_draft)
    assert second.cache_status == "hit" and second.response.parsed_json == {"items": []}
    assert second.summary.attempt_count == 0 and fake.calls == 1


def test_json_without_validator_is_logged_but_never_cached():
    fake = Fake()
    recorded = recorder(fake)
    call, ctx = request("memory_extraction"), identity("memory_extraction")
    assert recorded.execute(call, ctx).cache_status == "skip"
    assert recorded.execute(call, ctx).cache_status == "skip"
    assert fake.calls == 2 and len(recorded.store.events()) == 2


@pytest.mark.parametrize("raw,code", [
    ('{"wrong":1}', "SCHEMA_INVALID"),
    ('{"items":[],"items":[]}', "JSON_INTEGRITY"),
])
def test_invalid_business_json_is_logged_not_cached(raw, code):
    fake = Fake(raw=raw)
    recorded = recorder(fake)
    call, ctx = request("memory_extraction"), identity("memory_extraction")
    with pytest.raises(RecordingError) as info:
        recorded.execute(call, ctx, validator=validate_draft)
    assert info.value.code == code and info.value.summary.attempt_count == 1
    assert fake.calls == 1 and recorded.store.read(call_cache_key(call, ctx)) is None
    assert recorded.store.events()[0]["error_code"] == code


def test_executor_failure_records_actual_attempts_without_caching():
    fake = Fake(error=DeepSeekAdapterError("TIMEOUT", "synthetic-secret API_KEY"))
    recorded = recorder(fake)
    call, ctx = request(), identity()
    with pytest.raises(LLMExecutionError) as info:
        recorded.execute(call, ctx)
    assert info.value.code == "TIMEOUT" and info.value.summary.attempt_count == 3
    assert fake.calls == 3 and recorded.store.read(call_cache_key(call, ctx)) is None
    event = recorded.store.events()[0]
    assert event["status"] == "error" and event["summary"]["unknown_usage_attempts"] == 3
    assert "synthetic-secret" not in json.dumps(event) and "API_KEY" not in json.dumps(event)


def test_budget_reject_is_logged_before_provider():
    fake = Fake()
    recorded = recorder(fake)
    call, ctx = request(), identity()
    too_large = call.model_copy(update={"settings": call.settings.model_copy(update={"max_output_tokens": 99999})})
    with pytest.raises(LLMExecutionError) as info:
        recorded.execute(too_large, ctx)
    assert info.value.code == "REQUEST_BUDGET_EXCEEDED"
    assert info.value.summary.attempt_count == 0 and fake.calls == 0
    assert recorded.store.events()[0]["error_code"] == "REQUEST_BUDGET_EXCEEDED"


def test_default_store_never_creates_files_and_disabled_cache_is_explicit():
    fake = Fake()
    recorded = recorder(fake, cache_enabled=False)
    call, ctx = request(), identity()
    assert isinstance(recorded.store, MemoryRecordingStore)
    assert recorded.execute(call, ctx).cache_status == "skip"
    assert recorded.execute(call, ctx).cache_status == "skip"
    assert fake.calls == 2


def test_sqlite_opt_in_replays_across_instances_and_writes_no_raw_log(tmp_path):
    path = tmp_path / "calls.sqlite3"
    call, ctx = request(), identity()
    first_fake = Fake(usage={"prompt_tokens": 12, "completion_tokens": 3})
    first = recorder(first_fake, SQLiteRecordingStore(path)).execute(call, ctx)
    assert first.cache_status == "miss" and path.exists()
    second_fake = Fake(error=RuntimeError("must not call provider"))
    reopened = recorder(second_fake, SQLiteRecordingStore(path))
    second = reopened.execute(call, ctx)
    assert second.cache_status == "hit" and second_fake.calls == 0
    assert second.summary.attempt_count == 0 and second.source_summary.prompt_tokens_known == 12
    events = reopened.store.events()
    assert len(events) == 2 and [item["cache_status"] for item in events] == ["miss", "hit"]
    assert "synthetic-secret" not in json.dumps(events)


def test_cache_corruption_and_version_mismatch_never_replay(tmp_path):
    path = tmp_path / "calls.sqlite3"
    fake = Fake()
    recorded = recorder(fake, SQLiteRecordingStore(path))
    call, ctx = request(), identity()
    recorded.execute(call, ctx)
    key = call_cache_key(call, ctx)
    with sqlite3.connect(path) as connection:
        original = json.loads(connection.execute("SELECT blob FROM cache WHERE key=?", (key,)).fetchone()[0])
    original.pop("checksum")
    original["format_version"] = 0
    stale_checksum = "sha256:" + sha256(json.dumps(original, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    stale_blob = json.dumps({**original, "checksum": stale_checksum})
    for blob in ('{"broken":', stale_blob):
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE cache SET blob=? WHERE key=?", (blob, key))
        with pytest.raises(RecordingError) as info:
            recorded.execute(call, ctx)
        assert info.value.code == "CACHE_CORRUPT" and info.value.summary.attempt_count == 0
        assert fake.calls == 1
    assert [item["cache_status"] for item in recorded.store.events()] == ["miss", "corrupt", "corrupt"]


def test_cache_hit_rechecks_same_business_validator():
    fake = Fake()
    recorded = recorder(fake)
    call, ctx = request("memory_extraction"), identity("memory_extraction")
    assert recorded.execute(call, ctx, validator=validate_draft).cache_status == "miss"

    def rejected(_request, _response):
        raise ValueError("synthetic-secret changed validation")

    with pytest.raises(RecordingError) as info:
        recorded.execute(call, ctx, validator=rejected)
    assert info.value.code == "CACHE_CORRUPT" and fake.calls == 1
    assert "synthetic-secret" not in str(info.value)
    assert recorded.store.events()[-1]["cache_status"] == "corrupt"


def test_declared_backend_must_match_response_model():
    fake = Fake()
    recorded = recorder(fake)
    call = request()
    deepseek_ctx = identity(backend="deepseek", resolved_model_id="deepseek-chat")
    with pytest.raises(RecordingError) as info:
        recorded.execute(call, deepseek_ctx)
    assert info.value.code == "VALIDATION_FAILED" and fake.calls == 1
    assert recorded.store.read(call_cache_key(call, deepseek_ctx)) is None


def test_sqlite_requires_ignored_or_temporary_path(tmp_path):
    from pathlib import Path
    forbidden = Path.cwd() / "private-cache.sqlite3"
    with pytest.raises(ValueError):
        SQLiteRecordingStore(forbidden)
    assert not forbidden.exists()


def test_transaction_rolls_back_failed_event_and_preserves_old_cache(tmp_path):
    path = tmp_path / "calls.sqlite3"
    store = SQLiteRecordingStore(path)
    fake = Fake()
    recorded = recorder(fake, store)
    call, old_ctx = request(), identity()
    recorded.execute(call, old_ctx)
    new_ctx = replace(old_ctx, scope_id="different_scope")
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TRIGGER fail_event BEFORE INSERT ON events BEGIN SELECT RAISE(FAIL, 'injected'); END")
    with pytest.raises(RecordingError) as info:
        recorded.execute(call, new_ctx)
    assert info.value.code == "STORE_WRITE_FAILED" and info.value.summary.attempt_count == 1
    assert fake.calls == 2 and store.read(call_cache_key(call, new_ctx)) is None
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER fail_event")
    assert recorded.execute(call, old_ctx).cache_status == "hit"
    assert fake.calls == 2


def test_log_write_failure_is_observable_and_never_retries_provider():
    class FailingStore(MemoryRecordingStore):
        def append(self, event):
            raise OSError("synthetic-secret")
    fake = Fake(error=DeepSeekAdapterError("AUTH", "synthetic-secret"))
    recorded = recorder(fake, FailingStore())
    with pytest.raises(RecordingError) as info:
        recorded.execute(request(), identity())
    assert info.value.code == "LOG_WRITE_FAILED" and fake.calls == 1
    assert "synthetic-secret" not in str(info.value)


def test_same_key_single_flight_and_different_keys_remain_concurrent():
    entered = Event()
    release = Event()
    lock = Lock()
    state = {"calls": 0, "active": 0, "max": 0}

    class BlockingFake:
        def generate(self, call):
            with lock:
                state["calls"] += 1
                state["active"] += 1
                state["max"] = max(state["max"], state["active"])
                if state["active"] == 2:
                    entered.set()
            try:
                assert release.wait(3)
                return LLMResponse(output_mode="text", raw_output="Synthetic answer")
            finally:
                with lock:
                    state["active"] -= 1

    recorded = recorder(BlockingFake())
    call = request()
    first_ctx, second_ctx = identity(), identity(scope_id="different_scope")
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(recorded.execute, call, first_ctx)
        duplicate = pool.submit(recorded.execute, call, first_ctx)
        other = pool.submit(recorded.execute, call, second_ctx)
        try:
            assert entered.wait(3)
            assert state["calls"] == 2 and state["max"] == 2
        finally:
            release.set()
        results = [job.result(timeout=3) for job in (first, duplicate, other)]
    assert state["calls"] == 2 and state["active"] == 0
    assert sorted(item.cache_status for item in results) == ["hit", "miss", "miss"]


def test_single_flight_releases_after_failure_then_next_call_succeeds():
    entered = Event()
    release = Event()

    class OnceFail:
        calls = 0
        def generate(self, call):
            self.calls += 1
            if self.calls == 1:
                entered.set()
                assert release.wait(3)
                raise DeepSeekAdapterError("AUTH", "synthetic-secret")
            return LLMResponse(output_mode="text", raw_output="Synthetic answer")

    fake = OnceFail()
    recorded = recorder(fake)
    call, ctx = request(), identity()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(recorded.execute, call, ctx)
        try:
            assert entered.wait(3)
            second = pool.submit(recorded.execute, call, ctx)
        finally:
            release.set()
        with pytest.raises(LLMExecutionError):
            first.result(timeout=3)
        assert second.result(timeout=3).cache_status == "miss"
    assert fake.calls == 2
