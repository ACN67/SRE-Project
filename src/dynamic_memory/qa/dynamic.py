"""Dynamic QA uses only resolved effective memories, with bounded citations."""

from __future__ import annotations

import json
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict

from dynamic_memory.contracts import AnswerResult, SourceRef
from dynamic_memory.data.repository import SourceRepository
from dynamic_memory.llm.prompts import QA_PROMPT
from dynamic_memory.qa.benchmark_protocol import build_hybrid_context, context_refs
from dynamic_memory.reasoning.common import (
    DynamicComponent, DynamicReasoningError, checked_inputs, validate_resolution,
)


class _AnswerDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    status: Literal[
        "answered", "no_evidence", "time_uncertain", "unresolved_conflict"
    ]
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
        if (
            response.output_mode != "text"
            or response.finish_reason not in {None, "stop"}
        ):
            raise ValueError("incomplete response")
        draft = _AnswerDraft.model_validate(
            json.loads(
                response.raw_output,
                object_pairs_hook=_unique,
                parse_constant=lambda _: (_ for _ in ()).throw(
                    ValueError("invalid constant")
                ),
            )
        )
        refs = {
            (ref.session_id, ref.dia_id): ref
            for memory in memories for ref in memory.source_refs
        }
        evidence = [(ref.session_id, ref.dia_id) for ref in draft.evidence_refs]
        if (
            len(evidence) != len(set(evidence))
            or not set(evidence) <= refs.keys()
        ):
            raise ValueError("evidence outside actual context")
        if draft.status == "answered":
            if not evidence or not (draft.answer or "").strip():
                raise ValueError("confirmed answer needs evidence")
        elif draft.answer is not None or not (draft.uncertainty or "").strip():
            raise ValueError("uncertain result must explain uncertainty")
        return AnswerResult(
            status=draft.status,
            answer=draft.answer,
            used_memory_ids=[memory.memory_id for memory in memories],
            context_source_refs=list(refs.values()),
            evidence_refs=draft.evidence_refs,
            uncertainty=draft.uncertainty,
        )
    except Exception:
        raise DynamicReasoningError("ANSWER_INVALID") from None


class DynamicAnswerer(DynamicComponent):
    def __init__(
        self,
        runtime,
        provenance,
        *,
        source_repository: SourceRepository | None = None,
        source_metadata_lookup: Callable | None = None,
        question_transform: Callable | None = None,
        prompt_template=QA_PROMPT,
        plain_text: bool = False,
        max_context_memories: int = 12,
        synthetic_test_mode: bool = False,
        on_diagnostic=None,
    ):
        super().__init__(
            runtime,
            provenance,
            synthetic_test_mode=synthetic_test_mode,
            on_diagnostic=on_diagnostic,
        )
        if source_repository is not None and type(source_repository) is not SourceRepository:
            raise DynamicReasoningError("DEPENDENCY_INVALID")
        if source_metadata_lookup is not None and not callable(source_metadata_lookup):
            raise DynamicReasoningError("DEPENDENCY_INVALID")
        if question_transform is not None and not callable(question_transform):
            raise DynamicReasoningError("DEPENDENCY_INVALID")
        if (
            type(plain_text) is not bool
            or type(max_context_memories) is not int
            or max_context_memories < 1
        ):
            raise DynamicReasoningError("DEPENDENCY_INVALID")
        self.source_repository = source_repository
        self.source_metadata_lookup = source_metadata_lookup
        self.question_transform = question_transform
        self.prompt_template = prompt_template
        self.plain_text = plain_text
        self.max_context_memories = max_context_memories

    def __call__(self, request, analysis, resolution, candidates):
        return self.answer_dynamic(request, analysis, resolution, candidates)

    def answer_dynamic(self, request, analysis, resolution, candidates):
        request, analysis, memories = checked_inputs(request, analysis, candidates)
        resolution = validate_resolution(resolution, request, memories)
        status = None
        reason = resolution.uncertainty
        if not memories and analysis.time_mode != "conflict":
            status, reason = "no_evidence", "No candidate evidence is available."
        elif analysis.time_mode == "conflict":
            status = "time_uncertain"
            reason = "The explicit time and the question time conflict."
        elif resolution.unresolved_conflict_ids:
            status = "unresolved_conflict"
            reason = reason or "Relevant contradictory evidence cannot be resolved."
        elif "candidate_incomplete" in (reason or "").casefold():
            status = "no_evidence"
        elif any(decision.action == "UNRESOLVED" for decision in resolution.decisions):
            status = (
                "time_uncertain"
                if analysis.query_intent in {
                    "current_state", "historical_state", "state_history", "event_fact"
                }
                else "no_evidence"
            )
            reason = reason or "Candidate evidence is insufficient to confirm the answer."
        elif not resolution.effective_memory_ids:
            status = "no_evidence"
            reason = reason or "No effective evidence supports this question."
        if status:
            self.report("answer", request, analysis, memories, [], status)
            return AnswerResult(status=status, uncertainty=reason)
        retained = [
            memory for memory in memories
            if memory.memory_id in set(resolution.effective_memory_ids)
        ]
        if self.plain_text:
            return self._answer_plain(request, analysis, memories, retained)
        question = json.dumps({
            "request": request.model_dump(mode="json"),
            "analysis": analysis.model_dump(mode="json"),
        }, ensure_ascii=False)
        question += (
            "\nReturn one JSON object as text with exactly status, answer, evidence_refs, uncertainty. "
            "status is answered/no_evidence/time_uncertain/unresolved_conflict. "
            "Use only the supplied effective memory facts for private conversational claims. "
            "For answered give a nonblank answer and nonempty evidence_refs copied from the context sources. "
            "Otherwise answer=null and explain uncertainty. evidence_refs contain session_id and dia_id. "
            "Never infer unsupported persistence, treat plans as accomplished, or invent sources."
        )
        variables = {
            "context": json.dumps(
                [memory.model_dump(mode="json") for memory in retained],
                ensure_ascii=False,
            ),
            "question": question,
        }
        answer, call = self.execute(
            QA_PROMPT,
            variables,
            request,
            lambda _request, response: _decode(response, retained),
            "d-answer-text",
        )
        self.report(
            "answer", request, analysis, memories, retained, answer.status, call
        )
        return answer

    def _answer_plain(self, request, analysis, candidates, retained):
        rendered_question = request.question
        if self.question_transform is not None:
            try:
                rendered_question = self.question_transform(request)
            except Exception:
                raise DynamicReasoningError("QUESTION_PROTOCOL_INVALID") from None
            if not isinstance(rendered_question, str) or not rendered_question.strip():
                raise DynamicReasoningError("QUESTION_PROTOCOL_INVALID")
        retained = retained[: self.max_context_memories]
        context = build_hybrid_context(
            retained,
            self.source_repository,
            max_memories=len(retained),
            metadata_lookup=self.source_metadata_lookup,
        )
        refs = list(context_refs(context))
        answer, call = self.execute(
            self.prompt_template,
            {"context": context.text, "question": rendered_question},
            request,
            lambda _request, response: AnswerResult(
                status="answered",
                answer=response.raw_output.strip(),
                used_memory_ids=[memory.memory_id for memory in retained],
                context_source_refs=refs,
                evidence_refs=refs,
            ),
            "d-benchmark-plain-text",
        )
        self.report("answer", request, analysis, candidates, retained, answer.status, call)
        return answer
