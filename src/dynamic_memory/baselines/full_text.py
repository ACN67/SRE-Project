"""Source-only Full Text QA through the shared prompt, guard, and recorder."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from pydantic import BaseModel

from dynamic_memory.contracts import (
    AnswerResult, ConversationSample, QueryAnalysis, QueryRequest, SourceRef,
    render_visible_turn,
)
from dynamic_memory.llm.mock import MockLLMAdapter
from dynamic_memory.llm.prompts import QA_PROMPT
from dynamic_memory.pipeline.runtime import LLMRuntime, PreparedCall


class FullTextError(RuntimeError):
    """Stable sanitized failure; private input and provider exceptions are omitted."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"{code}: Full Text answer failed")


@dataclass(frozen=True, slots=True)
class FullTextProvenance:
    """Caller supplied, verified snapshot and source revision; never guessed here."""

    data_fingerprint: str
    query_analysis_version: str
    git_sha: str
    run_kind: str


@dataclass(frozen=True, slots=True)
class FullTextDiagnostics:
    included_refs: tuple[tuple[str, str], ...]
    dropped_refs: tuple[tuple[str, str], ...]
    empty_refs: tuple[tuple[str, str], ...]
    truncated_full_text: bool
    input_tokens_estimated: int | None
    input_budget_verified: bool
    input_budget_limit: int


@dataclass(frozen=True, slots=True)
class _VisibleTurn:
    key: tuple[str, str]
    line: str = field(repr=False)


def _reject_extra(value: object) -> None:
    if isinstance(value, BaseModel):
        if set(value.__dict__) != set(type(value).model_fields) or value.__pydantic_extra__:
            raise ValueError("unexpected DTO fields")
        for child in value.__dict__.values():
            _reject_extra(child)
    elif isinstance(value, (tuple, list)):
        for child in value:
            _reject_extra(child)
    elif isinstance(value, dict):
        for child in value.values():
            _reject_extra(child)


def _checked(value: object, kind: type[BaseModel], code: str):
    try:
        if type(value) is not kind:
            raise TypeError("unexpected DTO type")
        _reject_extra(value)
        return kind.model_validate(value.model_dump(mode="python", warnings="error"), strict=True)
    except Exception:
        raise FullTextError(code) from None


