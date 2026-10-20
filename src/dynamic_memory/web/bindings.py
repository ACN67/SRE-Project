"""Read-only UI seam; public DTOs remain owned by contracts."""
from dataclasses import dataclass
from importlib import import_module
from typing import Protocol

from dynamic_memory.contracts.models import MemoryRecord, SourceRef, SourceTurn


class ReadOnlyService(Protocol):
    def list_samples(self) -> list[str]: ...
    def list_memories(self, sample_id: str) -> list[MemoryRecord]: ...
    def get_memory(self, sample_id: str, memory_id: str) -> MemoryRecord: ...
    def get_turn(self, sample_id: str, ref: SourceRef) -> SourceTurn: ...


class ContractError(ValueError):
    """Returned data does not belong to the requested context."""


@dataclass(frozen=True)
class WebBindings:
    service: ReadOnlyService
    label: str = "团队统一服务"
    synthetic: bool = False

    def list_samples(self) -> list[str]:
        values = self.service.list_samples()
        if not isinstance(values, list) or any(not isinstance(x, str) or not x.strip() for x in values):
            raise ContractError("Invalid sample list")
        if len(set(values)) != len(values):
            raise ContractError("Duplicate sample IDs")
        return values

    def list_memories(self, sample: str) -> list[MemoryRecord]:
        values = self.service.list_memories(sample)
        if not isinstance(values, list):
            raise ContractError("Invalid memory list")
        records = [MemoryRecord.model_validate(value) for value in values]
        if any(m.sample_id != sample for m in records):
            raise ContractError("Memory sample mismatch")
        if len({m.memory_id for m in records}) != len(records):
            raise ContractError("Duplicate memory IDs")
        return records

    def get_memory(self, sample: str, memory_id: str) -> MemoryRecord:
        record = MemoryRecord.model_validate(self.service.get_memory(sample, memory_id))
        if record.sample_id != sample or record.memory_id != memory_id:
            raise ContractError("Memory identity mismatch")
        return record

    def get_turn(self, sample: str, ref: SourceRef) -> SourceTurn:
        turn = SourceTurn.model_validate(self.service.get_turn(sample, ref))
        if (turn.sample_id, turn.session_id, turn.dia_id) != (sample, ref.session_id, ref.dia_id):
            raise ContractError("Source identity mismatch")
        return turn


def load_bindings(factory: str) -> WebBindings:
    """Import only a trusted, operator-configured factory, never user input."""
    module, separator, function = factory.partition(":")
    if not separator or not module or not function:
        raise ValueError("Expected module:function")
    result = getattr(import_module(module), function)()
    if not isinstance(result, WebBindings):
        raise TypeError("Factory must return WebBindings")
    return result
