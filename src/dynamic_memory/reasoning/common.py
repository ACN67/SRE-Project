"""D's explicit dependencies and validation; no model or source I/O at import."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from pydantic import BaseModel

from dynamic_memory.contracts import (
    MemoryRecord, QueryAnalysis, QueryRequest, ResolutionResult,
)
from dynamic_memory.contracts.memory_id import validate_memory_id
from dynamic_memory.llm.mock import MockLLMAdapter
from dynamic_memory.pipeline.runtime import LLMRuntime


class DynamicReasoningError(ValueError):
    """Stable, sanitized error for the orchestrator's error boundary."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(f"{code}: dynamic reasoning failed")


def fingerprint(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def snapshot(value: object, schema: type[BaseModel]):
    try:
        def check(item: object) -> None:
            if isinstance(item, BaseModel):
                if (
                    set(item.__dict__) != set(type(item).model_fields)
                    or item.__pydantic_extra__
                ):
                    raise ValueError("unexpected fields")
                for child in item.__dict__.values():
                    check(child)
            elif isinstance(item, (list, tuple, dict)):
                for child in item.values() if isinstance(item, dict) else item:
                    check(child)

        if type(value) is not schema:
            raise ValueError("unexpected type")
        check(value)
        return schema.model_validate(value.model_dump(mode="python"), strict=True)
    except Exception:
        raise DynamicReasoningError("INPUT_INVALID") from None


def checked_inputs(
    request: object, analysis: object, candidates: object
) -> tuple[QueryRequest, QueryAnalysis, list[MemoryRecord]]:
    request = snapshot(request, QueryRequest)
    analysis = snapshot(analysis, QueryAnalysis)
    if (
        request.query_id != analysis.query_id
        or request.sample_id != analysis.sample_id
    ):
        raise DynamicReasoningError("QUERY_ANALYSIS_MISMATCH")
    if (
        (analysis.time_mode != "as_of" and analysis.resolved_as_of is not None)
        or (
            request.as_of is not None
            and analysis.time_mode not in {"as_of", "conflict", "unknown"}
        )
        or (
            request.as_of is not None
            and analysis.time_mode == "as_of"
            and request.as_of != analysis.resolved_as_of
        )
    ):
        raise DynamicReasoningError("QUERY_ANALYSIS_MISMATCH")
    if type(candidates) is not list:
        raise DynamicReasoningError("CANDIDATES_INVALID")
    memories = [snapshot(memory, MemoryRecord) for memory in candidates]
    if (
        len({memory.memory_id for memory in memories}) != len(memories)
        or any(memory.sample_id != request.sample_id for memory in memories)
    ):
        raise DynamicReasoningError("CANDIDATES_INVALID")
    try:
        for memory in memories:
            validate_memory_id(memory)
    except ValueError:
        raise DynamicReasoningError("CANDIDATES_INVALID") from None
    return request, analysis, memories


def validate_resolution(
    result: object, request: QueryRequest, memories: list[MemoryRecord]
) -> ResolutionResult:
    result = snapshot(result, ResolutionResult)
    by_id = {memory.memory_id: memory for memory in memories}
    actions = {decision.memory_id: decision.action for decision in result.decisions}
    try:
        if result.query_id != request.query_id or set(actions) != set(by_id):
            raise ValueError("coverage mismatch")
        if any(not decision.reason.strip() for decision in result.decisions):
            raise ValueError("empty reason")
        unresolved = set(result.unresolved_conflict_ids)
        if any(actions.get(memory_id) != "UNRESOLVED" for memory_id in unresolved):
            raise ValueError("conflict IDs must be unresolved")
        conflict_ids: set[str] = set()
        for edge in result.relations:
            left = by_id[edge.from_memory_id]
            right = by_id[edge.to_memory_id]
            if not edge.reason.strip() or not edge.scope_attribute.strip():
                raise ValueError("empty relation explanation")
            refs = {(ref.session_id, ref.dia_id) for ref in edge.evidence_refs}
            left_refs = {(ref.session_id, ref.dia_id) for ref in left.source_refs}
            right_refs = {(ref.session_id, ref.dia_id) for ref in right.source_refs}
            if (
                len(refs) != len(edge.evidence_refs)
                or not refs <= left_refs | right_refs
                or not refs & left_refs
                or not refs & right_refs
            ):
                raise ValueError("relation requires sources from both endpoints")
            if (
                edge.relation in {"UPDATE", "SUPERSEDE"}
                and left.event_time
                and right.event_time
                and left.assertion_type == right.assertion_type == "fact"
                and left.event_time.start
                and right.event_time.end_exclusive
            ):
                try:
                    reversed_time = (
                        left.event_time.start >= right.event_time.end_exclusive
                    )
                except TypeError:
                    reversed_time = False
                if reversed_time:
                    raise ValueError("relation reverses evidenced event chronology")
            if edge.relation == "CONFLICT":
                pair = {left.memory_id, right.memory_id}
                conflict_ids |= pair
                if pair & unresolved and not pair <= unresolved:
                    raise ValueError(
                        "a relevant conflict must retain both unresolved endpoints"
                    )
        if not unresolved <= conflict_ids:
            raise ValueError("conflict IDs require a conflict edge")
        if (
            any(decision.action == "UNRESOLVED" for decision in result.decisions)
            and not (result.uncertainty or "").strip()
        ):
            raise ValueError("unresolved decisions require uncertainty")
    except (KeyError, ValueError):
        raise DynamicReasoningError("RESOLUTION_INVALID") from None
    return result


@dataclass(frozen=True)
class DynamicProvenance:
    data_fingerprint: str
    query_analysis_version: str
    git_sha: str
    run_kind: str
    retriever_fingerprint: str


@dataclass(frozen=True)
class DynamicDiagnostic:
    phase: str
    query_id: str
    sample_id: str
    query_fingerprint: str
    candidate_ids: tuple[str, ...]
    included_memory_ids: tuple[str, ...]
    excluded_memory_ids: tuple[str, ...]
    status: str
    cache_hit: bool | None
    call_summary: object | None


class DynamicComponent:
    def __init__(
        self,
        runtime: LLMRuntime,
        provenance: DynamicProvenance,
        *,
        synthetic_test_mode: bool = False,
        on_diagnostic=None,
    ):
        if (
            not isinstance(runtime, LLMRuntime)
            or type(provenance) is not DynamicProvenance
            or type(synthetic_test_mode) is not bool
            or (on_diagnostic is not None and not callable(on_diagnostic))
        ):
            raise DynamicReasoningError("DEPENDENCY_INVALID")
        self.runtime = runtime
        self.provenance = provenance
        self.synthetic = synthetic_test_mode
        self.on_diagnostic = on_diagnostic

    def execute(self, template, variables, request, validator, schema_id):
        if self.runtime.backend == "mock" and (
            not self.synthetic or isinstance(self.runtime.adapter, MockLLMAdapter)
        ):
            raise DynamicReasoningError("SYNTHETIC_OUTPUT_DISALLOWED")
        provenance = self.provenance
        try:
            prepared = self.runtime.make_request(template, variables)
            identity = self.runtime.make_identity(
                prepared,
                sample_id=request.sample_id,
                scope_id=request.query_id,
                run_kind=provenance.run_kind,
                method="dynamic" if prepared.request.task == "qa" else None,
                data_fingerprint=provenance.data_fingerprint,
                schema_id=schema_id,
                schema_version="2.1",
                retriever_fingerprint=provenance.retriever_fingerprint,
                query_analysis_version=provenance.query_analysis_version,
                git_sha=provenance.git_sha,
            )
            result = self.runtime.execute(prepared, identity, validator=validator)
            return validator(prepared.request, result.response), result
        except DynamicReasoningError:
            raise
        except Exception as exc:
            code = getattr(exc, "code", None)
            if code not in {
                "INPUT_BUDGET_UNKNOWN", "INPUT_BUDGET_EXCEEDED",
                "OUTPUT_BUDGET_EXCEEDED", "INVALID_ESTIMATE",
                "RESPONSE_INCOMPLETE", "TIMEOUT",
            }:
                code = "MODEL_OUTPUT_INVALID"
            raise DynamicReasoningError(code) from None

    def report(self, phase, request, analysis, candidates, included, status, call=None):
        if self.on_diagnostic is None:
            return
        included_ids = tuple(memory.memory_id for memory in included)
        diagnostic = DynamicDiagnostic(
            phase,
            request.query_id,
            request.sample_id,
            fingerprint({
                "request": request.model_dump(mode="json"),
                "analysis": analysis.model_dump(mode="json"),
                "candidates": [
                    memory.model_dump(mode="json") for memory in candidates
                ],
            }),
            tuple(memory.memory_id for memory in candidates),
            included_ids,
            tuple(
                memory.memory_id for memory in candidates
                if memory.memory_id not in included_ids
            ),
            status,
            call.cache_status == "hit" if call is not None else None,
            getattr(call, "summary", None),
        )
        try:
            self.on_diagnostic(diagnostic)
        except Exception:
            raise DynamicReasoningError("DIAGNOSTIC_FAILED") from None
