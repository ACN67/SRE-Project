"""Vector RAG answer port through A's shared runtime; no D state resolution."""

from dataclasses import dataclass
import json
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict

from dynamic_memory.contracts.models import AnswerResult, MemoryRecord, QueryAnalysis, QueryRequest, SourceRef
from dynamic_memory.llm.mock import MockLLMAdapter
from dynamic_memory.llm.prompts import QA_PROMPT
from dynamic_memory.pipeline.runtime import LLMRuntime
from dynamic_memory.qa.benchmark_protocol import build_hybrid_context, context_refs
from dynamic_memory.data.repository import SourceRepository
from .index import RetrievalIndex, RetrievalIndexError, fingerprint
from .vector_retrieval import _snapshot


class VectorAnswerError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(f"{code}: vector answer failed")


@dataclass(frozen=True)
class VectorAnswerProvenance:
    data_fingerprint: str
    query_analysis_version: str
    git_sha: str
    run_kind: str


@dataclass(frozen=True)
class VectorAnswerDiagnostics:
    query_id: str
    sample_id: str
    query_fingerprint: str
    retriever_fingerprint: str
    included_memory_ids: tuple[str, ...]
    dropped_memory_ids: tuple[str, ...]
    input_tokens_estimated: int | None
    input_budget_verified: bool
    input_budget_limit: int


class _AnswerDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    status: Literal["answered", "no_evidence", "time_uncertain", "unresolved_conflict"]
    answer: str | None
    evidence_refs: list[SourceRef]
    uncertainty: str | None


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _decode(response, memories):
    try:
        if response.output_mode != "text" or response.finish_reason not in (None, "stop"):
            raise ValueError("incomplete response")
        raw = json.loads(response.raw_output, object_pairs_hook=_unique)
        draft = _AnswerDraft.model_validate(raw)
        refs = {(r.session_id, r.dia_id): r for m in memories for r in m.source_refs}
        evidence = [(r.session_id, r.dia_id) for r in draft.evidence_refs]
        if len(evidence) != len(set(evidence)) or not set(evidence) <= refs.keys():
            raise ValueError("unsupported source")
        if draft.status == "answered":
            if not draft.answer or not draft.answer.strip() or not evidence:
                raise ValueError("answer needs evidence")
        elif draft.answer is not None or not draft.uncertainty or not draft.uncertainty.strip():
            raise ValueError("uncertain answer must state reason")
        return AnswerResult(status=draft.status, answer=draft.answer,
                            used_memory_ids=[m.memory_id for m in memories],
                            context_source_refs=list(refs.values()), evidence_refs=draft.evidence_refs,
                            uncertainty=draft.uncertainty)
    except Exception:
        raise VectorAnswerError("ANSWER_INVALID") from None


def _decode_plain(response, memories, context) -> AnswerResult:
    try:
        if response.output_mode != "text" or response.finish_reason not in (None, "stop"):
            raise ValueError("incomplete response")
        answer = response.raw_output.strip()
        if not answer:
            raise ValueError("empty answer")
        refs = context_refs(context)
        return AnswerResult(
            status="answered",
            answer=answer,
            used_memory_ids=[memory.memory_id for memory in memories],
            context_source_refs=list(refs),
            evidence_refs=list(refs),
        )
    except Exception:
        raise VectorAnswerError("ANSWER_INVALID") from None


