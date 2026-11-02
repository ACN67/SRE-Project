"""B-06 behavior at the draft-to-MemoryRecord seam, using synthetic sources."""

import json
from datetime import datetime

import pytest

from dynamic_memory.contracts import (
    ConversationSample, ConversationSession, DatasetBundle, SourceTurn, TimeSpan,
)
from dynamic_memory.contracts.memory_id import compute_memory_id, validate_memory_id
from dynamic_memory.data.repository import SourceRepository
from dynamic_memory.llm import (
    CallIdentity, GuardedLLMAdapter, LLMResponse, MockLLMAdapter,
    RecordedLLMExecutor, request_fingerprint,
)
from dynamic_memory.memory.extraction import (
    MEMORY_DRAFT_PROMPT, AtomicFactDraft, WindowExtractionResult,
    build_extraction_request, extract_window,
)
from dynamic_memory.memory.normalization import (
    EvidenceAssessment, MemoryNormalizationError, normalize_memories,
)
from dynamic_memory.memory.windows import plan_extraction_windows
from dynamic_memory.pipeline.settings import load_settings


def _repository(*turns: SourceTurn) -> SourceRepository:
    sample = ConversationSample(
        sample_id="sample_1", source_order_1_based=1,
        sessions=[ConversationSession(session_id="session_1", turns=list(turns))],
    )
    return SourceRepository(DatasetBundle(
        snapshot_sha256="a" * 64, samples=[sample], split_by_sample_id={"sample_1": "main_dev"},
    ))


def _turn(dia_id: str = "d1", text: str = "Ada moved to Paris.",
          *, session_id: str = "session_1", sample_id: str = "sample_1",
          recorded_at: datetime | None = None) -> SourceTurn:
    return SourceTurn(sample_id=sample_id, session_id=session_id, dia_id=dia_id,
                      speaker="Ada", text=text, recorded_at=recorded_at)


def _draft(**changes) -> AtomicFactDraft:
    fields = dict(
        content="Ada moved to Paris.", entity="Ada", attribute="residence",
        value="Paris", fact_kind="event", assertion_type="fact", polarity="positive",
        recorded_at=None, event_time=None,
        source_refs=[{"session_id": "session_1", "dia_id": "d1"}],
    )
    fields.update(changes)
    return AtomicFactDraft(**fields)


def _result(*drafts: AtomicFactDraft, sample_id="sample_1", window_id="window_1") -> WindowExtractionResult:
    return WindowExtractionResult(
        sample_id=sample_id, extraction_window_id=window_id, facts=tuple(drafts),
        template_fingerprint="sha256:" + "b" * 64, input_tokens_estimated=100,
        cache_status="miss",
    )


def _approved(_draft, _turns) -> EvidenceAssessment:
    return EvidenceAssessment("supported", "SYNTHETIC_REVIEWED")


def _normalize(results, repository, checker=_approved):
    return normalize_memories("sample_1", results, repository, extractor_version="b06-v1",
                              evidence_checker=checker)


def test_supported_draft_becomes_contract_memory_with_verified_id() -> None:
    memories = _normalize([_result(_draft())], _repository(_turn()))

    assert len(memories) == 1
    memory = memories[0]
    assert memory.schema_version == "2.1"
    assert memory.sample_id == "sample_1"
    assert memory.content == "Ada moved to Paris."
    assert memory.source_refs[0].dia_id == "d1"
    assert memory.memory_id.startswith("mem_")
    assert memory.memory_id == compute_memory_id(memory)
    validate_memory_id(memory)
    later_version = normalize_memories(
        "sample_1", [_result(_draft())], _repository(_turn()),
        extractor_version="b06-v2", evidence_checker=_approved,
    )[0]
    assert later_version.memory_id == memory.memory_id
    assert later_version.extractor_version == "b06-v2"


def test_unclear_support_fails_closed_instead_of_publishing() -> None:
    with pytest.raises(MemoryNormalizationError) as caught:
        normalize_memories(
            "sample_1", [_result(_draft())], _repository(_turn()),
            extractor_version="b06-v1", evidence_checker=lambda _d, _t: EvidenceAssessment(
                "unclear", "NO_RELIABLE_CHECK",
            ),
        )
    assert caught.value.code == "EVIDENCE_UNCLEAR"


def test_empty_results_and_empty_facts_are_valid() -> None:
    repository = _repository(_turn())
    assert _normalize([], repository) == []
    assert _normalize([_result()], repository) == []
    with pytest.raises(MemoryNormalizationError) as caught:
        normalize_memories("unknown_sample", [], repository, extractor_version="b06-v1")
    assert caught.value.code == "SAMPLE_NOT_FOUND"


