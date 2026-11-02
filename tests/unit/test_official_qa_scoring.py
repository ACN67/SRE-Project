"""Hand-calculated, offline examples for pinned LoCoMo QA category behavior."""

import pytest

from evaluation.metrics.official_qa import normalize_answer, official_token_f1, score_official_qa
from evaluation.question_manifest import EvaluationInputError


def test_normalization_order_and_porter_stems():
    assert normalize_answer("  The, RUNNING and a DOG!!!  ") == "running dog"
    assert normalize_answer("A,B") == "ab"  # upstream removes commas before punctuation
    assert official_token_f1("Running and Dogs", "run dog") == 1.0
    assert official_token_f1("cat cat", "cat") == pytest.approx(2 / 3)
    assert official_token_f1("cat", "dog") == 0.0
    assert official_token_f1("", "") == 0.0


@pytest.mark.parametrize("category,prediction,gold,expected", [
    (1, "blue, red", "red, blue", 1.0),
    (1, "alpha,,beta", "beta, alpha,", 2 / 3),
    (1, "green", "red, blue", 0.0),
    (2, "run", "running", 1.0),
    (2, "2020 2021", "2020", 2 / 3),
    (3, "Paris", "Paris; France", 1.0),
    (3, "Paris; France", "Paris; France", 2 / 3),
    (4, "apple", "apple apple", 2 / 3),
    (5, "No Information Available", "anything", 1.0),
    (5, "It is NOT MENTIONED here", "anything", 1.0),
    (5, "I don't know", "anything", 0.0),
])
def test_five_official_categories(category, prediction, gold, expected):
    assert score_official_qa(category, prediction, gold) == pytest.approx(expected)


def test_invalid_score_inputs_fail_without_private_text():
    with pytest.raises(EvaluationInputError, match="QA_CATEGORY_INVALID"):
        score_official_qa(0, "synthetic", "SECRET_GOLD")
    with pytest.raises(EvaluationInputError, match="QA_SCORE_INPUT_INVALID") as error:
        score_official_qa(2, None, "SECRET_GOLD")
    assert "SECRET_GOLD" not in str(error.value)
