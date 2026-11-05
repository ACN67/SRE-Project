from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from dynamic_memory.contracts import (
    ConversationSample,
    ConversationSession,
    DatasetBundle,
    MemoryRecord,
    QueryRequest,
    SourceRef,
    SourceTurn,
)
from dynamic_memory.contracts.memory_id import compute_memory_id
from dynamic_memory.data.repository import SourceRepository
from dynamic_memory.llm.protocol import LLMMessage
from dynamic_memory.llm.token_estimate import (
    ESTIMATOR_VERSION,
    estimate_messages_tokens,
    estimate_text_tokens,
)
from dynamic_memory.qa.benchmark_protocol import (
    BenchmarkQuestion,
    build_hybrid_context,
    render_question,
)
from dynamic_memory.contracts import QueryAnalysis
from dynamic_memory.reasoning.service_ports import requires_state_resolution


def test_calibrated_estimator_is_far_below_utf8_byte_count():
    text = "Session=session_1 speaker=Sam text=" + ("synthetic evidence " * 1000)
    assert ESTIMATOR_VERSION == "calibrated-unicode-framing-v2"
    assert estimate_text_tokens(text) < len(text.encode("utf-8")) / 2
    assert estimate_messages_tokens((LLMMessage(role="user", content=text),)) > 0


def test_category_policy_is_minimal_and_does_not_include_gold():
    request = QueryRequest(
        query_id="q5",
        sample_id="sample",
        question="Which synthetic event happened?",
    )
    rendered = render_question(
        request,
        BenchmarkQuestion(
            query_id="q5",
            category=5,
            adversarial_answer="A synthetic distractor",
        ),
    )
    assert "Option A:" in rendered and "Option B:" in rendered
    assert "synthetic reference answer" not in rendered.lower()


def test_hybrid_context_contains_fact_turn_and_source_query_metadata():
    sample_id = "synthetic"
    turn = SourceTurn(
        sample_id=sample_id,
        session_id="session_1",
        dia_id="D1:1",
        speaker="Sam",
        text="Sam watched the film.",
        recorded_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    sample = ConversationSample(
        sample_id=sample_id,
        source_order_1_based=1,
        sessions=[ConversationSession(session_id="session_1", turns=[turn])],
    )
    repository = SourceRepository(DatasetBundle(
        snapshot_sha256=hashlib.sha256(b"synthetic").hexdigest(),
        samples=[sample],
        split_by_sample_id={sample_id: "main_dev"},
    ))
    draft = MemoryRecord(
        memory_id="mem_" + "0" * 20,
        sample_id=sample_id,
        content="Sam watched a film.",
        fact_kind="event",
        assertion_type="fact",
        polarity="positive",
        source_refs=[SourceRef(session_id="session_1", dia_id="D1:1")],
        extractor_version="synthetic-v1",
    )
    memory = draft.model_copy(update={"memory_id": compute_memory_id(draft)})
    context = build_hybrid_context(
        [memory],
        repository,
        metadata_lookup=lambda _sample, _ref: {"query": "Synthetic Film Title"},
    )
    assert "Sam watched a film." in context.text
    assert "Sam watched the film." in context.text
    assert "Synthetic Film Title" in context.text
    assert context.source_refs == (SourceRef(session_id="session_1", dia_id="D1:1"),)


def test_dynamic_state_router_only_uses_state_for_state_questions():
    def analysis(intent="general_fact", time_mode="latest_known"):
        return QueryAnalysis(
            query_id="q",
            sample_id="sample",
            query_intent=intent,
            time_mode=time_mode,
        )

    general = QueryRequest(
        query_id="q",
        sample_id="sample",
        question="What instrument does Sam play?",
    )
    state = QueryRequest(
        query_id="q",
        sample_id="sample",
        question="Does Sam still plan to move?",
    )
    assert not requires_state_resolution(general, analysis())
    assert requires_state_resolution(state, analysis("current_state"))
