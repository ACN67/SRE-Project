"""B-07 orchestration at the builder's public seam, using offline Mock calls."""

import json

import pytest

from dynamic_memory.contracts import ConversationSample, ConversationSession, DatasetBundle, SourceTurn
from dynamic_memory.data.repository import SourceRepository
from dynamic_memory.llm import CallIdentity, GuardedLLMAdapter, LLMResponse, MockLLMAdapter, RecordedLLMExecutor, request_fingerprint
from dynamic_memory.memory.build import MemoryBuilder, MemoryBuildError
from dynamic_memory.memory.extraction import MEMORY_DRAFT_PROMPT, build_extraction_request
from dynamic_memory.memory.normalization import EvidenceAssessment, MemoryNormalizationError
from dynamic_memory.memory.windows import plan_extraction_windows
from dynamic_memory.pipeline.settings import load_settings


def _setup(texts=(("Ada moved to Paris.",),), *, budget=None, responses=None, checker=None,
           reserved_prompt_tokens=1800):
    settings = load_settings()
    if budget is not None:
        settings.extraction.window_max_input_tokens = budget
        settings.models.extraction_max_input_tokens = budget
    sample = ConversationSample(sample_id="sample_1", source_order_1_based=1, sessions=[
        ConversationSession(session_id=f"session_{i}", turns=[
            SourceTurn(sample_id="sample_1", session_id=f"session_{i}", dia_id=f"d{j}", speaker="Ada", text=t)
            for j, t in enumerate(session, 1)
        ]) for i, session in enumerate(texts, 1)
    ])
    repository = SourceRepository(DatasetBundle(snapshot_sha256="a" * 64, samples=[sample],
                                                 split_by_sample_id={"sample_1": "main_dev"}))
    windows = plan_extraction_windows(
        sample, settings.extraction, reserved_prompt_tokens=reserved_prompt_tokens,
    )
    facts = {}
    for window in windows:
        fact = dict(content="Ada moved to Paris.", entity="Ada", attribute="residence", value="Paris",
                    fact_kind="event", assertion_type="fact", polarity="positive", recorded_at=None,
                    event_time=None, source_refs=[ref.model_dump() for ref in window.source_refs[:1]])
        raw = json.dumps({"facts": [fact]}) if responses is None else responses(window, fact)
        facts[request_fingerprint(build_extraction_request(window, settings.models))] = LLMResponse(output_mode="json", raw_output=raw)
    executor = RecordedLLMExecutor(GuardedLLMAdapter(MockLLMAdapter(facts, strict=True), settings.models,
                                                      sleep=lambda _: None))
    sha = "sha256:" + "b" * 64
    def identity(window):
        return CallIdentity(sample_id=window.sample_id, task="memory_extraction", scope_id=window.extraction_window_id,
                            method=None, run_kind="development", backend="mock", resolved_model_id=None,
                            data_fingerprint=sha, template_id=MEMORY_DRAFT_PROMPT.template_id,
                            template_version=MEMORY_DRAFT_PROMPT.version,
                            template_fingerprint=MEMORY_DRAFT_PROMPT.template_fingerprint,
                            schema_id="atomic_fact_draft", schema_version="1", config_fingerprint=sha,
                            execution_policy_fingerprint=sha, retriever_fingerprint=None,
                            query_analysis_version=None, git_sha="0" * 40)
    builder = MemoryBuilder(repository, executor, settings.models, settings.extraction, identity,
                            extractor_version="b07-v1", evidence_checker=checker,
                            reserved_prompt_tokens=reserved_prompt_tokens)
    return sample, builder, windows


def _approved(_draft, _sources):
    return EvidenceAssessment("supported", "MOCK_REVIEWED")


def test_complete_mock_chain_and_strict_entry() -> None:
    sample, builder, windows = _setup(checker=_approved)
    report = builder.build_memories_with_report(sample)
    assert report.build_status == "complete" and not report.partial_build
    assert (report.total_windows, report.successful_windows) == (1, 1)
    assert report.window_diagnostics[0].input_fact_count == 1
    assert report.window_diagnostics[0].accepted_fact_count == 1
    assert len(report.memories) == 1
    assert builder.build_memories(sample) == list(report.memories)


def test_empty_facts_is_complete_but_missing_checker_is_failed() -> None:
    sample, builder, windows = _setup(checker=None)
    report = builder.build_memories_with_report(sample)
    assert report.build_status == "failed"
    assert report.window_diagnostics[0].reason_code == "NO_CHECKER"
    with pytest.raises(MemoryBuildError, match="MEMORY_BUILD_FAILED"):
        builder.build_memories(sample)


def test_zero_facts_and_zero_windows_are_complete() -> None:
    sample, builder, _ = _setup(checker=None, responses=lambda _w, _f: '{"facts": []}')
    assert builder.build_memories(sample) == []
    assert builder.build_memories_with_report(sample).build_status == "complete"
    empty, builder, _ = _setup(texts=(), checker=None)
    assert builder.build_memories_with_report(empty).total_windows == 0
    assert builder.build_memories(empty) == []


def test_partial_keeps_reviewed_records_and_strict_rejects() -> None:
    sample, builder, windows = _setup(texts=(("Ada moved to Paris.",), ("Ada moved to Paris.",)),
                                      checker=_approved,
                                      responses=lambda w, f: json.dumps({"facts": [f]})
                                      if w.session_id == "session_1" else '{}')
    report = builder.build_memories_with_report(sample)
    assert (report.build_status, report.partial_build, report.total_windows, report.successful_windows) == (
        "partial", True, 2, 1)
    assert len(report.memories) == 1
    assert report.window_diagnostics[1].status == "failed"
    with pytest.raises(MemoryBuildError, match="MEMORY_BUILD_INCOMPLETE") as caught:
        builder.build_memories(sample)
    assert len(caught.value.report.memories) == 1


