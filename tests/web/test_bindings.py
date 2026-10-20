import sys
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from dynamic_memory.contracts.memory_id import validate_memory_id
from dynamic_memory.contracts.models import SourceRef, render_visible_turn
from dynamic_memory.web.bindings import ContractError, WebBindings, load_bindings
from dynamic_memory.web.demo import DemoService, demo_bindings


def test_fixtures_use_public_ids_and_source_contracts():
    bindings = demo_bindings()
    memories = bindings.list_memories("demo_caroline")
    assert len(memories) == 7
    assert any(m.event_time is None for m in memories)
    for memory in memories:
        validate_memory_id(memory)
        assert bindings.get_memory(memory.sample_id, memory.memory_id) == memory
        turn = bindings.get_turn(memory.sample_id, memory.source_refs[0])
        assert turn.text == memory.content
        assert render_visible_turn(turn).startswith(turn.text)
    assert bindings.list_memories("demo_empty") == []


@pytest.mark.parametrize("value", [None, [""], [1], ["same", "same"]])
def test_reject_invalid_sample_lists(value):
    with pytest.raises(ContractError):
        WebBindings(SimpleNamespace(list_samples=lambda: value)).list_samples()


@pytest.mark.parametrize("change", [{"sample_id": "other"}, {"schema_version": "1.2"}])
def test_reject_invalid_memory_list(change):
    record = DemoService().memories[0].model_dump()
    record.update(change)
    service = SimpleNamespace(list_memories=lambda _: [record])
    with pytest.raises((ContractError, ValidationError)):
        WebBindings(service).list_memories("demo_caroline")


def test_duplicate_ids_rejected():
    record = DemoService().memories[0]
    with pytest.raises(ContractError):
        WebBindings(SimpleNamespace(list_memories=lambda _: [record, record])).list_memories(record.sample_id)


def test_wrong_detail_id_rejected():
    service = DemoService()
    with pytest.raises(ContractError):
        WebBindings(SimpleNamespace(get_memory=lambda *_: service.memories[0])).get_memory("demo_caroline", service.memories[1].memory_id)


@pytest.mark.parametrize("field", ["sample_id", "session_id", "dia_id"])
def test_wrong_source_identity_rejected(field):
    turn = next(iter(DemoService().turns.values())).model_dump()
    turn[field] = "other"
    def get_turn(sample, ref):
        assert isinstance(ref, SourceRef)
        return turn
    with pytest.raises(ContractError):
        WebBindings(SimpleNamespace(get_turn=get_turn)).get_turn("demo_caroline", SourceRef(session_id="session_1", dia_id="D1:1"))


def test_missing_source_and_unknown_sample_are_errors():
    bindings = demo_bindings()
    with pytest.raises(KeyError):
        bindings.get_turn("demo_caroline", SourceRef(session_id="missing", dia_id="missing"))
    with pytest.raises(KeyError):
        bindings.list_memories("missing")


def test_factory_loads_typed_bindings_and_rejects_bad_config(monkeypatch):
    monkeypatch.setitem(sys.modules, "test_web_factory", SimpleNamespace(create=demo_bindings, invalid=lambda: object()))
    assert load_bindings("test_web_factory:create").synthetic
    with pytest.raises(TypeError):
        load_bindings("test_web_factory:invalid")
    with pytest.raises(ValueError):
        load_bindings("missing_separator")


def test_demo_returns_independent_objects():
    service = DemoService()
    first = service.list_memories("demo_caroline")[0]
    first.content = "changed"
    assert service.list_memories("demo_caroline")[0].content != "changed"
