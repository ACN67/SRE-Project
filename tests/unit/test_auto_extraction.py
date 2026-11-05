"""Deterministic extraction keeps quote provenance and needs no network."""

from __future__ import annotations

from datetime import datetime, timezone

from dynamic_memory.contracts import (
    ConversationSample,
    ConversationSession,
    SourceTurn,
)
from dynamic_memory.memory.auto_extraction import (
    AUTO_EXTRACTOR_VERSION,
    _extract_facts_payload,
    create_auto_extraction_runtime,
    extract_window_auto,
)
from dynamic_memory.memory.extraction import MEMORY_DRAFT_PROMPT, extract_window
from dynamic_memory.memory.windows import plan_extraction_windows
from dynamic_memory.pipeline.settings import load_settings


def _sample() -> ConversationSample:
    return ConversationSample(
        sample_id="sample_auto",
        source_order_1_based=1,
        sessions=[
            ConversationSession(
                session_id="session_1",
                turns=[
                    SourceTurn(
                        sample_id="sample_auto",
                        session_id="session_1",
                        dia_id="D1:1",
                        speaker="Ada",
                        text="I moved to Paris in July 2024. I plan to visit Rome next month.",
                        recorded_at=datetime(
                            2024,
                            7,
                            20,
                            9,
                            0,
                            tzinfo=timezone.utc,
                        ),
                    ),
                    SourceTurn(
                        sample_id="sample_auto",
                        session_id="session_1",
                        dia_id="D1:2",
                        speaker="Ben",
                        text="Ada loves classical music.",
                        recorded_at=datetime(
                            2024,
                            7,
                            20,
                            9,
                            1,
                            tzinfo=timezone.utc,
                        ),
                    ),
                ],
            )
        ],
    )


def test_deterministic_extraction_preserves_claims_and_sources() -> None:
    settings = load_settings()
    window = plan_extraction_windows(
        _sample(),
        settings.extraction,
        reserved_prompt_tokens=1800,
    )[0]
    runtime = create_auto_extraction_runtime(settings)
    prepared = runtime.make_request(
        MEMORY_DRAFT_PROMPT,
        {"conversation": window.input_text},
    )
    identity = runtime.make_identity(
        prepared,
        sample_id=window.sample_id,
        scope_id=window.extraction_window_id,
        run_kind="development",
        method=None,
        data_fingerprint="sha256:" + "1" * 64,
        schema_id="atomic_fact_draft",
        schema_version="1",
        retriever_fingerprint=None,
        query_analysis_version=None,
        git_sha="0" * 40,
    )
    result = extract_window(
        window,
        runtime.recorder,
        identity,
        runtime.settings.models,
    )

    assert AUTO_EXTRACTOR_VERSION == "auto-utterance-v10-state-attributes"
    assert len(result.facts) == 3
    assert result.facts[0].content == "Ada: I moved to Paris in July 2024."
    assert result.facts[0].entity == "Ada"
    assert result.facts[0].attribute == "residence"
    assert result.facts[0].event_time is not None
    assert result.facts[0].event_time.start.year == 2024
    assert result.facts[0].event_time.start.month == 7
    assert result.facts[1].assertion_type == "plan"
    assert result.facts[2].entity == "Ada"
    assert all(
        fact.source_refs[0].dia_id in {"D1:1", "D1:2"}
        for fact in result.facts
    )


def test_direct_auto_extraction_does_not_need_runtime() -> None:
    settings = load_settings()
    window = plan_extraction_windows(
        _sample(),
        settings.extraction,
        reserved_prompt_tokens=1800,
    )[0]
    first = extract_window_auto(window)
    second = extract_window_auto(window)
    assert first.facts == second.facts
    assert first.cache_status == "auto"


def test_sentence_opener_is_not_misclassified_as_an_entity() -> None:
    facts = _extract_facts_payload(
        {
            "turns": [
                {
                    "speaker": "Ada",
                    "visible_text": "Started a new project yesterday.",
                    "recorded_at": "2024-07-20T09:00:00+00:00",
                    "source_ref": {
                        "session_id": "session_1",
                        "dia_id": "D1:1",
                    },
                }
            ]
        }
    )

    assert len(facts) == 1
    assert facts[0].content == "Ada: Started a new project yesterday."
    assert facts[0].entity is None


def test_greetings_do_not_become_memories() -> None:
    facts = _extract_facts_payload(
        {
            "turns": [
                {
                    "speaker": "Ada",
                    "visible_text": "Hey Ben! Thanks, Ada. Good luck!",
                    "recorded_at": "2024-07-20T09:00:00+00:00",
                    "source_ref": {
                        "session_id": "session_1",
                        "dia_id": "D1:1",
                    },
                }
            ]
        }
    )

    assert "Ada: Good luck!" in {fact.content for fact in facts}
    assert all(
        not fact.content.endswith("Hey Ben!") and
        not fact.content.endswith("Thanks, Ada.")
        for fact in facts
    )


