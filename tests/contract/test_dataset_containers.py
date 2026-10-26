"""Synthetic IC-001 container contract; no real conversation or Gold is loaded."""

from __future__ import annotations

from copy import deepcopy
import pytest
from pydantic import ValidationError

from dynamic_memory.contracts import (
    ConversationSample, ConversationSession, DatasetBundle, SourceTurn, render_visible_turn,
)
from dynamic_memory.contracts.models import SCHEMA_VERSION


SNAPSHOT = "a" * 64  # synthetic digest, not a frozen dataset snapshot


def turn(sample="synthetic_a", session="session_2", dia="dia_2", *, text="Synthetic text.", **extra):
    return SourceTurn(sample_id=sample, session_id=session, dia_id=dia,
                      speaker="Synthetic speaker", text=text, **extra)


def sample(sample_id="synthetic_a", order=1):
    return ConversationSample(
        sample_id=sample_id, source_order_1_based=order,
        sessions=[
            ConversationSession(session_id="session_2", turns=[
                turn(sample_id, "session_2", "dia_2"),
                turn(sample_id, "session_2", "dia_1", text=""),
            ]),
            ConversationSession(session_id="session_1", turns=[
                turn(sample_id, "session_1", "dia_2", recorded_at=None,
                     image_caption="Synthetic caption.", caption_origin="blip_caption"),
            ]),
        ],
    )


def bundle(*samples):
    return DatasetBundle(snapshot_sha256=SNAPSHOT, samples=list(samples),
                         split_by_sample_id={item.sample_id: "main_dev" for item in samples})


def test_public_exports_schema_and_order_preserved_across_roundtrip():
    original = bundle(sample(), sample("synthetic_b", 2))
    assert original.schema_version == SCHEMA_VERSION == "2.1"
    assert original.samples[0].schema_version == "2.1"
    assert [s.sample_id for s in original.samples] == ["synthetic_a", "synthetic_b"]
    assert [s.session_id for s in original.samples[0].sessions] == ["session_2", "session_1"]
    assert [t.dia_id for t in original.samples[0].sessions[0].turns] == ["dia_2", "dia_1"]
    assert original.samples[0].sessions[0].turns[1].text == ""
    assert original.samples[0].sessions[0].turns[1].recorded_at is None
    assert DatasetBundle.model_validate_json(original.model_dump_json()) == original
    assert isinstance(original.samples[0].sessions[0].turns[0], SourceTurn)
    assert original.model_dump()["samples"][0]["source_order_1_based"] == 1


def test_empty_containers_are_structurally_valid_without_inventing_data():
    empty = DatasetBundle(snapshot_sha256=SNAPSHOT, samples=[], split_by_sample_id={})
    assert empty.samples == []
    item = ConversationSample(sample_id="synthetic_empty", source_order_1_based=1,
                              sessions=[ConversationSession(session_id="session_empty", turns=[])])
    assert bundle(item).samples[0].sessions[0].turns == []


def test_source_identity_is_session_scoped_and_parent_bound():
    item = sample()
    assert item.sessions[0].turns[0].dia_id == item.sessions[1].turns[0].dia_id
    with pytest.raises(ValidationError, match="duplicate dia_id"):
        ConversationSession(session_id="session_2", turns=[turn(), turn()])
    with pytest.raises(ValidationError, match="turn session_id"):
        ConversationSession(session_id="session_1", turns=[turn(session="session_2")])
    with pytest.raises(ValidationError, match="turn sample_id"):
        ConversationSample(sample_id="synthetic_b", source_order_1_based=1,
                           sessions=[ConversationSession(session_id="session_2", turns=[turn()])])
    with pytest.raises(ValidationError, match="duplicate session_id"):
        ConversationSample(sample_id="synthetic_a", source_order_1_based=1,
                           sessions=[item.sessions[0], item.sessions[0]])


def test_bundle_rejects_duplicates_and_incomplete_split():
    first = sample()
    with pytest.raises(ValidationError, match="duplicate sample_id"):
        bundle(first, first)
    with pytest.raises(ValidationError, match="split_by_sample_id"):
        DatasetBundle(snapshot_sha256=SNAPSHOT, samples=[first], split_by_sample_id={})
    with pytest.raises(ValidationError, match="split_by_sample_id"):
        DatasetBundle(snapshot_sha256=SNAPSHOT, samples=[], split_by_sample_id={"ghost": "test"})
    with pytest.raises(ValidationError):
        DatasetBundle(snapshot_sha256=SNAPSHOT, samples=[first],
                      split_by_sample_id={"synthetic_a": "unknown"})