def test_multiple_distinct_facts_stay_distinct_with_stable_ids() -> None:
    repository = _repository(_turn(), _turn("d2", "Ada moved to Rome."))
    paris = _draft()
    rome = _draft(content="Ada moved to Rome.", value="Rome",
                  source_refs=[{"session_id": "session_1", "dia_id": "d2"}])
    first = _normalize([_result(paris, rome)], repository)
    second = _normalize([_result(rome, paris)], repository)
    assert [(m.content, m.memory_id) for m in first] == [(m.content, m.memory_id) for m in second]
    assert {m.value for m in first} == {"Paris", "Rome"}
    assert len({m.memory_id for m in first}) == 2


def test_same_session_duplicate_across_windows_merges_sources_before_id() -> None:
    repository = _repository(_turn(), _turn("d2"))
    first = _draft()
    second = _draft(source_refs=[{"session_id": "session_1", "dia_id": "d2"}])
    merged = _normalize([_result(first, window_id="window_1"),
                         _result(second, window_id="window_2")], repository)
    assert len(merged) == 1
    assert [ref.dia_id for ref in merged[0].source_refs] == ["d1", "d2"]
    assert merged[0].memory_id == compute_memory_id(merged[0])
    unmerged_id = _normalize([_result(first)], repository)[0].memory_id
    assert merged[0].memory_id != unmerged_id
    reversed_input = _normalize([_result(second, window_id="window_2"),
                                 _result(first, window_id="window_1")], repository)
    assert reversed_input[0].model_dump(mode="json") == merged[0].model_dump(mode="json")


def test_same_source_duplicate_windows_do_not_duplicate_source_ref() -> None:
    repository = _repository(_turn())
    memories = _normalize([_result(_draft(), window_id="one"),
                           _result(_draft(), window_id="two")], repository)
    assert len(memories) == 1 and len(memories[0].source_refs) == 1


@pytest.mark.parametrize("change", [
    {"polarity": "negative"}, {"assertion_type": "plan"},
    {"assertion_type": "hypothetical"}, {"assertion_type": "uncertain"},
    {"assertion_type": "unknown"}, {"fact_kind": "state"},
    {"fact_kind": "unknown"}, {"entity": "Other"},
    {"attribute": "different"}, {"value": "Elsewhere"},
    {"event_time": TimeSpan(raw="later")},
])
def test_semantically_distinct_fields_never_deduplicate(change) -> None:
    # The explicit checker represents a synthetic review decision; the code must
    # preserve distinct values even when an assessment approves both drafts.
    repository = _repository(_turn())
    base = _draft()
    changed = _draft(**change)
    if change.get("entity") == "Other":
        with pytest.raises(MemoryNormalizationError):
            _normalize([_result(base, changed)], repository)
        return
    memories = _normalize([_result(base, changed)], repository)
    assert len(memories) == 2
    assert memories[0].memory_id != memories[1].memory_id


def test_nfc_whitespace_and_source_order_are_canonical() -> None:
    repository = _repository(_turn(text="Áda moved to Paris."),
                             _turn("d2", text="Áda moved to Paris."))
    refs = [{"session_id": "session_1", "dia_id": "d2"},
            {"session_id": "session_1", "dia_id": "d1"}]
    draft = _draft(content="  Ada\t moved   to Paris.  ", entity=" A\u0301da ",
                   attribute="  residence  ", value=" Paris ", source_refs=refs)
    memory = _normalize([_result(draft)], repository)[0]
    assert memory.content == "Ada moved to Paris."
    assert memory.entity == "Áda"
    assert memory.value == "Paris"
    assert [ref.dia_id for ref in memory.source_refs] == ["d1", "d2"]
    assert memory.memory_id == compute_memory_id(memory)


def test_source_not_found_and_cross_sample_are_rejected() -> None:
    repository = _repository(_turn())
    missing = _draft(source_refs=[{"session_id": "session_1", "dia_id": "absent"}])
    with pytest.raises(MemoryNormalizationError) as caught:
        _normalize([_result(missing)], repository)
    assert caught.value.code == "SOURCE_NOT_FOUND"
    with pytest.raises(MemoryNormalizationError) as caught:
        _normalize([_result(_draft(), sample_id="other")], repository)
    assert caught.value.code == "SAMPLE_MISMATCH"


def test_source_existing_only_in_another_sample_is_rejected() -> None:
    own = ConversationSample(sample_id="sample_1", source_order_1_based=1, sessions=[
        ConversationSession(session_id="session_1", turns=[_turn()]),
    ])
    foreign = ConversationSample(sample_id="sample_2", source_order_1_based=2, sessions=[
        ConversationSession(session_id="session_2", turns=[
            _turn("d9", sample_id="sample_2", session_id="session_2"),
        ]),
    ])
    repository = SourceRepository(DatasetBundle(
        snapshot_sha256="a" * 64, samples=[own, foreign],
        split_by_sample_id={"sample_1": "main_dev", "sample_2": "extra_dev"},
    ))
    draft = _draft(source_refs=[{"session_id": "session_2", "dia_id": "d9"}])
    with pytest.raises(MemoryNormalizationError) as caught:
        _normalize([_result(draft)], repository)
    assert caught.value.code == "SAMPLE_MISMATCH"


