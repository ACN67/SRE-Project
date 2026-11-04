"""Dynamic QA uses only resolved effective memories, with bounded citations."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict

from dynamic_memory.contracts import AnswerResult, SourceRef
from dynamic_memory.llm.prompts import QA_PROMPT
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
    def __call__(self, request, analysis, resolution, candidates):
        return self.answer_dynamic(request, analysis, resolution, candidates)

    def answer_dynamic(self, request, analysis, resolution, candidates):
        request, analysis, memories = checked_inputs(request, analysis, candidates)
        resolution = validate_resolution(resolution, request, memories)
        status = None
        reason = resolution.uncertainty
        if not memories and analysis.time_mode != "conflict":
            status, reason = "no_evidence", "No candidate evidence is available."
        elif (
            analysis.time_mode in {"conflict", "unknown"}
            or analysis.time_issue is not None
        ):
            status = "time_uncertain"
            reason = "Shared query time conditions cannot be resolved."
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
