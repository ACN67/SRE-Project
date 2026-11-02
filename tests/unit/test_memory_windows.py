"""Synthetic behavior checks for B-04 extraction window planning."""

import json

import pytest

from dynamic_memory.contracts import ConversationSample, ConversationSession, SourceRef, SourceTurn
from dynamic_memory.memory.windows import WindowPlanningError, plan_extraction_windows
from dynamic_memory.pipeline.settings import load_settings


def _turn(dia_id: str, text: str, *, session_id: str = "session_1", caption: str | None = None) -> SourceTurn:
    return SourceTurn(
        sample_id="synthetic_sample", session_id=session_id, dia_id=dia_id,
        speaker="Ada", text=text, image_caption=caption,
        caption_origin="blip_caption" if caption else None,
    )


def _sample(*sessions: ConversationSession) -> ConversationSample:
    return ConversationSample(
        sample_id="synthetic_sample", source_order_1_based=1, sessions=list(sessions),
    )


def test_short_session_is_one_traceable_window() -> None:
    first = _turn("d1", "Ada joined the lab.", caption="a blue room")
    second = _turn("d2", "She met Ben.")
    sample = _sample(ConversationSession(session_id="session_1", turns=[first, second]))

    windows = plan_extraction_windows(sample, load_settings().extraction)

    assert len(windows) == 1
    window = windows[0]
    assert (window.sample_id, window.session_id) == ("synthetic_sample", "session_1")
    assert window.source_refs == (
        SourceRef(session_id="session_1", dia_id="d1"),
        SourceRef(session_id="session_1", dia_id="d2"),
    )
    assert window.visible_turns == ("Ada joined the lab. [image_caption: a blue room]", "She met Ben.")
    assert first.text == "Ada joined the lab."
    assert "Ada joined the lab." not in repr(window)
    assert window.extraction_window_id.startswith("window_")
    assert window.estimated_total_tokens <= window.max_input_tokens == 4096
    payload = json.loads(window.input_text)
    assert set(payload) == {"sample_id", "session_id", "turns"}
    assert set(payload["turns"][0]) == {"source_ref", "speaker", "recorded_at", "visible_text"}
    assert payload["turns"][0]["source_ref"] == {"session_id": "session_1", "dia_id": "d1"}
    assert payload["turns"][0]["visible_text"] == window.visible_turns[0]
    assert window.estimated_payload_tokens > len("".join(window.visible_turns))
    ref = window.source_refs[0]
    ref.dia_id = "changed"
    assert window.source_refs[0].dia_id == "d1"


def test_long_session_splits_with_two_turn_overlap_and_complete_coverage() -> None:
    session = ConversationSession(
        session_id="session_1",
        turns=[_turn(f"d{i}", f"fact {i}") for i in range(1, 6)],
    )
    settings = load_settings().extraction.model_copy(update={"window_max_input_tokens": 40})

    windows = plan_extraction_windows(
        _sample(session), settings,
        token_estimator=lambda content: 5 + 10 * content.count('"source_ref"'),
        estimator_version="synthetic-count-v1", reserved_prompt_tokens=5,
    )

    assert [[ref.dia_id for ref in window.source_refs] for window in windows] == [
        ["d1", "d2", "d3"], ["d2", "d3", "d4"], ["d3", "d4", "d5"],
    ]
    assert all(window.estimated_total_tokens <= 40 for window in windows)
    assert {ref.dia_id for window in windows for ref in window.source_refs} == {
        "d1", "d2", "d3", "d4", "d5",
    }


def test_session_isolation_empty_inputs_and_stable_content_bound_identity() -> None:
    first = ConversationSession(session_id="session_1", turns=[_turn("d1", "fact one")])
    empty = ConversationSession(session_id="session_2", turns=[])
    last = ConversationSession(session_id="session_3", turns=[
        _turn("d1", "fact two", session_id="session_3"),
    ])
    settings = load_settings().extraction
    sample = _sample(first, empty, last)

    windows = plan_extraction_windows(sample, settings)
    repeated = plan_extraction_windows(sample, settings)
    assert [w.session_id for w in windows] == ["session_1", "session_3"]
    assert [w.extraction_window_id for w in windows] == [w.extraction_window_id for w in repeated]
    assert windows[0].extraction_window_id != windows[1].extraction_window_id
    assert plan_extraction_windows(_sample(empty), settings) == []
    assert plan_extraction_windows(_sample(), settings) == []

    changed = _sample(first, empty, ConversationSession(session_id="session_3", turns=[
        _turn("d1", "changed fact", session_id="session_3"),
    ]))
    modified = plan_extraction_windows(changed, settings)
    assert modified[0].extraction_window_id == windows[0].extraction_window_id
    assert modified[1].extraction_window_id != windows[1].extraction_window_id
    alternate = plan_extraction_windows(sample, settings.model_copy(update={"window_overlap_turns": 1}))
    assert alternate[0].extraction_window_id != windows[0].extraction_window_id
    changed_ref = _sample(ConversationSession(session_id="session_1", turns=[_turn("renamed", "fact one")]))
    assert plan_extraction_windows(changed_ref, settings)[0].extraction_window_id != windows[0].extraction_window_id