def test_thanks_for_asking_does_not_become_a_memory() -> None:
    facts = _extract_facts_payload(
        {
            "turns": [
                {
                    "speaker": "Ada",
                    "visible_text": "Thanks for askin', Ben!",
                    "recorded_at": "2024-07-20T09:00:00",
                    "source_ref": {
                        "session_id": "session_1",
                        "dia_id": "D1:1",
                    },
                }
            ]
        }
    )

    assert facts == ()


def test_naive_session_time_anchors_relative_dates() -> None:
    facts = _extract_facts_payload(
        {
            "turns": [
                {
                    "speaker": "Ada",
                    "visible_text": (
                        "I went to the museum yesterday. "
                        "I ran a race last Saturday."
                    ),
                    "recorded_at": "2024-07-20T09:00:00",
                    "source_ref": {
                        "session_id": "session_1",
                        "dia_id": "D1:1",
                    },
                }
            ]
        }
    )

    assert facts[0].event_time is not None
    assert facts[0].event_time.start.date().isoformat() == "2024-07-19"
    assert facts[1].event_time is not None
    assert facts[1].event_time.start.date().isoformat() == "2024-07-13"


def test_explicit_ongoing_state_takes_precedence_over_secondary_hope() -> None:
    facts = _extract_facts_payload(
        {
            "turns": [
                {
                    "speaker": "Ada",
                    "visible_text": (
                        "I'm currently reading The Lean Startup and hoping "
                        "it will help my business."
                    ),
                    "recorded_at": "2024-07-20T09:00:00",
                    "source_ref": {
                        "session_id": "session_1",
                        "dia_id": "D1:1",
                    },
                }
            ]
        }
    )

    assert len(facts) == 1
    assert facts[0].assertion_type == "fact"
    assert facts[0].attribute == "currently_reading"


def test_state_attributes_distinguish_games_and_achievements() -> None:
    facts = _extract_facts_payload(
        {
            "turns": [
                {
                    "speaker": "Ada",
                    "visible_text": (
                        "I'm currently playing Zelda. "
                        "I won my first video game tournament last week."
                    ),
                    "recorded_at": "2024-07-20T09:00:00",
                    "source_ref": {
                        "session_id": "session_1",
                        "dia_id": "D1:1",
                    },
                }
            ]
        }
    )

    assert [fact.attribute for fact in facts] == [
        "current_game",
        "achievement",
    ]


def test_entity_uses_first_mentioned_conversation_participant() -> None:
    facts = _extract_facts_payload(
        {
            "turns": [
                {
                    "speaker": "Ada",
                    "visible_text": "Ada lives in Paris.",
                    "recorded_at": "2024-07-20T09:00:00",
                    "source_ref": {
                        "session_id": "session_1",
                        "dia_id": "D1:1",
                    },
                },
                {
                    "speaker": "Ben",
                    "visible_text": "Ben told Ada about Rome.",
                    "recorded_at": "2024-07-20T09:01:00",
                    "source_ref": {
                        "session_id": "session_1",
                        "dia_id": "D1:2",
                    },
                },
            ]
        }
    )

    assert facts[0].entity == "Ada"
    assert facts[1].entity == "Ben"


def test_state_fields_use_stable_history_categories() -> None:
    facts = _extract_facts_payload(
        {
            "turns": [
                {
                    "speaker": "Ada",
                    "visible_text": (
                        "I am working on a focused business. "
                        "I am searching for investors. "
                        "I am learning analytics."
                    ),
                    "recorded_at": "2024-07-20T09:00:00+00:00",
                    "source_ref": {
                        "session_id": "session_1",
                        "dia_id": "D1:1",
                    },
                }
            ]
        }
    )

    assert [fact.attribute for fact in facts] == [
        "current_work",
        "search_target",
        "skill_learning",
    ]


def test_greeting_filter_keeps_first_person_claims() -> None:
    facts = _extract_facts_payload(
        {
            "turns": [
                {
                    "speaker": "Ada",
                    "visible_text": (
                        "Yeah, I play clarinet. Thanks, Ben!"
                    ),
                    "recorded_at": "2024-07-20T09:00:00+00:00",
                    "source_ref": {
                        "session_id": "session_1",
                        "dia_id": "D1:1",
                    },
                }
            ]
        }
    )

    assert len(facts) == 1
    assert "I play clarinet" in facts[0].content


def test_currently_planning_remains_a_plan() -> None:
    facts = _extract_facts_payload(
        {
            "turns": [
                {
                    "speaker": "Maria",
                    "visible_text": (
                        "I'm currently planning a fundraiser this month."
                    ),
                    "recorded_at": "2024-07-20T09:00:00+00:00",
                    "source_ref": {
                        "session_id": "session_1",
                        "dia_id": "D1:1",
                    },
                }
            ]
        }
    )

    assert len(facts) == 1
    assert facts[0].assertion_type == "plan"
