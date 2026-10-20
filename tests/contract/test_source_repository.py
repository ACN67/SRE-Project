"""Synthetic IC-001 repository contract and isolated real-loader composition."""

from __future__ import annotations

import json

import pytest

from dynamic_memory.contracts import (
    ConversationSample, ConversationSession, DatasetBundle, SourceRef, SourceTurn,
    render_visible_turn,
)
from dynamic_memory.data import SourceRepository, SourceRepositoryError, load_conversations
from dynamic_memory.data.snapshot import freeze_split_config, inspect_snapshot


SHA = "a" * 64


def turn(sample: str, session: str, dia: str, text: str = "synthetic text") -> SourceTurn:
    return SourceTurn(sample_id=sample, session_id=session, dia_id=dia,
                      speaker="Synthetic speaker", text=text)


def sample(sample_id: str, order: int) -> ConversationSample:
    return ConversationSample(sample_id=sample_id, source_order_1_based=order, sessions=[
        ConversationSession(session_id="session_2", turns=[
            turn(sample_id, "session_2", "z", "SECRET_SOURCE_Z"),
            turn(sample_id, "session_2", "a", ""),
        ]),
        ConversationSession(session_id="session_1", turns=[
            SourceTurn(sample_id=sample_id, session_id="session_1", dia_id="z",
                       speaker="Synthetic speaker", text="SECRET_SOURCE_OTHER",
                       recorded_at=None, image_caption="synthetic caption",
                       caption_origin="blip_caption"),
        ]),
    ])


def bundle(*samples: ConversationSample) -> DatasetBundle:
    return DatasetBundle(snapshot_sha256=SHA, samples=list(samples),
                         split_by_sample_id={s.sample_id: "main_dev" for s in samples})


def assert_error(code: str, action, *args):
    with pytest.raises(SourceRepositoryError) as caught:
        action(*args)
    assert caught.value.code == code
    assert "SECRET_" not in str(caught.value)
    assert "synthetic text" not in str(caught.value)


def test_exact_order_lookup_and_shared_pair_scoping():
    repo = SourceRepository(bundle(sample("synthetic_b", 1), sample("synthetic_a", 2)))
    assert repo.list_samples() == ["synthetic_b", "synthetic_a"]
    assert [s.session_id for s in repo.get_sample("synthetic_b").sessions] == ["session_2", "session_1"]
    assert [t.dia_id for t in repo.get_sample("synthetic_b").sessions[0].turns] == ["z", "a"]
    ref = SourceRef(session_id="session_2", dia_id="z")
    assert repo.get_turn("synthetic_b", ref).sample_id == "synthetic_b"
    assert repo.get_turn("synthetic_a", ref).sample_id == "synthetic_a"
    captioned = repo.get_turn("synthetic_b", SourceRef(session_id="session_1", dia_id="z"))
    assert captioned.text == "SECRET_SOURCE_OTHER" and captioned.recorded_at is None
    assert captioned.image_caption == "synthetic caption" and captioned.caption_origin == "blip_caption"
    assert render_visible_turn(captioned) == "SECRET_SOURCE_OTHER [image_caption: synthetic caption]"
    assert repo.get_turn("synthetic_b", SourceRef(session_id="session_2", dia_id="a")).text == ""
    assert render_visible_turn(repo.get_turn("synthetic_b", ref)) == "SECRET_SOURCE_Z"


def test_empty_vs_not_loaded_and_zero_turns():
    unloaded = SourceRepository()
    assert_error("DATA_NOT_LOADED", unloaded.list_samples)
    assert_error("DATA_NOT_LOADED", unloaded.get_sample, "synthetic")
    assert_error("DATA_NOT_LOADED", unloaded.get_turn, "synthetic", SourceRef(session_id="s", dia_id="d"))
    assert SourceRepository(bundle()).list_samples() == []
    empty = ConversationSample(sample_id="synthetic_empty", source_order_1_based=1, sessions=[
        ConversationSession(session_id="session_1", turns=[]),
    ])
    repo = SourceRepository(bundle(empty))
    assert repo.list_samples() == ["synthetic_empty"]
    assert repo.get_sample("synthetic_empty").sessions[0].turns == []
    assert_error("SOURCE_NOT_FOUND", repo.get_turn, "synthetic_empty", SourceRef(session_id="session_1", dia_id="d"))