def test_zero_overlap_covers_each_turn_once() -> None:
    sample = _sample(ConversationSession(session_id="session_1", turns=[
        _turn(f"d{i}", f"fact {i}") for i in range(1, 5)
    ]))
    settings = load_settings().extraction.model_copy(update={
        "window_max_input_tokens": 30, "window_overlap_turns": 0,
    })
    windows = plan_extraction_windows(
        sample, settings, token_estimator=lambda text: 5 + 10 * text.count('"source_ref"'),
        estimator_version="synthetic-count-v1", reserved_prompt_tokens=5,
    )
    assert [ref.dia_id for window in windows for ref in window.source_refs] == [
        "d1", "d2", "d3", "d4",
    ]


def test_rejects_bypassed_sample_validation_and_missing_estimator_version() -> None:
    sample = _sample(ConversationSession(session_id="session_1", turns=[_turn("d1", "fact")]))
    damaged = sample.model_copy(update={"sample_id": ""})
    with pytest.raises(WindowPlanningError) as error:
        plan_extraction_windows(damaged, load_settings().extraction)
    assert error.value.code == "INVALID_SAMPLE_OR_SETTINGS"
    with pytest.raises(WindowPlanningError) as error:
        plan_extraction_windows(sample, load_settings().extraction, token_estimator=lambda text: 1)
    assert error.value.code == "TOKEN_ESTIMATE_FAILED"


def test_exact_budget_is_allowed_but_one_oversized_turn_is_rejected() -> None:
    sample = _sample(ConversationSession(session_id="session_1", turns=[_turn("d1", "secret_literal")]))
    settings = load_settings().extraction.model_copy(update={"window_max_input_tokens": 20})
    estimator = lambda content: 15
    exact = plan_extraction_windows(
        sample, settings, token_estimator=estimator,
        estimator_version="synthetic-fixed-v1", reserved_prompt_tokens=5,
    )
    assert exact[0].estimated_total_tokens == 20

    with pytest.raises(WindowPlanningError) as error:
        plan_extraction_windows(sample, settings, reserved_prompt_tokens=5)
    assert error.value.code == "WINDOW_INPUT_EXCEEDS_BUDGET"
    assert "secret_literal" not in str(error.value)


def test_overlap_that_cannot_advance_fails_instead_of_looping() -> None:
    sample = _sample(ConversationSession(session_id="session_1", turns=[
        _turn("d1", "one"), _turn("d2", "two"), _turn("d3", "three"),
    ]))
    settings = load_settings().extraction.model_copy(update={"window_max_input_tokens": 30})
    with pytest.raises(WindowPlanningError) as error:
        plan_extraction_windows(
            sample, settings, token_estimator=lambda text: 5 + 10 * text.count('"source_ref"'),
            estimator_version="synthetic-count-v1", reserved_prompt_tokens=5,
        )
    assert error.value.code == "WINDOW_OVERLAP_NO_PROGRESS"


def test_overlap_that_fits_but_blocks_the_next_new_turn_is_diagnosed() -> None:
    sample = _sample(ConversationSession(session_id="session_1", turns=[
        _turn("d1", "fact one"), _turn("d2", "fact two"),
        _turn("d3", "fact three"), _turn("d4", "fact four"),
    ]))
    settings = load_settings().extraction.model_copy(update={"window_max_input_tokens": 40})
    with pytest.raises(WindowPlanningError) as error:
        plan_extraction_windows(
            sample, settings,
            token_estimator=lambda text: 5 + 10 * text.count('"source_ref"') + (20 if "fact four" in text else 0),
            estimator_version="synthetic-weighted-v1", reserved_prompt_tokens=5,
        )
    assert error.value.code == "WINDOW_OVERLAP_NO_PROGRESS"


@pytest.mark.parametrize("estimate", [None, -1, True, 1.5])
def test_invalid_token_estimate_is_diagnosed(estimate: object) -> None:
    sample = _sample(ConversationSession(session_id="session_1", turns=[_turn("d1", "private text")]))
    with pytest.raises(WindowPlanningError) as error:
        plan_extraction_windows(
            sample, load_settings().extraction, token_estimator=lambda text: estimate,
            estimator_version="synthetic-invalid-v1",
        )
    assert error.value.code == "TOKEN_ESTIMATE_FAILED"
    assert "private text" not in str(error.value)


def test_token_estimator_exception_and_missing_reserve_are_diagnosed() -> None:
    sample = _sample(ConversationSession(session_id="session_1", turns=[_turn("d1", "private text")]))

    def broken_estimator(_: str) -> int:
        raise RuntimeError("private text")

    with pytest.raises(WindowPlanningError) as error:
        plan_extraction_windows(
            sample, load_settings().extraction, token_estimator=broken_estimator,
            estimator_version="synthetic-broken-v1",
        )
    assert error.value.code == "TOKEN_ESTIMATE_FAILED"
    assert "private text" not in str(error.value)
    with pytest.raises(WindowPlanningError) as error:
        plan_extraction_windows(sample, load_settings().extraction, reserved_prompt_tokens=4096)
    assert error.value.code == "WINDOW_BUDGET_INVALID"