class FullTextAnswerer:
    """Inject as QueryOrchestrator(answer_full_text=answerer).

    The same TokenEstimator installed in runtime's guard is used to select a
    suffix of complete turns. Diagnostics contain only source IDs and counts.
    ``on_diagnostic`` is invoked before the provider call, including no-evidence
    and budget-failure paths.
    """

    def __init__(self, runtime: LLMRuntime, provenance: FullTextProvenance,
                 *, on_diagnostic: Callable[[FullTextDiagnostics], None] | None = None,
                 synthetic_test_mode: bool = False) -> None:
        if not isinstance(runtime, LLMRuntime) or not isinstance(provenance, FullTextProvenance):
            raise FullTextError("DEPENDENCY_INVALID")
        if on_diagnostic is not None and not callable(on_diagnostic):
            raise FullTextError("DEPENDENCY_INVALID")
        if not isinstance(synthetic_test_mode, bool):
            raise FullTextError("DEPENDENCY_INVALID")
        self._runtime = runtime
        self._provenance = provenance
        self._on_diagnostic = on_diagnostic
        self._synthetic_test_mode = synthetic_test_mode

    def _report(self, diagnostic: FullTextDiagnostics) -> None:
        if self._on_diagnostic is not None:
            try:
                self._on_diagnostic(diagnostic)
            except Exception:
                raise FullTextError("DIAGNOSTIC_FAILED") from None

    def __call__(self, request: QueryRequest, analysis: QueryAnalysis,
                 sample: ConversationSample) -> AnswerResult:
        query = _checked(request, QueryRequest, "QUERY_INVALID")
        info = _checked(analysis, QueryAnalysis, "ANALYSIS_INVALID")
        source = _checked(sample, ConversationSample, "SAMPLE_INVALID")
        if (query.query_id != info.query_id or query.sample_id != info.sample_id or
                query.sample_id != source.sample_id):
            raise FullTextError("SOURCE_ID_MISMATCH")
        if info.time_mode == "conflict":
            raise FullTextError("TIME_CONFLICT_NOT_EARLY_STOPPED")
        if query.as_of is not None and (info.time_mode != "as_of" or info.resolved_as_of != query.as_of):
            raise FullTextError("TIME_CONDITION_MISMATCH")

        visible: list[_VisibleTurn] = []
        empty: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for session in source.sessions:
            for turn in session.turns:
                key = (session.session_id, turn.dia_id)
                if key in seen:
                    raise FullTextError("SOURCE_DUPLICATE")
                seen.add(key)
                body = render_visible_turn(turn)
                if not body.strip():
                    empty.append(key)
                    continue
                stamp = f" | recorded_at={turn.recorded_at.isoformat()}" if turn.recorded_at else ""
                # The caption remains the public [image_caption: ...] field, not speaker speech.
                visible.append(_VisibleTurn(key, f"Session={session.session_id} | dia_id={turn.dia_id} | speaker={turn.speaker}{stamp}\n{body}"))

        limit = self._runtime.settings.models.qa_max_input_tokens
        if not visible:
            self._report(FullTextDiagnostics((), (), tuple(empty), False, None, False, limit))
            return AnswerResult(status="no_evidence", uncertainty="No visible source turns in this sample.")

        conditions = f"query_intent={info.query_intent}; time_mode={info.time_mode}"
        if info.resolved_as_of is not None:
            conditions += f"; resolved_as_of={info.resolved_as_of.isoformat()}"
        question = f"{query.question}\n\nShared query conditions: {conditions}"
        estimator = self._runtime.guarded._estimator
        strict = self._runtime.guarded._strict_budget
        if estimator is None and strict:
            self._report(FullTextDiagnostics(tuple(turn.key for turn in visible), (),
                                             tuple(empty), False, None, False, limit))
            raise FullTextError("INPUT_BUDGET_UNKNOWN")

        dropped: list[_VisibleTurn] = []
        retained = visible
        prepared: PreparedCall | None = None
        estimate: int | None = None
        while retained:
            context = "\n\n".join(turn.line for turn in retained)
            try:
                prepared = self._runtime.make_request(QA_PROMPT, {"context": context, "question": question})
            except Exception:
                raise FullTextError("REQUEST_INVALID") from None
            if estimator is not None:
                try:
                    estimate = estimator(prepared.request.messages)
                except Exception:
                    raise FullTextError("INVALID_ESTIMATE") from None
                if estimate is not None and (isinstance(estimate, bool) or not isinstance(estimate, int) or estimate < 0):
                    self._report(FullTextDiagnostics(
                        tuple(turn.key for turn in retained), tuple(turn.key for turn in dropped),
                        tuple(empty), bool(dropped), None, False, limit))
                    raise FullTextError("INVALID_ESTIMATE")
            if estimate is None:
                if strict:
                    self._report(FullTextDiagnostics(
                        tuple(turn.key for turn in retained), tuple(turn.key for turn in dropped),
                        tuple(empty), bool(dropped), None, False, limit))
                    raise FullTextError("INPUT_BUDGET_UNKNOWN")
                break
            if estimate <= prepared.request.settings.max_input_tokens:
                break
            dropped.append(retained[0])
            retained = retained[1:]

        diagnostic = FullTextDiagnostics(
            tuple(turn.key for turn in retained), tuple(turn.key for turn in dropped),
            tuple(empty), bool(dropped), estimate if retained else None,
            estimate is not None and bool(retained), limit,
        )
        self._report(diagnostic)
        if not retained or prepared is None:
            raise FullTextError("INPUT_BUDGET_EXCEEDED")
        if self._runtime.backend == "mock" and (
            not self._synthetic_test_mode or isinstance(self._runtime.adapter, MockLLMAdapter)
        ):
            raise FullTextError("SYNTHETIC_ANSWER_DISALLOWED")

        try:
            identity = self._runtime.make_identity(
                prepared, sample_id=source.sample_id, scope_id=query.query_id,
                run_kind=self._provenance.run_kind, method="full_text",
                data_fingerprint=self._provenance.data_fingerprint,
                schema_id=None, schema_version=None, retriever_fingerprint=None,
                query_analysis_version=self._provenance.query_analysis_version,
                git_sha=self._provenance.git_sha,
            )
        except Exception:
            raise FullTextError("IDENTITY_INVALID") from None
        try:
            result = self._runtime.execute(prepared, identity)
            response = result.response
            if response.output_mode != "text" or response.finish_reason == "length" or not response.raw_output.strip():
                raise FullTextError("ANSWER_INVALID")
            answer = response.raw_output.strip()
        except FullTextError:
            raise
        except Exception:
            raise FullTextError("MODEL_FAILED") from None
        return AnswerResult(
            status="answered", answer=answer, used_memory_ids=[],
            context_source_refs=[SourceRef(session_id=s, dia_id=d) for s, d in diagnostic.included_refs],
            evidence_refs=[],
        )