@pytest.mark.parametrize("text,draft,code", [
    ("Bob bought a car.", _draft(), "EVIDENCE_UNCLEAR"),
    ("Ada bought a bicycle.", _draft(), "EVIDENCE_UNCLEAR"),
    ("Ada moved to Rome.", _draft(), "EVIDENCE_UNCLEAR"),
    ("Ada did not move to Paris.", _draft(), "EVIDENCE_UNCLEAR"),
    ("Ada plans to move to Paris.", _draft(), "EVIDENCE_UNCLEAR"),
    ("Ada moved to Paris on 2024-01-01.", _draft(event_time=TimeSpan(
        raw="2024-01-02", start=datetime(2024, 1, 2), end_exclusive=datetime(2024, 1, 3),
        precision="day", resolution="explicit",
    )), "EVIDENCE_UNSUPPORTED"),
])
def test_obvious_evidence_conflicts_fail_even_if_checker_says_supported(text, draft, code) -> None:
    with pytest.raises(MemoryNormalizationError) as caught:
        _normalize([_result(draft)], _repository(_turn(text=text)))
    assert caught.value.code == code
    assert caught.value.extraction_window_id == "window_1"
    assert caught.value.reason_code is not None
    assert text not in str(caught.value)


def test_missing_or_negative_review_never_publishes_a_memory() -> None:
    repository = _repository(_turn())
    for checker, expected in (
        (None, "EVIDENCE_UNCLEAR"),
        (lambda _d, _t: EvidenceAssessment("unsupported", "FACT_MISMATCH"), "EVIDENCE_UNSUPPORTED"),
        (lambda _d, _t: object(), "EVIDENCE_CHECK_FAILED"),
    ):
        with pytest.raises(MemoryNormalizationError) as caught:
            _normalize([_result(_draft())], repository, checker=checker)
        assert caught.value.code == expected


def test_each_cited_source_requires_its_own_support_decision() -> None:
    repository = _repository(_turn(), _turn("d2", "Ada mentioned a bicycle."))
    draft = _draft(source_refs=[{"session_id": "session_1", "dia_id": "d1"},
                                {"session_id": "session_1", "dia_id": "d2"}])
    def checker(_draft, turns):
        return EvidenceAssessment("supported" if turns[0].dia_id == "d1" else "unclear",
                                  "SYNTHETIC_CHECK")
    with pytest.raises(MemoryNormalizationError) as caught:
        _normalize([_result(draft)], repository, checker=checker)
    assert caught.value.code == "EVIDENCE_UNCLEAR"


def test_recorded_at_must_match_a_cited_source_and_unknown_stays_null() -> None:
    source_date = datetime(2024, 1, 1, 10)
    repository = _repository(_turn(recorded_at=source_date))
    good = _draft(recorded_at=source_date.isoformat())
    memory = _normalize([_result(good)], repository)[0]
    assert memory.recorded_at == source_date and memory.event_time is None
    wrong = _draft(recorded_at="2024-01-02T10:00:00")
    with pytest.raises(MemoryNormalizationError) as caught:
        _normalize([_result(wrong)], repository)
    assert caught.value.code == "RECORDED_AT_MISMATCH"


def test_different_sessions_are_not_merged() -> None:
    sample = ConversationSample(sample_id="sample_1", source_order_1_based=1, sessions=[
        ConversationSession(session_id="session_1", turns=[_turn()]),
        ConversationSession(session_id="session_2", turns=[
            _turn(session_id="session_2"),
        ]),
    ])
    repository = SourceRepository(DatasetBundle(
        snapshot_sha256="a" * 64, samples=[sample], split_by_sample_id={"sample_1": "main_dev"},
    ))
    second = _draft(source_refs=[{"session_id": "session_2", "dia_id": "d1"}])
    memories = _normalize([_result(_draft(), second)], repository)
    assert len(memories) == 2
    assert {memory.source_refs[0].session_id for memory in memories} == {"session_1", "session_2"}


def test_cross_session_source_refs_in_one_fact_are_rejected() -> None:
    sample = ConversationSample(sample_id="sample_1", source_order_1_based=1, sessions=[
        ConversationSession(session_id="session_1", turns=[_turn()]),
        ConversationSession(session_id="session_2", turns=[_turn(session_id="session_2")]),
    ])
    repository = SourceRepository(DatasetBundle(
        snapshot_sha256="a" * 64, samples=[sample], split_by_sample_id={"sample_1": "main_dev"},
    ))
    draft = _draft(source_refs=[{"session_id": "session_1", "dia_id": "d1"},
                                {"session_id": "session_2", "dia_id": "d1"}])
    with pytest.raises(MemoryNormalizationError) as caught:
        _normalize([_result(draft)], repository)
    assert caught.value.code == "SOURCE_SESSION_MISMATCH"


