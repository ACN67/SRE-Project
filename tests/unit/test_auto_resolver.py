from __future__ import annotations

from datetime import datetime, timezone

from dynamic_memory.contracts import (
    MemoryRecord,
    QueryAnalysis,
    QueryRequest,
    SourceRef,
    TimeSpan,
)
from dynamic_memory.contracts.memory_id import compute_memory_id
from dynamic_memory.reasoning.auto_resolver import DeterministicStateResolver


def _memory(
    value: str,
    content: str,
    *,
    year: int,
    polarity: str = "positive",
    dia_id: str = "D1:1",
    entity: str = "Ada",
    attribute: str = "residence",
) -> MemoryRecord:
    start = datetime(year, 1, 1, tzinfo=timezone.utc)
    end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    record = MemoryRecord(
        memory_id="mem_" + "0" * 20,
        sample_id="conv-auto",
        content=content,
        entity=entity,
        attribute=attribute,
        value=value,
        fact_kind="state",
        assertion_type="fact",
        polarity=polarity,
        recorded_at=start,
        event_time=TimeSpan(
            raw=str(year),
            start=start,
            end_exclusive=end,
            precision="year",
            resolution="explicit",
        ),
        source_refs=[SourceRef(session_id="session_1", dia_id=dia_id)],
        extractor_version="auto-utterance-v2",
    )
    record.memory_id = compute_memory_id(record)
    return record


def _query(intent: str = "current_state"):
    request = QueryRequest(
        query_id="q-auto",
        sample_id="conv-auto",
        question="Where does Ada live now?",
    )
    analysis = QueryAnalysis(
        query_id=request.query_id,
        sample_id=request.sample_id,
        query_intent=intent,
        entity_hints=["Ada"],
        attribute_hints=["residence"],
        time_mode="latest_known",
    )
    return request, analysis


def test_explicit_replacement_excludes_older_state() -> None:
    request, analysis = _query()
    older = _memory("Paris", "I live in Paris.", year=2022, dia_id="D1:1")
    newer = _memory(
        "Rome",
        "I moved to Rome instead and live there now.",
        year=2023,
        dia_id="D2:1",
    )
    result = DeterministicStateResolver(
        conversation_cutoff=lambda _: datetime(
            2024, 1, 1, tzinfo=timezone.utc
        )
    ).resolve(request, analysis, [older, newer])

    assert [item.action for item in result.decisions] == [
        "EXCLUDE",
        "RETAIN",
    ]
    assert result.effective_memory_ids == [newer.memory_id]
    assert result.relations[0].relation == "SUPERSEDE"
    assert result.unresolved_conflict_ids == []


def test_state_history_keeps_both_ordered_claims() -> None:
    request, analysis = _query("state_history")
    older = _memory("Paris", "I live in Paris.", year=2022)
    newer = _memory("Rome", "I moved to Rome instead.", year=2023)
    result = DeterministicStateResolver().resolve(
        request,
        analysis,
        [older, newer],
    )
    assert result.effective_memory_ids == [older.memory_id, newer.memory_id]
    assert result.relations[0].relation == "SUPERSEDE"


def test_unresolved_polarity_conflict_is_fail_closed() -> None:
    request, analysis = _query()
    positive = _memory("Rome", "I live in Rome.", year=2023)
    negative = _memory(
        "Rome",
        "I do not live in Rome.",
        year=2023,
        polarity="negative",
    )
    result = DeterministicStateResolver().resolve(
        request,
        analysis,
        [positive, negative],
    )
    assert set(result.unresolved_conflict_ids) == {
        positive.memory_id,
        negative.memory_id,
    }
    assert all(item.action == "UNRESOLVED" for item in result.decisions)


def test_mixed_timezone_awareness_does_not_break_ordering() -> None:
    request, analysis = _query()
    older = _memory("Paris", "I live in Paris.", year=2022).model_copy(
        update={
            "recorded_at": datetime(2022, 1, 1),
            "event_time": None,
        }
    )
    older.memory_id = compute_memory_id(older)
    newer = _memory(
        "Rome",
        "I moved to Rome instead and live there now.",
        year=2023,
    )

    result = DeterministicStateResolver().resolve(
        request,
        analysis,
        [older, newer],
    )

    assert [item.action for item in result.decisions] == [
        "EXCLUDE",
        "RETAIN",
    ]


def test_compatible_work_attributes_form_a_transition_chain() -> None:
    request, analysis = _query()
    request = request.model_copy(update={"question": "Where does Ada work now?"})
    analysis = analysis.model_copy(update={"attribute_hints": ["employment"]})
    plan = _memory(
        "start a business",
        "I am starting my own business.",
        year=2022,
    ).model_copy(update={"attribute": "venture_status"})
    active = _memory(
        "my business",
        "I am still working on my business.",
        year=2023,
    ).model_copy(update={"attribute": "current_work"})
    plan.memory_id = compute_memory_id(plan)
    active.memory_id = compute_memory_id(active)

    result = DeterministicStateResolver().resolve(
        request,
        analysis,
        [plan, active],
    )

    assert result.relations
    assert result.relations[0].relation == "UPDATE"
    assert result.relations[0].scope_attribute == "state_transition"
    assert result.effective_memory_ids == [active.memory_id]


