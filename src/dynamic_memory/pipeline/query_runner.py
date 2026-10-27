"""A-12 synchronous three-method dispatch over explicitly injected business ports.

Only the port that loads a verified retrieval index may establish B/C disk and
index provenance. A validates public DTOs and source identities, not facts.
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from typing import Any

from pydantic import BaseModel

from dynamic_memory.contracts import (
    AnswerResult, CandidateSet, ConversationSample, ErrorInfo, MemoryRecord,
    QueryAnalysis, QueryRequest, QueryResponse, ResolutionResult, SourceRef,
    validate_resolution_against_candidates, validate_response_context,
)
from dynamic_memory.contracts.memory_id import validate_memory_id

from .query_conditions import PreparedQuery, QueryAnalyzer, QueryPreparationError, prepare_shared_query

_METHODS = frozenset({"full_text", "vector_rag", "dynamic"})
_UPSTREAM_CODES = frozenset({
    "DATA_NOT_LOADED", "DATA_LOAD_FAILED", "DATA_FORMAT_INVALID", "SAMPLE_NOT_FOUND",
    "SAMPLE_MISMATCH", "SOURCE_NOT_FOUND", "DUPLICATE_SOURCE_REF",
    "BUILD_NOT_FOUND", "ARTIFACT_INCOMPLETE", "MEMORY_BUILD_FAILED",
    "ARTIFACT_CORRUPT", "ARTIFACT_STALE", "INDEX_NOT_FOUND", "INDEX_STALE",
})
_MESSAGES = {
    "COMPONENT_UNAVAILABLE": "Required query component is unavailable.",
    "COMPONENT_FAILED": "A query component failed.",
    "SAMPLE_INVALID": "The source sample violates the public contract.",
    "CANDIDATE_INVALID": "Retrieval candidates violate the public contract.",
    "RESOLUTION_INVALID": "State resolution violates the public contract.",
    "ANSWER_INVALID": "The answer violates the public contract.",
    "RESPONSE_INVALID": "The assembled response violates the public contract.",
    **{code: "Required source, build, or index data is unavailable." for code in _UPSTREAM_CODES},
}


class _DispatchFailure(Exception):
    def __init__(self, code: str) -> None:
        self.code = code


def _reject_unknown_fields(value: object) -> None:
    """Detect model_copy extras before Pydantic serialization discards them."""
    if isinstance(value, BaseModel):
        if set(value.__dict__) != set(type(value).model_fields) or value.__pydantic_extra__:
            raise ValueError("unexpected model fields")
        for child in value.__dict__.values():
            _reject_unknown_fields(child)
    elif isinstance(value, (list, tuple, dict)):
        children = value.values() if isinstance(value, dict) else value
        for child in children:
            _reject_unknown_fields(child)


def _checked(value: object, expected_type: type[BaseModel], code: str):
    try:
        if type(value) is not expected_type:
            raise TypeError("unexpected DTO type")
        _reject_unknown_fields(value)
        document = value.model_dump(mode="python", warnings="error")
        validated = expected_type.model_validate(document, strict=True)
        return expected_type.model_validate_json(validated.model_dump_json())
    except Exception:
        raise _DispatchFailure(code) from None


def _invoke(port: Callable | None, *args):
    if not callable(port):
        raise _DispatchFailure("COMPONENT_UNAVAILABLE")
    try:
        return port(*args)
    except Exception as exc:
        code = getattr(exc, "code", None)
        if not isinstance(code, str) or code not in _UPSTREAM_CODES:
            code = "COMPONENT_FAILED"
        raise _DispatchFailure(code) from None


def _source_keys(sample: ConversationSample) -> set[tuple[str, str, str]]:
    return {
        (turn.sample_id, turn.session_id, turn.dia_id)
        for session in sample.sessions for turn in session.turns
    }


def _checked_candidates(value: object, request: QueryRequest, sample: ConversationSample) -> CandidateSet:
    candidates = _checked(value, CandidateSet, "CANDIDATE_INVALID")
    if candidates.query_id != request.query_id or candidates.sample_id != request.sample_id:
        raise _DispatchFailure("CANDIDATE_INVALID")
    sources = _source_keys(sample)
    if any((request.sample_id, ref.session_id, ref.dia_id) not in sources
           for item in candidates.items for ref in item.source_refs):
        raise _DispatchFailure("CANDIDATE_INVALID")
    return candidates


def _checked_memories(value: object, candidates: CandidateSet, sample: ConversationSample) -> list[MemoryRecord]:
    if not isinstance(value, list) or len(value) != len(candidates.items):
        raise _DispatchFailure("CANDIDATE_INVALID")
    sources = _source_keys(sample)
    checked: list[MemoryRecord] = []
    for raw, item in zip(value, candidates.items):
        record = _checked(raw, MemoryRecord, "CANDIDATE_INVALID")
        if record.sample_id != sample.sample_id or record.memory_id != item.memory_id or record.source_refs != item.source_refs:
            raise _DispatchFailure("CANDIDATE_INVALID")
        if any((sample.sample_id, ref.session_id, ref.dia_id) not in sources for ref in record.source_refs):
            raise _DispatchFailure("CANDIDATE_INVALID")
        try:
            validate_memory_id(record)
        except Exception:
            raise _DispatchFailure("CANDIDATE_INVALID") from None
        checked.append(record)
    return checked


def _checked_resolution(value: object, candidates: CandidateSet,
                        memories: list[MemoryRecord], sample: ConversationSample) -> ResolutionResult:
    resolution = _checked(value, ResolutionResult, "RESOLUTION_INVALID")
    try:
        validate_resolution_against_candidates(resolution, candidates)
        sources = _source_keys(sample)
        candidate_refs = {(ref.session_id, ref.dia_id) for record in memories for ref in record.source_refs}
        for edge in resolution.relations:
            for ref in edge.evidence_refs:
                if ((sample.sample_id, ref.session_id, ref.dia_id) not in sources or
                        (ref.session_id, ref.dia_id) not in candidate_refs):
                    raise ValueError("relation evidence is outside candidates")
    except Exception:
        raise _DispatchFailure("RESOLUTION_INVALID") from None
    return resolution


def _checked_answer(value: object) -> AnswerResult:
    answer = _checked(value, AnswerResult, "ANSWER_INVALID")
    if answer.status == "error":
        code = answer.error.code if answer.error is not None else None
        raise _DispatchFailure(code if code in _UPSTREAM_CODES else "COMPONENT_FAILED")
    if answer.error is not None:
        raise _DispatchFailure("ANSWER_INVALID")
    return answer


class QueryOrchestrator:
    """One service-owned dispatcher; no implicit provider, index, or global state.

    load_verified_index(sample) MUST reject incomplete/unverified B artifacts
    and stale C indexes. Its result is opaque to A and passed unchanged to C.
    """

    def __init__(
        self, *, analyze_query: QueryAnalyzer | None = None,
        get_sample: Callable[[str], ConversationSample] | None = None,
        load_verified_index: Callable[[ConversationSample], Any] | None = None,
        retrieve: Callable[[QueryRequest, QueryAnalysis, Any], CandidateSet] | None = None,
        load_candidates: Callable[[CandidateSet, Any], list[MemoryRecord]] | None = None,
        resolve: Callable[[QueryRequest, QueryAnalysis, list[MemoryRecord]], ResolutionResult] | None = None,
        answer_full_text: Callable[[QueryRequest, QueryAnalysis, ConversationSample], AnswerResult] | None = None,
        answer_vector_rag: Callable[[QueryRequest, QueryAnalysis, list[MemoryRecord]], AnswerResult] | None = None,
        answer_dynamic: Callable[[QueryRequest, QueryAnalysis, ResolutionResult, list[MemoryRecord]], AnswerResult] | None = None,
    ) -> None:
        self._analyze_query = analyze_query
        self._get_sample = get_sample
        self._load_verified_index = load_verified_index
        self._retrieve = retrieve
        self._load_candidates = load_candidates
        self._resolve = resolve
        self._answer_full_text = answer_full_text
        self._answer_vector_rag = answer_vector_rag
        self._answer_dynamic = answer_dynamic

    def with_source_lookup(self, get_sample: Callable[[str], ConversationSample]) -> "QueryOrchestrator":
        """Return an independent dispatcher bound to a service-owned source lookup.

        Keep the analyzer, retrieval, resolution and answer ports unchanged, but
        do not reuse a caller-supplied get_sample port from a different dataset.
        """
        if not callable(get_sample):
            raise QueryPreparationError("INVALID_SOURCE_LOOKUP")
        return QueryOrchestrator(
            analyze_query=self._analyze_query,
            get_sample=get_sample,
            load_verified_index=self._load_verified_index,
            retrieve=self._retrieve,
            load_candidates=self._load_candidates,
            resolve=self._resolve,
            answer_full_text=self._answer_full_text,
            answer_vector_rag=self._answer_vector_rag,
            answer_dynamic=self._answer_dynamic,
        )

    def _response(self, request: QueryRequest, analysis: QueryAnalysis, method: str,
                  answer: AnswerResult, sample: ConversationSample,
                  candidates: CandidateSet | None = None, memories: list[MemoryRecord] | None = None,
                  resolution: ResolutionResult | None = None) -> QueryResponse:
        if method == "full_text" and answer.used_memory_ids:
            raise _DispatchFailure("ANSWER_INVALID")
        if method == "dynamic" and resolution is not None:
            actions = {decision.memory_id: decision.action for decision in resolution.decisions}
            if any(actions.get(memory_id) == "EXCLUDE" for memory_id in answer.used_memory_ids):
                raise _DispatchFailure("ANSWER_INVALID")
            if answer.status == "answered" and not set(answer.used_memory_ids) <= set(resolution.effective_memory_ids):
                raise _DispatchFailure("ANSWER_INVALID")
            allowed_refs = {
                (ref.session_id, ref.dia_id)
                for memory in (memories or []) if actions.get(memory.memory_id) != "EXCLUDE"
                for ref in memory.source_refs
            }
            if any((ref.session_id, ref.dia_id) not in allowed_refs for ref in answer.evidence_refs):
                raise _DispatchFailure("ANSWER_INVALID")
        try:
            assembled = QueryResponse(
                query_id=request.query_id, sample_id=request.sample_id,
                question=request.question, method=method, query_analysis=analysis,
                retrieved=candidates, resolution=resolution, answer_result=answer,
            )
            _reject_unknown_fields(assembled)
            checked = QueryResponse.model_validate(assembled.model_dump(mode="python", warnings="error"), strict=True)
            turns = [turn for session in sample.sessions for turn in session.turns]
            validate_response_context(checked, memories or [], turns)
            return QueryResponse.model_validate_json(checked.model_dump_json())
        except _DispatchFailure:
            raise
        except Exception:
            raise _DispatchFailure("RESPONSE_INVALID") from None

    @staticmethod
    def _error(request: QueryRequest, analysis: QueryAnalysis, method: str, code: str) -> QueryResponse:
        return QueryResponse(
            query_id=request.query_id, sample_id=request.sample_id,
            question=request.question, method=method, query_analysis=analysis,
            retrieved=None, resolution=None,
            answer_result=AnswerResult(status="error", error=ErrorInfo(code=code, message=_MESSAGES[code])),
        )

    def prepare_query(self, request: QueryRequest) -> PreparedQuery:
        """Run the injected C analyzer once; the result can serve all methods."""
        return prepare_shared_query(request, self._analyze_query)

    def run_query(self, request: QueryRequest, method: str = "dynamic") -> QueryResponse:
        """Prepare once and dispatch one method."""
        if not isinstance(method, str) or method not in _METHODS:
            raise QueryPreparationError("INVALID_METHOD")
        return self.run_prepared(self.prepare_query(request), method)

    def run_prepared(self, prepared: PreparedQuery, method: str = "dynamic") -> QueryResponse:
        """Dispatch a previously prepared request without re-running C analysis."""
        if not isinstance(method, str) or method not in _METHODS:
            raise QueryPreparationError("INVALID_METHOD")
        if not isinstance(prepared, PreparedQuery):
            raise QueryPreparationError("INVALID_PREPARED_QUERY")
        try:
            if prepared.must_early_stop:
                return prepared.make_early_response(method)
            query = prepared.request
            analysis = prepared.analysis
            if analysis is None:
                raise ValueError("missing analysis")
        except Exception:
            raise QueryPreparationError("INVALID_PREPARED_QUERY") from None
        try:
            sample = _checked(_invoke(self._get_sample, query.sample_id), ConversationSample, "SAMPLE_INVALID")
            if sample.sample_id != query.sample_id:
                raise _DispatchFailure("SAMPLE_INVALID")
            if method == "full_text":
                answer = _checked_answer(_invoke(self._answer_full_text, deepcopy(query), deepcopy(analysis), deepcopy(sample)))
                return self._response(query, analysis, method, answer, sample)

            index = _invoke(self._load_verified_index, deepcopy(sample))
            if index is None:
                raise _DispatchFailure("COMPONENT_UNAVAILABLE")
            candidates = _checked_candidates(
                _invoke(self._retrieve, deepcopy(query), deepcopy(analysis), index), query, sample)
            memories = _checked_memories(_invoke(self._load_candidates, deepcopy(candidates), index), candidates, sample)
            if method == "vector_rag":
                answer = (_checked_answer(_invoke(self._answer_vector_rag, deepcopy(query), deepcopy(analysis), deepcopy(memories)))
                          if memories else AnswerResult(status="no_evidence", uncertainty="No retrieval candidates are available."))
                return self._response(query, analysis, method, answer, sample, candidates, memories)

            resolution = _checked_resolution(
                _invoke(self._resolve, deepcopy(query), deepcopy(analysis), deepcopy(memories)),
                candidates, memories, sample)
            answer = (_checked_answer(_invoke(self._answer_dynamic, deepcopy(query), deepcopy(analysis),
                                              deepcopy(resolution), deepcopy(memories)))
                      if memories else AnswerResult(status="no_evidence", uncertainty="No retrieval candidates are available."))
            return self._response(query, analysis, method, answer, sample, candidates, memories, resolution)
        except _DispatchFailure as exc:
            return self._error(query, analysis, method, exc.code)
