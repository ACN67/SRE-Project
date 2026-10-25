"""Pure, Gold-independent conversion from public answers to scoring inputs."""

from __future__ import annotations

from dataclasses import dataclass

from dynamic_memory.contracts import AnswerResult


@dataclass(frozen=True, slots=True)
class ScoringInput:
    original_status: str
    adapted_text: str | None
    execution_failed: bool


_FIXED = {
    "no_evidence": "No information available",
    "time_uncertain": "Unable to determine the time from available evidence",
    "unresolved_conflict": "Conflicting information in the conversation",
}


def adapt_answer(answer: AnswerResult) -> ScoringInput:
    """Keep execution errors separate; category-specific scoring comes later."""
    if type(answer) is not AnswerResult:
        raise TypeError("AnswerResult required")
    try:
        if set(answer.__dict__) != set(AnswerResult.model_fields) or answer.__pydantic_extra__:
            raise ValueError("unexpected AnswerResult fields")
        checked = AnswerResult.model_validate_json(answer.model_dump_json())
    except Exception:
        raise ValueError("invalid AnswerResult") from None
    if checked.status == "error":
        return ScoringInput("error", None, True)
    return ScoringInput(checked.status,
                        checked.answer if checked.status == "answered" else _FIXED[checked.status],
                        False)
