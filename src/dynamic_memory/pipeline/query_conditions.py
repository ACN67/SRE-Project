"""A-side handoff for one C query analysis and shared early-stop responses.

The caller injects C's analyzer. This module does not infer intent or time.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from dynamic_memory.contracts.models import (
    AnswerResult, ErrorInfo, QueryAnalysis, QueryRequest, QueryResponse, TimeIssue,
)

QueryAnalyzer = Callable[[QueryRequest], QueryAnalysis]

_ERROR_MESSAGES = {
    "ANALYZER_UNAVAILABLE": "Query analyzer is unavailable.",
    "ANALYZER_FAILURE": "Query analysis failed.",
    "ANALYSIS_TYPE_INVALID": "Query analyzer returned an unexpected type.",
    "ANALYSIS_INVALID": "Query analysis violates the public contract.",
    "ANALYSIS_ID_MISMATCH": "Query analysis identity does not match the request.",
    "TIME_CONDITION_MISMATCH": "Query analysis time condition does not match the request.",
}
_METHODS = frozenset({"full_text", "vector_rag", "dynamic"})


class QueryPreparationError(ValueError):
    """Sanitized caller error, raised only when a response cannot be formed."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"{code}: query preparation failed")


@dataclass(frozen=True, slots=True)
class PreparedQuery:
    """Immutable serialized snapshot; accessors return independent DTO copies."""

    _request_json: str = field(repr=False)
    _analysis_json: str | None = field(repr=False)
    error_code: str | None = None

    @property
    def request(self) -> QueryRequest:
        return QueryRequest.model_validate_json(self._request_json)

    @property
    def analysis(self) -> QueryAnalysis | None:
        return QueryAnalysis.model_validate_json(self._analysis_json) if self._analysis_json is not None else None

    @property
    def must_early_stop(self) -> bool:
        return self.error_code is not None or self._analysis_json is not None and self.analysis.time_mode == "conflict"

    def make_early_response(self, method: str) -> QueryResponse | None:
        """Build one common failure/conflict shape, or None for normal analysis."""
        if not isinstance(method, str) or method not in _METHODS:
            raise QueryPreparationError("INVALID_METHOD")
        request = self.request
        if self.error_code is not None:
            result = AnswerResult(status="error", error=ErrorInfo(
                code=self.error_code, message=_ERROR_MESSAGES[self.error_code],
            ))
            analysis = None
        else:
            analysis = self.analysis
            if analysis is None or analysis.time_mode != "conflict":
                return None
            result = AnswerResult(
                status="time_uncertain",
                uncertainty="The explicit time and the time stated in the question conflict.",
            )
        return QueryResponse(
            query_id=request.query_id, sample_id=request.sample_id, question=request.question,
            method=method, query_analysis=analysis, retrieved=None, resolution=None,
            answer_result=result,
        )


def _failure(request_json: str, code: str) -> PreparedQuery:
    return PreparedQuery(request_json, None, code)


def _only_public_fields(value: object, expected_type: type) -> bool:
    """Reject extra fields introduced by model_copy before serialization drops them."""
    return (
        type(value) is expected_type
        and set(value.__dict__) == set(expected_type.model_fields)
        and not value.__pydantic_extra__
    )


def prepare_shared_query(request: QueryRequest, analyzer: QueryAnalyzer) -> PreparedQuery:
    """Validate the request, call C once, then freeze its validated analysis.

    Invalid caller input raises INVALID_QUERY_REQUEST. Analyzer and result
    failures instead return a PreparedQuery that builds status=error for each
    method. No exception body, question or hint is copied into diagnostics.
    """
    if not isinstance(request, QueryRequest):
        raise QueryPreparationError("INVALID_QUERY_REQUEST")
    try:
        if not _only_public_fields(request, QueryRequest):
            raise ValueError("unexpected request fields")
        checked_request = QueryRequest.model_validate(request.model_dump(mode="python", warnings=False), strict=True)
        request_json = checked_request.model_dump_json()
    except Exception:
        raise QueryPreparationError("INVALID_QUERY_REQUEST") from None

    if not callable(analyzer):
        return _failure(request_json, "ANALYZER_UNAVAILABLE")
    try:
        candidate = analyzer(QueryRequest.model_validate_json(request_json))
    except Exception:
        return _failure(request_json, "ANALYZER_FAILURE")
    if not isinstance(candidate, QueryAnalysis):
        return _failure(request_json, "ANALYSIS_TYPE_INVALID")
    try:
        if not _only_public_fields(candidate, QueryAnalysis):
            raise ValueError("unexpected analysis fields")
        if candidate.time_issue is not None and not _only_public_fields(candidate.time_issue, TimeIssue):
            raise ValueError("unexpected time issue fields")
        analysis = QueryAnalysis.model_validate(candidate.model_dump(mode="python", warnings=False), strict=True)
        analysis_json = analysis.model_dump_json()
    except Exception:
        return _failure(request_json, "ANALYSIS_INVALID")
    if analysis.query_id != checked_request.query_id or analysis.sample_id != checked_request.sample_id:
        return _failure(request_json, "ANALYSIS_ID_MISMATCH")

    issue = analysis.time_issue
    if analysis.time_mode == "conflict":
        if (checked_request.as_of is None or issue is None or
                issue.explicit_as_of != checked_request.as_of.isoformat() or
                not issue.question_time.strip() or
                analysis.resolved_as_of is not None):
            return _failure(request_json, "TIME_CONDITION_MISMATCH")
    elif issue is not None and issue.code == "QUERY_TIME_CONFLICT":
        return _failure(request_json, "TIME_CONDITION_MISMATCH")
    elif analysis.time_mode == "as_of":
        if checked_request.as_of is not None and analysis.resolved_as_of != checked_request.as_of:
            return _failure(request_json, "TIME_CONDITION_MISMATCH")
    elif analysis.time_mode == "latest_known":
        if checked_request.as_of is not None or analysis.resolved_as_of is not None:
            return _failure(request_json, "TIME_CONDITION_MISMATCH")
    elif analysis.resolved_as_of is not None:
        return _failure(request_json, "TIME_CONDITION_MISMATCH")
    elif checked_request.as_of is not None and issue is None:
        return _failure(request_json, "TIME_CONDITION_MISMATCH")

    return PreparedQuery(request_json, analysis_json)