@pytest.mark.parametrize("field, value", [
    ("sample_id", ""), ("sample_id", "   "), ("sample_id", 123),
    ("source_order_1_based", 0), ("source_order_1_based", True),
    ("source_order_1_based", "1"),
])
def test_sample_invalid_identity_and_order(field, value):
    kwargs = {"sample_id": "synthetic_a", "source_order_1_based": 1, "sessions": []}
    kwargs[field] = value
    with pytest.raises(ValidationError):
        ConversationSample(**kwargs)


def test_turn_identity_and_caption_rules_without_rewriting_original_text():
    source = turn(text="", recorded_at=None, image_caption="Synthetic caption.",
                  caption_origin="blip_caption")
    session = ConversationSession(session_id="session_2", turns=[source])
    assert session.turns[0].text == ""
    assert render_visible_turn(source) == " [image_caption: Synthetic caption.]"
    assert source.recorded_at is None
    with pytest.raises(ValidationError, match="caption_origin"):
        ConversationSession(session_id="session_2", turns=[turn(
            image_caption="Synthetic caption.", caption_origin="unverified")])
    with pytest.raises(ValidationError, match="turn source identity"):
        ConversationSession(session_id="session_2", turns=[turn(sample=" ")])
    with pytest.raises(ValidationError):
        turn(image_caption="Synthetic caption.")


@pytest.mark.parametrize("name", [
    "qa", "answer", "evidence", "observation", "session_summary", "event_summary",
    "raw_json", "metadata", "extras",
])
def test_gold_and_opaque_fields_rejected_at_every_container_level(name):
    secret = "SECRET_GOLD"
    for cls, payload in (
        (ConversationSession, {"session_id": "session_1", "turns": []}),
        (ConversationSample, {"sample_id": "synthetic_a", "source_order_1_based": 1, "sessions": []}),
        (DatasetBundle, {"snapshot_sha256": SNAPSHOT, "samples": [], "split_by_sample_id": {}}),
    ):
        with pytest.raises(ValidationError):
            cls.model_validate({**payload, name: secret})
    assert secret not in bundle(sample()).model_dump_json()


def test_nested_gold_and_malformed_input_rejected():
    payload = bundle(sample()).model_dump()
    for path in (("samples", 0), ("samples", 0, "sessions", 0),
                 ("samples", 0, "sessions", 0, "turns", 0)):
        changed = deepcopy(payload)
        target = changed
        for component in path:
            target = target[component]
        target["qa"] = {"answer": "SECRET_GOLD"}
        with pytest.raises(ValidationError):
            DatasetBundle.model_validate(changed)
    changed = deepcopy(payload)
    changed["samples"][0]["sessions"][0]["turns"][0]["text"] = None
    with pytest.raises(ValidationError):
        DatasetBundle.model_validate(changed)
    with pytest.raises(ValidationError):
        DatasetBundle.model_validate({**payload, "samples": "not a list"})


def test_schema_snapshot_and_nested_type_validation():
    payload = bundle(sample()).model_dump()
    for changed in (
        {**payload, "schema_version": "2.2"},
        {**payload, "snapshot_sha256": "not-a-digest"},
        {**payload, "samples": [{**payload["samples"][0], "schema_version": "2.2"}]},
        {**payload, "samples": ["not a sample"]},
    ):
        with pytest.raises(ValidationError):
            DatasetBundle.model_validate(changed)
    assert DatasetBundle.model_validate(payload).snapshot_sha256 == SNAPSHOT


@pytest.mark.parametrize("container", ["session", "sample", "bundle", "nested_turn"])
def test_rejected_gold_input_is_hidden_in_validation_error_text(container):
    secret = "SECRET_GOLD_SYNTHETIC_SENTINEL"
    if container == "session":
        validate = lambda: ConversationSession.model_validate({
            "session_id": "session_1", "turns": [], "qa": {"answer": secret},
        })
    elif container == "sample":
        validate = lambda: ConversationSample.model_validate({
            "sample_id": "synthetic_a", "source_order_1_based": 1,
            "sessions": [], "observation": secret,
        })
    elif container == "bundle":
        validate = lambda: DatasetBundle.model_validate({
            "snapshot_sha256": SNAPSHOT, "samples": [],
            "split_by_sample_id": {}, "event_summary": secret,
        })
    else:
        payload = bundle(sample()).model_dump()
        payload["samples"][0]["sessions"][0]["turns"][0]["qa"] = {"answer": secret}
        validate = lambda: DatasetBundle.model_validate(payload)

    with pytest.raises(ValidationError) as exc:
        validate()

    assert secret not in str(exc.value)
    assert secret not in str(exc.value.errors(include_input=False))
