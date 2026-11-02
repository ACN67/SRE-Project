"""Fixed, category-free scoring input mappings for five public statuses."""

import pytest

from dynamic_memory.contracts import AnswerResult, ErrorInfo
from evaluation.metrics.answer_adapter import adapt_answer


@pytest.mark.parametrize("status,expected", [
    ("no_evidence", "No information available"),
    ("time_uncertain", "Unable to determine the time from available evidence"),
    ("unresolved_conflict", "Conflicting information in the conversation"),
])
def test_fixed_uncertainty_text(status, expected):
    answer = AnswerResult(status=status, uncertainty="Synthetic reason")
    before = answer.model_dump_json()
    result = adapt_answer(answer)
    assert result.original_status == status and result.adapted_text == expected
    assert not result.execution_failed and answer.model_dump_json() == before


def test_answered_preserves_raw_answer_exactly():
    text = "  Synthetic answer, punctuation!  "
    result = adapt_answer(AnswerResult(status="answered", answer=text))
    assert result.adapted_text == text and not result.execution_failed


def test_error_is_failure_not_correct_rejection():
    result = adapt_answer(AnswerResult(status="error",
                                       error=ErrorInfo(code="MODEL_FAILED", message="Synthetic failure")))
    assert result.original_status == "error"
    assert result.adapted_text is None and result.execution_failed


def test_injected_answer_field_is_rejected():
    altered = AnswerResult(status="answered", answer="Synthetic").model_copy(
        update={"category": 5})
    with pytest.raises(ValueError, match="invalid AnswerResult"):
        adapt_answer(altered)
