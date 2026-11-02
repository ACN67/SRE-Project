"""Public schema v2.1. Validation requiring a source index belongs in validation.py."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "2.1"


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VersionedModel(ContractModel):
    schema_version: Literal["2.1"] = SCHEMA_VERSION


class SourceRef(ContractModel):
    session_id: str = Field(min_length=1)
    dia_id: str = Field(min_length=1)


class SourceTurn(ContractModel):
    sample_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    dia_id: str = Field(min_length=1)
    speaker: str = Field(min_length=1)
    text: str
    recorded_at: datetime | None = None
    image_caption: str | None = None
    caption_origin: str | None = None

    @model_validator(mode="after")
    def caption_source(self):
        if bool(self.image_caption) != bool(self.caption_origin):
            raise ValueError("image_caption and caption_origin must occur together")
        return self


def render_visible_turn(turn: SourceTurn) -> str:
    return turn.text + (f" [image_caption: {turn.image_caption}]" if turn.image_caption else "")


class TimeSpan(ContractModel):
    raw: str | None = None
    start: datetime | None = None
    end_exclusive: datetime | None = None
    precision: Literal["instant", "day", "month", "year", "unknown"] = "unknown"
    resolution: Literal["explicit", "relative", "unknown"] = "unknown"

    @model_validator(mode="after")
    def ordered(self):
        if (self.start is None) != (self.end_exclusive is None):
            raise ValueError("TimeSpan boundaries must both be set or both null")
        if self.start is not None:
            try:
                valid = self.start < self.end_exclusive
            except TypeError as exc:
                raise ValueError("TimeSpan boundaries have incompatible time zones") from exc
            if not valid:
                raise ValueError("TimeSpan requires start < end_exclusive")
        return self


class MemoryRecord(VersionedModel):
    memory_id: str = Field(pattern=r"^mem_[0-9a-f]{20}$")
    sample_id: str = Field(min_length=1)
    content: str = Field(min_length=1)
    entity: str | None = None
    attribute: str | None = None
    value: str | None = None
    fact_kind: Literal["event", "state", "unknown"]
    assertion_type: Literal["fact", "plan", "hypothetical", "uncertain", "unknown"]
    polarity: Literal["positive", "negative", "unknown"]
    recorded_at: datetime | None = None
    event_time: TimeSpan | None = None
    source_refs: list[SourceRef] = Field(min_length=1)
    extractor_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def one_session(self):
        if len({r.session_id for r in self.source_refs}) != 1:
            raise ValueError("MemoryRecord source_refs must belong to one session")
        if len({(r.session_id, r.dia_id) for r in self.source_refs}) != len(self.source_refs):
            raise ValueError("MemoryRecord source_refs contain duplicates")
        return self


QueryIntent = Literal["current_state", "historical_state", "state_history", "event_fact", "general_fact", "unknown"]
TimeMode = Literal["latest_known", "as_of", "unknown", "conflict"]


class QueryRequest(VersionedModel):
    query_id: str = Field(min_length=1)
    sample_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    as_of: date | None = None

    @field_validator("question")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("question must contain non-whitespace text")
        return value


class TimeIssue(ContractModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    explicit_as_of: str | None = None
    question_time: str | None = None


class QueryAnalysis(VersionedModel):
    query_id: str = Field(min_length=1)
    sample_id: str = Field(min_length=1)
    query_intent: QueryIntent
    entity_hints: list[str] = Field(default_factory=list)
    attribute_hints: list[str] = Field(default_factory=list)
    time_mode: TimeMode
    resolved_as_of: date | None = None
    time_issue: TimeIssue | None = None

    @model_validator(mode="after")
    def time_consistency(self):
        if self.time_mode == "as_of" and self.resolved_as_of is None:
            raise ValueError("as_of time_mode requires resolved_as_of")
        if self.time_mode == "conflict" and (self.time_issue is None or self.time_issue.code != "QUERY_TIME_CONFLICT" or not self.time_issue.explicit_as_of or not self.time_issue.question_time):
            raise ValueError("conflict requires QUERY_TIME_CONFLICT and both source times")
        return self


class CandidateItem(ContractModel):
    memory_id: str = Field(min_length=1)
    rank: int = Field(ge=1)
    score: float
    source_refs: list[SourceRef] = Field(min_length=1)
    origin: Literal["semantic", "history_expansion"]


class CandidateSet(VersionedModel):
    query_id: str = Field(min_length=1)
    sample_id: str = Field(min_length=1)
    base_top_k: int = Field(ge=1)
    max_candidates: int = Field(ge=1)
    retriever_fingerprint: str = Field(min_length=1)
    items: list[CandidateItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def budget_and_order(self):
        if self.base_top_k > self.max_candidates:
            raise ValueError("base_top_k exceeds max_candidates")
        if len(self.items) > self.max_candidates:
            raise ValueError("CandidateSet exceeds max_candidates")
        if [item.rank for item in self.items] != list(range(1, len(self.items) + 1)):
            raise ValueError("CandidateSet rank must be consecutive from 1 and ordered")
        if len({item.memory_id for item in self.items}) != len(self.items):
            raise ValueError("CandidateSet has duplicate memory IDs")
        return self


class RelationEdge(ContractModel):
    from_memory_id: str = Field(min_length=1)
    to_memory_id: str = Field(min_length=1)
    relation: Literal["UPDATE", "SUPERSEDE", "CONFLICT"]
    scope_attribute: str = Field(min_length=1)
    evidence_refs: list[SourceRef] = Field(default_factory=list)
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def endpoints(self):
        if self.from_memory_id == self.to_memory_id:
            raise ValueError("RelationEdge cannot be a self-loop")
        if self.relation == "CONFLICT" and self.from_memory_id > self.to_memory_id:
            raise ValueError("CONFLICT endpoints must be lexically ordered")
        return self


class MemoryDecision(ContractModel):
    memory_id: str = Field(min_length=1)
    action: Literal["RETAIN", "EXCLUDE", "UNRESOLVED"]
    reason: str = Field(min_length=1)


class ResolutionResult(VersionedModel):
    query_id: str = Field(min_length=1)
    relations: list[RelationEdge] = Field(default_factory=list)
    decisions: list[MemoryDecision] = Field(default_factory=list)
    effective_memory_ids: list[str] = Field(default_factory=list)
    unresolved_conflict_ids: list[str] = Field(default_factory=list)
    uncertainty: str | None = None

    @model_validator(mode="after")
    def internal_sets(self):
        ids = [d.memory_id for d in self.decisions]
        if len(ids) != len(set(ids)):
            raise ValueError("ResolutionResult has duplicate decisions")
        retained = {d.memory_id for d in self.decisions if d.action == "RETAIN"}
        if len(self.effective_memory_ids) != len(set(self.effective_memory_ids)) or set(self.effective_memory_ids) != retained:
            raise ValueError("effective_memory_ids must equal RETAIN IDs")
        if len(self.unresolved_conflict_ids) != len(set(self.unresolved_conflict_ids)):
            raise ValueError("unresolved_conflict_ids contain duplicates")
        edges = [(e.from_memory_id, e.to_memory_id, e.relation, e.scope_attribute) for e in self.relations]
        if len(edges) != len(set(edges)):
            raise ValueError("duplicate RelationEdge")
        return self


class ErrorInfo(ContractModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)


class AnswerResult(VersionedModel):
    status: Literal["answered", "no_evidence", "time_uncertain", "unresolved_conflict", "error"]
    answer: str | None = None
    used_memory_ids: list[str] = Field(default_factory=list)
    context_source_refs: list[SourceRef] = Field(default_factory=list)
    evidence_refs: list[SourceRef] = Field(default_factory=list)
    uncertainty: str | None = None
    error: ErrorInfo | None = None

    @model_validator(mode="after")
    def status_and_sources(self):
        if self.status == "answered" and (not self.answer or not self.answer.strip() or self.error is not None):
            raise ValueError("answered requires nonempty answer and no error")
        if self.status == "error" and (self.error is None or self.answer is not None):
            raise ValueError("error requires error info and null answer")
        if self.status in {"no_evidence", "time_uncertain", "unresolved_conflict"} and not self.uncertainty:
            raise ValueError("uncertain status requires uncertainty reason")
        if not {(r.session_id, r.dia_id) for r in self.evidence_refs} <= {(r.session_id, r.dia_id) for r in self.context_source_refs}:
            raise ValueError("evidence_refs must be included in context_source_refs")
        if len(self.used_memory_ids) != len(set(self.used_memory_ids)):
            raise ValueError("used_memory_ids contain duplicates")
        return self


class QueryResponse(VersionedModel):
    query_id: str = Field(min_length=1)
    sample_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    method: Literal["full_text", "vector_rag", "dynamic", "pure_vector_topk"]
    query_analysis: QueryAnalysis | None
    retrieved: CandidateSet | None
    resolution: ResolutionResult | None
    answer_result: AnswerResult

    @model_validator(mode="after")
    def method_shape(self):
        if self.query_analysis and (self.query_analysis.query_id != self.query_id or self.query_analysis.sample_id != self.sample_id):
            raise ValueError("QueryAnalysis query/sample mismatch")
        if self.retrieved and (self.retrieved.query_id != self.query_id or self.retrieved.sample_id != self.sample_id):
            raise ValueError("CandidateSet query/sample mismatch")
        if self.resolution and self.resolution.query_id != self.query_id:
            raise ValueError("ResolutionResult query mismatch")
        if self.method == "full_text":
            if self.retrieved is not None or self.resolution is not None or self.answer_result.used_memory_ids:
                raise ValueError("full_text cannot include retrieved, resolution or used_memory_ids")
        elif self.method in {"vector_rag", "pure_vector_topk"}:
            if self.resolution is not None:
                raise ValueError("vector_rag cannot include resolution")
            self._require_retrieval()
            if self.method == "pure_vector_topk" and self.answer_result.status == "answered" and not self.retrieved.items:
                raise ValueError("pure_vector_topk answered response requires candidates")
        else:
            self._require_retrieval()
            if self.resolution is None and not self._early_stop():
                raise ValueError("dynamic requires resolution on normal path")
        return self

    def _early_stop(self):
        return self.answer_result.status == "error" or (self.answer_result.status == "time_uncertain" and self.query_analysis is not None and self.query_analysis.time_mode == "conflict")

    def _require_retrieval(self):
        if self.retrieved is None and not self._early_stop():
            raise ValueError("retrieved is required on normal path")