class VectorAnswerer:
    """Bind verified index, runtime and run identity; inject the callable into A.

    Candidates must be the ordered result of C.load_candidates. The index checks
    their content/provenance, but cannot prove which query selected them.
    """

    def __init__(
        self,
        runtime: LLMRuntime,
        provenance: VectorAnswerProvenance,
        index: RetrievalIndex,
        *,
        on_diagnostic=None,
        synthetic_test_mode=False,
        source_repository: SourceRepository | None = None,
        source_metadata_lookup: Callable | None = None,
        question_transform: Callable[[QueryRequest], str] | None = None,
        prompt_template=QA_PROMPT,
        plain_text: bool = False,
        max_context_memories: int = 12,
    ):
        if (not isinstance(runtime, LLMRuntime) or type(provenance) is not VectorAnswerProvenance or
                type(index) is not RetrievalIndex or type(synthetic_test_mode) is not bool or
                (on_diagnostic is not None and not callable(on_diagnostic))):
            raise VectorAnswerError("DEPENDENCY_INVALID")
        if (source_repository is not None and type(source_repository) is not SourceRepository):
            raise VectorAnswerError("DEPENDENCY_INVALID")
        if source_metadata_lookup is not None and not callable(source_metadata_lookup):
            raise VectorAnswerError("DEPENDENCY_INVALID")
        if question_transform is not None and not callable(question_transform):
            raise VectorAnswerError("DEPENDENCY_INVALID")
        if type(plain_text) is not bool or type(max_context_memories) is not int or max_context_memories < 1:
            raise VectorAnswerError("DEPENDENCY_INVALID")
        index.__post_init__()
        self._runtime, self._provenance, self._index = runtime, provenance, index
        self._on_diagnostic, self._synthetic = on_diagnostic, synthetic_test_mode
        self._source_repository = source_repository
        self._source_metadata_lookup = source_metadata_lookup
        self._question_transform = question_transform
        self._prompt_template = prompt_template
        self._plain_text = plain_text
        self._max_context_memories = max_context_memories

    def _report(self, request, analysis, included, dropped, estimate):
        # Use this call's validated snapshots, never mutable instance-level query state.
        index_fp = self._index.retriever_fingerprint
        diagnostic = VectorAnswerDiagnostics(
            query_id=request.query_id, sample_id=request.sample_id,
            query_fingerprint=fingerprint({"request": request.model_dump(mode="json"),
                                           "analysis": analysis.model_dump(mode="json"), "index": index_fp}),
            retriever_fingerprint=index_fp,
            included_memory_ids=tuple(m.memory_id for m in included),
            dropped_memory_ids=tuple(m.memory_id for m in dropped), input_tokens_estimated=estimate,
            input_budget_verified=estimate is not None and bool(included),
            input_budget_limit=self._runtime.settings.models.qa_max_input_tokens)
        if self._on_diagnostic:
            try:
                self._on_diagnostic(diagnostic)
            except Exception:
                raise VectorAnswerError("DIAGNOSTIC_FAILED") from None

    def __call__(self, request, analysis, candidates):
        return self.answer_vector_rag(request, analysis, candidates)

    def answer_vector_rag(self, request: QueryRequest, analysis: QueryAnalysis,
                          candidates: list[MemoryRecord]) -> AnswerResult:
        try:
            request = _snapshot(request, QueryRequest, "QUERY_INVALID")
            analysis = _snapshot(analysis, QueryAnalysis, "ANALYSIS_INVALID")
            if type(candidates) is not list:
                raise VectorAnswerError("CANDIDATES_INVALID")
            memories = [_snapshot(m, MemoryRecord, "CANDIDATES_INVALID") for m in candidates]
        except RetrievalIndexError as error:
            raise VectorAnswerError(error.code) from None
        if request.sample_id != analysis.sample_id or request.sample_id != self._index.sample_id:
            raise VectorAnswerError("SAMPLE_MISMATCH")
        if request.query_id != analysis.query_id:
            raise VectorAnswerError("QUERY_ANALYSIS_MISMATCH")
        if analysis.time_mode == "conflict":
            self._report(request, analysis, [], [], None)
            return AnswerResult(status="time_uncertain", uncertainty="Explicit date conflicts with question time.")
        if ((analysis.time_mode != "as_of" and analysis.resolved_as_of is not None) or
                (request.as_of is not None and analysis.time_mode == "latest_known") or
                (request.as_of is not None and analysis.time_mode == "as_of" and request.as_of != analysis.resolved_as_of)):
            raise VectorAnswerError("QUERY_ANALYSIS_MISMATCH")
        originals = {m.memory_id: m for m in self._index.memories}
        if (len({m.memory_id for m in memories}) != len(memories) or
                any(originals.get(m.memory_id) != m for m in memories)):
            raise VectorAnswerError("CANDIDATES_INVALID")
        if not memories:
            self._report(request, analysis, [], [], None)
            return AnswerResult(status="no_evidence", uncertainty="No candidate memories are available.")

        if self._plain_text:
            return self._answer_plain(request, analysis, memories)

        # The legacy JSON envelope remains available for non-benchmark callers.
        # Benchmark runs use plain text so token-F1 is not penalized by an
        # unrelated answer/evidence envelope.
        question = json.dumps({"request": request.model_dump(mode="json"),
                               "analysis": analysis.model_dump(mode="json")}, ensure_ascii=False)
        question += ('\nReturn one JSON object as text with exactly status, answer, evidence_refs, uncertainty. '
                     'status is answered/no_evidence/time_uncertain/unresolved_conflict. '
                     'For answered give a nonblank answer and evidence_refs copied from context sources. '
                     'Otherwise set answer=null and explain uncertainty. '
                     'evidence_refs are objects with session_id and dia_id; never invent them.')
        retained, dropped = memories[:], []
        estimator = self._runtime.guarded._estimator
        strict = self._runtime.guarded._strict_budget
        estimate = None
        while retained:
            context = json.dumps([m.model_dump(mode="json") for m in retained], ensure_ascii=False)
            prepared = self._runtime.make_request(QA_PROMPT, {"context": context, "question": question})
            try:
                estimate = estimator(prepared.request.messages) if estimator else None
                if estimate is not None and (type(estimate) is not int or estimate < 0):
                    raise ValueError("invalid estimate")
            except Exception:
                raise VectorAnswerError("INVALID_ESTIMATE") from None
            if estimate is None:
                if strict:
                    self._report(request, analysis, retained, dropped, None)
                    raise VectorAnswerError("INPUT_BUDGET_UNKNOWN")
                break
            if estimate <= prepared.request.settings.max_input_tokens:
                break
            dropped.insert(0, retained.pop())  # preserve ranked prefix, whole Memory only
        self._report(request, analysis, retained, dropped, estimate if retained else None)
        if not retained:
            raise VectorAnswerError("INPUT_BUDGET_EXCEEDED")
        if self._runtime.backend == "mock" and (not self._synthetic or isinstance(self._runtime.adapter, MockLLMAdapter)):
            raise VectorAnswerError("SYNTHETIC_ANSWER_DISALLOWED")
        try:
            identity = self._runtime.make_identity(
                prepared, sample_id=request.sample_id, scope_id=request.query_id,
                run_kind=self._provenance.run_kind, method="vector_rag",
                data_fingerprint=self._provenance.data_fingerprint,
                schema_id="c-vector-answer-text", schema_version="1",
                retriever_fingerprint=self._index.retriever_fingerprint,
                query_analysis_version=self._provenance.query_analysis_version, git_sha=self._provenance.git_sha)
        except Exception:
            raise VectorAnswerError("IDENTITY_INVALID") from None
        try:
            result = self._runtime.execute(prepared, identity, validator=lambda req, resp: _decode(resp, retained))
        except Exception:
            raise VectorAnswerError("MODEL_FAILED") from None
        return _decode(result.response, retained)

    def _answer_plain(
        self,
        request: QueryRequest,
        analysis: QueryAnalysis,
        memories: list[MemoryRecord],
    ) -> AnswerResult:
        rendered_question = request.question
        if self._question_transform is not None:
            try:
                rendered_question = self._question_transform(request)
            except Exception:
                raise VectorAnswerError("QUESTION_PROTOCOL_INVALID") from None
            if not isinstance(rendered_question, str) or not rendered_question.strip():
                raise VectorAnswerError("QUESTION_PROTOCOL_INVALID")

        retained = memories[: self._max_context_memories]
        dropped = memories[len(retained):]
        estimator = self._runtime.guarded._estimator
        strict = self._runtime.guarded._strict_budget
        prepared = None
        estimate = None
        context = None
        while retained:
            context = build_hybrid_context(
                retained,
                self._source_repository,
                max_memories=len(retained),
                metadata_lookup=self._source_metadata_lookup,
            )
            prepared = self._runtime.make_request(
                self._prompt_template,
                {"context": context.text, "question": rendered_question},
            )
            try:
                estimate = estimator(prepared.request.messages) if estimator else None
                if estimate is not None and (type(estimate) is not int or estimate < 0):
                    raise ValueError("invalid estimate")
            except Exception:
                raise VectorAnswerError("INVALID_ESTIMATE") from None
            if estimate is None:
                if strict:
                    self._report(request, analysis, retained, dropped, None)
                    raise VectorAnswerError("INPUT_BUDGET_UNKNOWN")
                break
            if estimate <= prepared.request.settings.max_input_tokens:
                break
            dropped.insert(0, retained.pop())
        self._report(request, analysis, retained, dropped, estimate if retained else None)
        if not retained or prepared is None or context is None:
            raise VectorAnswerError("INPUT_BUDGET_EXCEEDED")
        if self._runtime.backend == "mock" and (
            not self._synthetic or isinstance(self._runtime.adapter, MockLLMAdapter)
        ):
            raise VectorAnswerError("SYNTHETIC_ANSWER_DISALLOWED")
        try:
            identity = self._runtime.make_identity(
                prepared,
                sample_id=request.sample_id,
                scope_id=request.query_id,
                run_kind=self._provenance.run_kind,
                method="vector_rag",
                data_fingerprint=self._provenance.data_fingerprint,
                schema_id="c-benchmark-plain-text",
                schema_version="1",
                retriever_fingerprint=self._index.retriever_fingerprint,
                query_analysis_version=self._provenance.query_analysis_version,
                git_sha=self._provenance.git_sha,
            )
        except Exception:
            raise VectorAnswerError("IDENTITY_INVALID") from None
        try:
            result = self._runtime.execute(
                prepared,
                identity,
                validator=lambda _request, response: _decode_plain(response, retained, context),
            )
        except Exception:
            raise VectorAnswerError("MODEL_FAILED") from None
        return _decode_plain(result.response, retained, context)