def test_missing_and_cross_sample_priority():
    only_a = ConversationSample(sample_id="synthetic_a", source_order_1_based=1, sessions=[
        ConversationSession(session_id="session_1", turns=[turn("synthetic_a", "session_1", "one")]),
    ])
    only_b = ConversationSample(sample_id="synthetic_b", source_order_1_based=2, sessions=[
        ConversationSession(session_id="session_2", turns=[turn("synthetic_b", "session_2", "two")]),
    ])
    repo = SourceRepository(bundle(only_a, only_b))
    assert_error("SAMPLE_NOT_FOUND", repo.get_sample, "ghost")
    assert_error("SAMPLE_NOT_FOUND", repo.get_turn, "ghost", SourceRef(session_id="session_2", dia_id="two"))
    assert_error("SOURCE_NOT_FOUND", repo.get_turn, "synthetic_a", SourceRef(session_id="absent", dia_id="one"))
    assert_error("SOURCE_NOT_FOUND", repo.get_turn, "synthetic_a", SourceRef(session_id="session_1", dia_id="absent"))
    assert_error("SAMPLE_MISMATCH", repo.get_turn, "synthetic_a", SourceRef(session_id="session_2", dia_id="two"))


@pytest.mark.parametrize("bad,code", [
    ("sample_duplicate", "DATA_FORMAT_INVALID"),
    ("session_duplicate", "DATA_FORMAT_INVALID"),
    ("turn_duplicate", "DUPLICATE_SOURCE_REF"),
    ("turn_sample", "SAMPLE_MISMATCH"),
    ("turn_session", "DATA_FORMAT_INVALID"),
    ("turn_dia_blank", "DATA_FORMAT_INVALID"),
    ("sample_order", "DATA_FORMAT_INVALID"),
    ("nested_type", "DATA_FORMAT_INVALID"),
    ("bad_version", "DATA_FORMAT_INVALID"),
])
def test_model_copy_corruption_rejected_before_publication(bad, code):
    original = bundle(sample("synthetic_a", 1))
    s = original.samples[0]
    session = s.sessions[0]
    first = session.turns[0]
    if bad == "sample_duplicate":
        original = original.model_copy(update={"samples": [s, s]})
    elif bad == "session_duplicate":
        original = original.model_copy(update={"samples": [s.model_copy(update={"sessions": [session, session]})]})
    elif bad == "turn_duplicate":
        duplicate = session.model_copy(update={"turns": [first, first]})
        original = original.model_copy(update={"samples": [s.model_copy(update={"sessions": [duplicate]})]})
    elif bad in {"turn_sample", "turn_session", "turn_dia_blank"}:
        key, value = {"turn_sample": ("sample_id", "synthetic_b"),
                      "turn_session": ("session_id", "wrong"),
                      "turn_dia_blank": ("dia_id", " ")}[bad]
        changed = first.model_copy(update={key: value})
        changed_session = session.model_copy(update={"turns": [changed]})
        original = original.model_copy(update={"samples": [s.model_copy(update={"sessions": [changed_session]})]})
    elif bad == "sample_order":
        original = original.model_copy(update={"samples": [s.model_copy(update={"source_order_1_based": 0})]})
    elif bad == "nested_type":
        original = original.model_copy(update={"samples": ["SECRET_GOLD_BAD"]})
    else:
        original = original.model_copy(update={"schema_version": "SECRET_GOLD_BAD"})
    assert_error(code, SourceRepository, original)


def test_noncontiguous_original_source_orders_preserved():
    single = SourceRepository(bundle(sample("synthetic_test", 9)))
    assert single.list_samples() == ["synthetic_test"]
    assert single.get_sample("synthetic_test").source_order_1_based == 9
    assert single.get_turn("synthetic_test", SourceRef(session_id="session_2", dia_id="z")).sample_id == "synthetic_test"

    subset = SourceRepository(bundle(sample("synthetic_extra_a", 5), sample("synthetic_extra_b", 6)))
    assert subset.list_samples() == ["synthetic_extra_a", "synthetic_extra_b"]
    assert [subset.get_sample(sid).source_order_1_based for sid in subset.list_samples()] == [5, 6]
    assert subset.get_turn("synthetic_extra_b", SourceRef(session_id="session_1", dia_id="z")).sample_id == "synthetic_extra_b"


