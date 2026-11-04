"""D's explicit dependencies and validation; no model or source I/O at import."""

from dataclasses import dataclass
import hashlib
import json

from pydantic import BaseModel

from dynamic_memory.contracts import MemoryRecord, QueryAnalysis, QueryRequest, ResolutionResult
from dynamic_memory.contracts.memory_id import validate_memory_id
from dynamic_memory.llm.mock import MockLLMAdapter
from dynamic_memory.pipeline.runtime import LLMRuntime


class DynamicReasoningError(ValueError):
    """Stable, sanitized error, suitable for the orchestrator's error boundary."""

    def __init__(self, code):
        self.code = code
        super().__init__(f"{code}: dynamic reasoning failed")


def fingerprint(value):
    return "sha256:" + hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def snapshot(value, schema):
    try:
        def check(item):
            if isinstance(item, BaseModel):
                if set(item.__dict__) != set(type(item).model_fields) or item.__pydantic_extra__:
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


def checked_inputs(request, analysis, candidates):
    request, analysis = snapshot(request, QueryRequest), snapshot(analysis, QueryAnalysis)
    if (request.query_id != analysis.query_id or request.sample_id != analysis.sample_id):
        raise DynamicReasoningError("QUERY_ANALYSIS_MISMATCH")
    if ((analysis.time_mode != "as_of" and analysis.resolved_as_of is not None) or
        (request.as_of is not None and analysis.time_mode not in {"as_of", "conflict", "unknown"}) or
        (request.as_of is not None and analysis.time_mode == "as_of" and request.as_of != analysis.resolved_as_of)):
        raise DynamicReasoningError("QUERY_ANALYSIS_MISMATCH")
    if type(candidates) is not list:
        raise DynamicReasoningError("CANDIDATES_INVALID")
    memories = [snapshot(m, MemoryRecord) for m in candidates]
    if (len({m.memory_id for m in memories}) != len(memories) or
        any(m.sample_id != request.sample_id for m in memories)):
        raise DynamicReasoningError("CANDIDATES_INVALID")
    try:
        for memory in memories:
            validate_memory_id(memory)
    except ValueError:
        raise DynamicReasoningError("CANDIDATES_INVALID") from None
    return request, analysis, memories


def validate_resolution(result, request, memories):
    result = snapshot(result, ResolutionResult)
    by_id = {m.memory_id: m for m in memories}
    actions = {d.memory_id: d.action for d in result.decisions}
    try:
        if result.query_id != request.query_id or set(actions) != set(by_id):
            raise ValueError("coverage mismatch")
        if any(not d.reason.strip() for d in result.decisions):
            raise ValueError("empty reason")
        unresolved = set(result.unresolved_conflict_ids)
        if any(actions.get(mid) != "UNRESOLVED" for mid in unresolved):
            raise ValueError("conflict IDs must be unresolved")
        conflict_ids = set()
        for edge in result.relations:
            left, right = by_id[edge.from_memory_id], by_id[edge.to_memory_id]
            if not edge.reason.strip() or not edge.scope_attribute.strip():
                raise ValueError("empty relation explanation")
            refs = {(r.session_id, r.dia_id) for r in edge.evidence_refs}
            lrefs = {(r.session_id, r.dia_id) for r in left.source_refs}
            rrefs = {(r.session_id, r.dia_id) for r in right.source_refs}
            if (len(refs) != len(edge.evidence_refs) or not refs <= lrefs | rrefs or
                not refs & lrefs or not refs & rrefs):
                raise ValueError("relation requires sources from both endpoints")
            if (edge.relation in {"UPDATE", "SUPERSEDE"} and left.event_time and right.event_time
                    and left.assertion_type == right.assertion_type == "fact"):
                # Only reject provably reversed, comparable event intervals.
                # Overlap or absent dates cannot establish chronology.
                if left.event_time.start and right.event_time.end_exclusive:
                    try:
                        reversed_time = left.event_time.start >= right.event_time.end_exclusive
                    except TypeError:
                        reversed_time = False
                    if reversed_time:
                        raise ValueError("relation reverses evidenced event chronology")
            if edge.relation == "CONFLICT":
                pair = {left.memory_id, right.memory_id}
                conflict_ids |= pair
                if pair & unresolved and not pair <= unresolved:
                    raise ValueError("a relevant conflict must retain both unresolved endpoints")
        if not unresolved <= conflict_ids:
            raise ValueError("conflict IDs require a conflict edge")
        if any(d.action == "UNRESOLVED" for d in result.decisions) and not (result.uncertainty or "").strip():
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
    def __init__(self, runtime: LLMRuntime, provenance: DynamicProvenance, *,
                 synthetic_test_mode=False, on_diagnostic=None):
        if (not isinstance(runtime, LLMRuntime) or type(provenance) is not DynamicProvenance or
            type(synthetic_test_mode) is not bool or
            (on_diagnostic is not None and not callable(on_diagnostic))):
            raise DynamicReasoningError("DEPENDENCY_INVALID")
        self.runtime, self.provenance = runtime, provenance
        self.synthetic, self.on_diagnostic = synthetic_test_mode, on_diagnostic

    def execute(self, template, variables, request, validator, schema_id):
        if self.runtime.backend == "mock" and (
            not self.synthetic or isinstance(self.runtime.adapter, MockLLMAdapter)
        ):
            raise DynamicReasoningError("SYNTHETIC_OUTPUT_DISALLOWED")
        p = self.provenance
        try:
            prepared = self.runtime.make_request(template, variables)
            identity = self.runtime.make_identity(
                prepared, sample_id=request.sample_id, scope_id=request.query_id,
                run_kind=p.run_kind, method="dynamic" if prepared.request.task == "qa" else None,
                data_fingerprint=p.data_fingerprint,
                schema_id=schema_id, schema_version="2.1", retriever_fingerprint=p.retriever_fingerprint,
                query_analysis_version=p.query_analysis_version, git_sha=p.git_sha)
            result = self.runtime.execute(prepared, identity, validator=validator)
            return validator(prepared.request, result.response), result
        except DynamicReasoningError:
            raise
        except Exception as exc:
            code = getattr(exc, "code", None)
            if code not in {"INPUT_BUDGET_UNKNOWN", "INPUT_BUDGET_EXCEEDED", "OUTPUT_BUDGET_EXCEEDED",
                            "INVALID_ESTIMATE", "RESPONSE_INCOMPLETE", "TIMEOUT"}:
                code = "MODEL_OUTPUT_INVALID"
            raise DynamicReasoningError(code) from None

    def report(self, phase, request, analysis, candidates, included, status, call=None):
        if self.on_diagnostic is None:
            return
        included_ids = tuple(m.memory_id for m in included)
        diagnostic = DynamicDiagnostic(
            phase, request.query_id, request.sample_id,
            fingerprint({"request": request.model_dump(mode="json"),
                         "analysis": analysis.model_dump(mode="json"),
                         "candidates": [m.model_dump(mode="json") for m in candidates]}),
            tuple(m.memory_id for m in candidates), included_ids,
            tuple(m.memory_id for m in candidates if m.memory_id not in included_ids),
            status, call.cache_status == "hit" if call is not None else None,
            getattr(call, "summary", None))
        try:
            self.on_diagnostic(diagnostic)
        except Exception:
            raise DynamicReasoningError("DIAGNOSTIC_FAILED") from None