def test_business_alias_links_paraphrased_work_updates() -> None:
    request, analysis = _query()
    request = request.model_copy(update={"question": "Where does Ada work now?"})
    analysis = analysis.model_copy(update={"attribute_hints": ["employment"]})
    older = _memory(
        "my biz",
        "I am going to start my biz.",
        year=2022,
        attribute="venture_status",
    )
    newer = _memory(
        "business plans",
        "I am working on business plans.",
        year=2023,
        attribute="current_work",
    )

    result = DeterministicStateResolver().resolve(
        request,
        analysis,
        [older, newer],
    )

    assert any(
        edge.relation == "UPDATE"
        and {edge.from_memory_id, edge.to_memory_id}
        == {older.memory_id, newer.memory_id}
        for edge in result.relations
    )


def test_speaker_prefix_supplies_missing_entity_for_relations() -> None:
    request = QueryRequest(
        query_id="q-work",
        sample_id="conv-auto",
        question="What is Ada working on now?",
    )
    analysis = QueryAnalysis(
        query_id=request.query_id,
        sample_id=request.sample_id,
        query_intent="current_state",
        entity_hints=["Ada"],
        attribute_hints=["current_work"],
        time_mode="latest_known",
    )
    older = _memory(
        "my business",
        "Ada: I am working on my business.",
        year=2022,
        entity=None,
        attribute="current_work",
    )
    newer = _memory(
        "business plans",
        "Ada: I am working on business plans now.",
        year=2023,
        entity=None,
        attribute="current_work",
    )

    result = DeterministicStateResolver().resolve(
        request,
        analysis,
        [older, newer],
    )

    assert result.relations


def test_as_of_uses_the_requested_cutoff_not_an_exact_day() -> None:
    request = QueryRequest(
        query_id="q-as-of",
        sample_id="conv-auto",
        question="Where did Ada live on January 1, 2024?",
        as_of=datetime(2024, 1, 1).date(),
    )
    analysis = QueryAnalysis(
        query_id=request.query_id,
        sample_id=request.sample_id,
        query_intent="historical_state",
        entity_hints=["Ada"],
        attribute_hints=["residence"],
        time_mode="as_of",
        resolved_as_of=request.as_of,
    )
    before = _memory("Paris", "Ada lived in Paris.", year=2023)
    after = _memory("Rome", "Ada moved to Rome.", year=2025)

    result = DeterministicStateResolver().resolve(
        request,
        analysis,
        [before, after],
    )

    assert [item.action for item in result.decisions] == [
        "RETAIN",
        "EXCLUDE",
    ]


def test_state_relevance_filters_wrong_subject_and_unrelated_chatter() -> None:
    request = QueryRequest(
        query_id="q-reading",
        sample_id="conv-auto",
        question="What book is Ada currently reading?",
    )
    analysis = QueryAnalysis(
        query_id=request.query_id,
        sample_id=request.sample_id,
        query_intent="current_state",
        entity_hints=["Ada"],
        attribute_hints=["currently_reading"],
        time_mode="latest_known",
    )
    relevant = _memory(
        "The Lean Startup",
        "Ada: I'm currently reading The Lean Startup.",
        year=2024,
        attribute="currently_reading",
    )
    chatter = _memory(
        "excited",
        "Ada: I'm excited to see where it takes me!",
        year=2024,
        attribute=None,
        dia_id="D2:1",
    )
    wrong_subject = _memory(
        "The Hobbit",
        "Ben: I'm currently reading The Hobbit.",
        year=2024,
        entity="Ben",
        attribute="currently_reading",
        dia_id="D3:1",
    )

    result = DeterministicStateResolver().resolve(
        request,
        analysis,
        [relevant, chatter, wrong_subject],
    )

    assert [item.action for item in result.decisions] == [
        "RETAIN",
        "EXCLUDE",
        "EXCLUDE",
    ]


def test_relevant_source_turn_keeps_its_reviewed_fact_group() -> None:
    request = QueryRequest(
        query_id="q-source-group",
        sample_id="conv-auto",
        question="What book is Ada currently reading?",
    )
    analysis = QueryAnalysis(
        query_id=request.query_id,
        sample_id=request.sample_id,
        query_intent="current_state",
        entity_hints=["Ada"],
        attribute_hints=["currently_reading"],
        time_mode="latest_known",
    )
    first = _memory(
        "The Lean Startup",
        "Ada: I am reading The Lean Startup.",
        year=2024,
        attribute="currently_reading",
    )
    same_turn = _memory(
        "useful",
        "Ada: The book is useful.",
        year=2024,
        attribute=None,
    )

    result = DeterministicStateResolver().resolve(
        request,
        analysis,
        [first, same_turn],
    )

    assert result.effective_memory_ids == [
        first.memory_id,
        same_turn.memory_id,
    ]