def test_all_windows_failed_and_review_reason_is_visible_without_source_text() -> None:
    sample, builder, windows = _setup(texts=(("Ada moved to Paris.",), ("Ada moved to Paris.",)),
                                      checker=lambda _d, _s: EvidenceAssessment("unclear", "NEEDS_REVIEW"))
    report = builder.build_memories_with_report(sample)
    assert report.build_status == "failed" and not report.memories
    assert all(d.error_code == "EVIDENCE_UNCLEAR" and d.reason_code == "NEEDS_REVIEW"
               for d in report.window_diagnostics)
    assert all(d.input_fact_count == d.rejected_fact_count == 1 for d in report.window_diagnostics)
    assert "Ada moved" not in str(report)  # DTO repr should not echo source text


def test_planning_failure_is_not_a_zero_window_success() -> None:
    sample, builder, _ = _setup(texts=(("Ada moved to Paris.",),), checker=_approved)
    builder.reserved_prompt_tokens = 4096
    report = builder.build_memories_with_report(sample)
    assert report.build_status == "failed"
    assert report.window_diagnostics[0].stage == "planning"
    assert report.window_diagnostics[0].error_code == "WINDOW_BUDGET_INVALID"


def test_source_mismatch_fails_before_model_call() -> None:
    sample, builder, _ = _setup(checker=_approved)
    other = sample.model_copy(update={"sample_id": "other"})
    report = builder.build_memories_with_report(other)
    assert report.build_status == "failed"
    assert report.window_diagnostics[0].error_code == "INVALID_SAMPLE_OR_SETTINGS"
    assert not builder.executor.store.events()


def test_repeated_build_is_deterministic_and_sessions_stay_separate() -> None:
    sample, builder, _ = _setup(texts=(("Ada moved to Paris.",), ("Ada moved to Paris.",)), checker=_approved)
    first = builder.build_memories(sample)
    second = builder.build_memories(sample)
    assert len(first) == len(second) == 2
    assert [m.memory_id for m in first] == [m.memory_id for m in second]
    assert {m.source_refs[0].session_id for m in first} == {"session_1", "session_2"}


@pytest.mark.parametrize("checker,code,reason", [
    (lambda _d, _s: EvidenceAssessment("unsupported", "DENIED"), "EVIDENCE_UNSUPPORTED", "DENIED"),
    (lambda _d, _s: (_ for _ in ()).throw(RuntimeError("secret-key")), "EVIDENCE_CHECK_FAILED", "CHECKER_EXCEPTION"),
])
def test_reviewer_rejection_or_error_is_diagnostic_not_zero_facts(checker, code, reason) -> None:
    sample, builder, _ = _setup(checker=checker)
    report = builder.build_memories_with_report(sample)
    diagnostic = report.window_diagnostics[0]
    assert report.build_status == "failed" and not report.memories
    assert (diagnostic.error_code, diagnostic.reason_code) == (code, reason)
    assert diagnostic.input_fact_count == diagnostic.rejected_fact_count == 1
    assert "secret-key" not in str(diagnostic)


def test_invalid_source_in_model_output_cannot_be_published() -> None:
    sample, builder, _ = _setup(checker=_approved, responses=lambda _w, fact: json.dumps({"facts": [
        {**fact, "source_refs": [{"session_id": "session_1", "dia_id": "missing"}]}
    ]}))
    report = builder.build_memories_with_report(sample)
    assert report.build_status == "failed"
    assert report.window_diagnostics[0].error_code == "SOURCE_OUT_OF_WINDOW"


def test_prompt_budget_failure_is_window_failure_without_provider_call() -> None:
    sample, builder, _ = _setup(checker=_approved)
    builder.token_estimator = lambda _messages: 4096
    report = builder.build_memories_with_report(sample)
    assert report.build_status == "failed"
    assert report.window_diagnostics[0].error_code == "INPUT_BUDGET_EXCEEDED"
    assert not builder.executor.store.events()


def test_overlapping_windows_merge_sources_and_keep_one_memory() -> None:
    # More than one window in the same session, with repeated identical facts.
    sample, builder, windows = _setup(texts=(tuple("Ada moved to Paris." for _ in range(30)),),
                                      budget=4096, checker=_approved)
    assert len(windows) > 1
    report = builder.build_memories_with_report(sample)
    assert report.build_status == "complete", [(d.error_code, d.reason_code) for d in report.window_diagnostics]
    assert len(report.memories) == 1
    assert len(report.memories[0].source_refs) > 1
    assert report.total_windows == report.successful_windows == len(windows)


def test_finalization_failure_keeps_real_window_count_and_fails_build(monkeypatch) -> None:
    import dynamic_memory.memory.build as build_module

    sample, builder, windows = _setup(checker=_approved)

    def fail_finalization(_memories):
        raise MemoryNormalizationError("MEMORY_INVALID")

    monkeypatch.setattr(build_module, "merge_reviewed_memories", fail_finalization)
    report = builder.build_memories_with_report(sample)

    assert report.total_windows == len(windows) == 1
    assert report.successful_windows == 0
    assert report.build_status == "failed"
    assert report.memories == ()
    assert report.window_diagnostics[0].stage == "finalization"
    assert report.window_diagnostics[0].error_code == "MEMORY_INVALID"