def test_distinct_event_dates_remain_separate() -> None:
    repository = _repository(_turn(text="Ada moved to Paris in 2023 and 2024."))
    first = _draft(event_time=TimeSpan(raw="2023", start=datetime(2023, 1, 1),
                                        end_exclusive=datetime(2024, 1, 1), precision="year",
                                        resolution="explicit"))
    second = _draft(event_time=TimeSpan(raw="2024", start=datetime(2024, 1, 1),
                                         end_exclusive=datetime(2025, 1, 1), precision="year",
                                         resolution="explicit"))
    memories = _normalize([_result(first, second)], repository)
    assert len(memories) == 2 and memories[0].memory_id != memories[1].memory_id


def test_bypassed_draft_validation_cannot_enter_success_output() -> None:
    damaged = _draft().model_copy(update={"polarity": "invented"})
    with pytest.raises(MemoryNormalizationError) as caught:
        _normalize([_result(damaged)], _repository(_turn()))
    assert caught.value.code == "DRAFT_INVALID"


def test_caption_keeps_its_origin_and_never_appears_in_diagnostics() -> None:
    source = SourceTurn(sample_id="sample_1", session_id="session_1", dia_id="d1",
                        speaker="Ada", text="", image_caption="Ada holds a blue book",
                        caption_origin="blip_caption")
    draft = _draft(content="Ada holds a blue book", entity="Ada", attribute="object",
                   value="blue book")
    seen = []
    def checker(_draft, turns):
        seen.append(turns[0])
        return EvidenceAssessment("unclear", "CAPTION_NEEDS_REVIEW")
    with pytest.raises(MemoryNormalizationError) as caught:
        _normalize([_result(draft)], _repository(source), checker=checker)
    assert caught.value.code == "EVIDENCE_UNCLEAR"
    assert seen[0].text == "" and seen[0].image_caption == "Ada holds a blue book"
    assert "blue book" not in str(caught.value)


def test_real_b04_b05_mock_overlap_flows_into_one_b06_memory() -> None:
    settings = load_settings()
    turns = [_turn(f"d{i}", "Ada moved to Paris. " + "x" * 760) for i in range(1, 7)]
    sample = ConversationSample(sample_id="sample_1", source_order_1_based=1, sessions=[
        ConversationSession(session_id="session_1", turns=turns),
    ])
    repository = SourceRepository(DatasetBundle(
        snapshot_sha256="a" * 64, samples=[sample], split_by_sample_id={"sample_1": "main_dev"},
    ))
    windows = plan_extraction_windows(sample, settings.extraction)
    assert "d3" in [ref.dia_id for ref in windows[0].source_refs]
    assert "d3" in [ref.dia_id for ref in windows[1].source_refs]
    raw = json.dumps({"facts": [_draft(source_refs=[
        {"session_id": "session_1", "dia_id": "d3"},
    ]).model_dump(mode="json")]})
    presets = {
        request_fingerprint(build_extraction_request(window, settings.models)):
            LLMResponse(output_mode="json", raw_output=raw)
        for window in windows[:2]
    }
    executor = RecordedLLMExecutor(GuardedLLMAdapter(
        MockLLMAdapter(presets, strict=True), settings.models, sleep=lambda _: None,
    ))
    sha = "sha256:" + "a" * 64

    def identity(window):
        return CallIdentity(
            sample_id="sample_1", task="memory_extraction", scope_id=window.extraction_window_id,
            method=None, run_kind="development", backend="mock", resolved_model_id=None,
            data_fingerprint=sha, template_id=MEMORY_DRAFT_PROMPT.template_id,
            template_version=MEMORY_DRAFT_PROMPT.version,
            template_fingerprint=MEMORY_DRAFT_PROMPT.template_fingerprint,
            schema_id="atomic_fact_draft", schema_version="1", config_fingerprint=sha,
            execution_policy_fingerprint=sha, retriever_fingerprint=None,
            query_analysis_version=None, git_sha="0" * 40,
        )

    results = [extract_window(window, executor, identity(window), settings.models)
               for window in windows[:2]]
    memories = _normalize(results, repository)
    assert len(memories) == 1
    assert [ref.dia_id for ref in memories[0].source_refs] == ["d3"]
    assert memories[0].memory_id == compute_memory_id(memories[0])
    assert [event["status"] for event in executor.store.events()] == ["success", "success"]
