"""Read-only UI seam; public DTOs remain owned by contracts."""
from dataclasses import dataclass
from importlib import import_module
from typing import Protocol

from dynamic_memory.contracts.models import MemoryRecord, SourceRef, SourceTurn, QueryRequest, QueryResponse
from dynamic_memory.contracts.validation import validate_response_context


class ReadOnlyService(Protocol):
    def list_samples(self) -> list[str]: ...
    def list_memories(self, sample_id: str) -> list[MemoryRecord]: ...
    def get_memory(self, sample_id: str, memory_id: str) -> MemoryRecord: ...
    def get_turn(self, sample_id: str, ref: SourceRef) -> SourceTurn: ...


class ContractError(ValueError):
    """Returned data does not belong to the requested context."""


class InterfaceUnavailable(RuntimeError):
    code = "COMPONENT_UNAVAILABLE"


@dataclass(frozen=True)
class WebBindings:
    service: ReadOnlyService
    label: str = "团队统一服务"
    synthetic: bool = False
    dynamic_available: bool = True

    def supports(self, method: str) -> bool:
        return callable(getattr(self.service, method, None))

    def get_query_state(self, sample: str) -> str | None:
        if not self.supports("get_query_state"):
            return None
        value = self.service.get_query_state(sample)
        if type(value) is not str or not value or len(value) > 512:
            raise ContractError("Invalid query state")
        return value

    def get_build_status(self, sample: str):
        # Use A's sole public DTO after merge; never maintain a local substitute.
        if not self.supports("get_build_status"):
            raise InterfaceUnavailable()
        try:
            from dynamic_memory.contracts.service_status import BuildStatus
        except ModuleNotFoundError:
            raise InterfaceUnavailable() from None
        value = self.service.get_build_status(sample)
        if not isinstance(value, BuildStatus):
            raise ContractError("Expected public BuildStatus")
        return BuildStatus.model_validate(value.model_dump(mode="python"), strict=True)

    def run_query(self, request: QueryRequest, method: str = "dynamic") -> QueryResponse:
        if method not in {"full_text", "vector_rag", "dynamic"}:
            raise ContractError("Unknown query method")
        if not self.supports("run_query"):
            raise InterfaceUnavailable()
        request = QueryRequest.model_validate(request.model_dump(mode="python"))
        expected = (request.query_id, request.sample_id, request.question, method)
        raw = self.service.run_query(request, method=method)
        response = QueryResponse.model_validate(raw.model_dump(mode="python") if isinstance(raw, QueryResponse) else raw)
        if (response.query_id, response.sample_id, response.question, response.method) != expected:
            raise ContractError("Query response identity mismatch")
        # Read only referenced records through the same service. A also validates
        # provenance; these checks protect the UI from mismatched integrations.
        memories = [self.get_memory(request.sample_id, item.memory_id)
                    for item in response.retrieved.items] if response.retrieved is not None else []
        turns = [self.get_turn(request.sample_id, ref) for ref in response.answer_result.context_source_refs]
        validate_response_context(response, memories, turns)
        return response

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