def test_reject_duplicate_or_malformed_original_source_order():
    first = sample("synthetic_a", 5)
    second = sample("synthetic_b", 6)
    repeated = second.model_copy(update={"source_order_1_based": 5})
    assert_error("DATA_FORMAT_INVALID", SourceRepository, bundle(first, repeated))
    for invalid in (0, -1, True, "9"):
        damaged = first.model_copy(update={"source_order_1_based": invalid})
        assert_error("DATA_FORMAT_INVALID", SourceRepository, bundle(damaged))


def test_argument_format_and_mutation_isolation():
    source = bundle(sample("synthetic_a", 1))
    repo = SourceRepository(source)
    ref = SourceRef(session_id="session_2", dia_id="z")
    source.samples[0].sessions[0].turns[0].text = "changed original"
    source.samples.clear()
    returned_sample = repo.get_sample("synthetic_a")
    returned_sample.sessions[0].turns[0].sample_id = "wrong"
    returned_sample.sessions[0].turns[0].text = "changed returned sample"
    returned_turn = repo.get_turn("synthetic_a", ref)
    returned_turn.dia_id = "wrong"
    returned_turn.text = "changed returned turn"
    assert repo.list_samples() == ["synthetic_a"]
    assert repo.get_turn("synthetic_a", ref).text == "SECRET_SOURCE_Z"
    assert repo.get_turn("synthetic_a", ref).dia_id == "z"
    assert repo.get_sample("synthetic_a").sessions[0].turns[0].sample_id == "synthetic_a"
    assert_error("DATA_FORMAT_INVALID", repo.get_sample, " ")
    assert_error("DATA_FORMAT_INVALID", repo.get_turn, "synthetic_a", None)
    assert_error("DATA_FORMAT_INVALID", repo.get_turn, "synthetic_a", SourceRef(session_id=" ", dia_id="z"))
    assert_error("DATA_FORMAT_INVALID", repo.get_turn, "synthetic_a", ref.model_copy(update={"dia_id": " "}))


def test_gold_canary_not_in_repository_error_or_extra_payload():
    source = bundle(sample("synthetic_a", 1))
    damaged = source.model_copy(update={"SECRET_GOLD_extra": "SECRET_GOLD_PAYLOAD"})
    assert_error("DATA_FORMAT_INVALID", SourceRepository, damaged)
    repo = SourceRepository(source)
    assert "SECRET_GOLD" not in repo.get_sample("synthetic_a").model_dump_json()
    assert_error("DATA_FORMAT_INVALID", repo.get_turn, "synthetic_a", {"session_id": "SECRET_GOLD_REF"})


def test_loader_bundle_is_direct_repository_input(tmp_path):
    raw = tmp_path / "synthetic10.json"
    rows = [{"sample_id": f"synthetic-{i}", "conversation": {
        "session_1": [{"speaker": "A", "dia_id": "d1", "text": f"synthetic turn {i}"}],
    }} for i in range(10)]
    raw.write_text(json.dumps(rows), encoding="utf-8")
    split = tmp_path / "data_split.yaml"
    split.write_text(
        "snapshot_sha256: null\nsample_ids: null\ngroups:\n"
        "  main_dev: {source_order_1_based: [1, 2, 3, 4], sample_ids: null}\n"
        "  extra_dev: {source_order_1_based: [5, 6, 7, 8], sample_ids: null}\n"
        "  test: {source_order_1_based: [9, 10], sample_ids: null}\n", encoding="utf-8")
    freeze_split_config(inspect_snapshot(raw), split, apply=True)
    loaded = load_conversations(str(raw), str(split))
    repo = SourceRepository(loaded)
    assert repo.list_samples() == [f"synthetic-{i}" for i in range(10)]
    assert repo.get_sample("synthetic-9").source_order_1_based == 10
    assert repo.get_turn("synthetic-9", SourceRef(session_id="session_1", dia_id="d1")).text == "synthetic turn 9"
    assert list(loaded.split_by_sample_id.values()) == ["main_dev"] * 4 + ["extra_dev"] * 4 + ["test"] * 2
