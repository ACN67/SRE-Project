"""LoCoMo QA rule subset pinned to snap-research/locomo cbfbc1dba6bc53d00625212a0f22d55ffee7c1fc.

Only normalize_answer, f1_score, f1 and the five eval_question_answering
category branches are reproduced. This module performs no Gold access or I/O.
"""

from __future__ import annotations

import string
from collections import Counter

import regex
from nltk.stem import PorterStemmer

from evaluation.question_manifest import EvaluationInputError

_STEMMER = PorterStemmer()
_PUNCTUATION = set(string.punctuation)
_ARTICLES = regex.compile(r"\b(a|an|the|and)\b")


def normalize_answer(value: str) -> str:
    """Match the pinned upstream's comma, lowercase, ASCII punctuation, article order."""
    if not isinstance(value, str):
        raise EvaluationInputError("QA_SCORE_INPUT_INVALID")
    value = value.replace(",", "").lower()
    value = "".join(character for character in value if character not in _PUNCTUATION)
    return " ".join(_ARTICLES.sub(" ", value).split())


def official_token_f1(prediction: str, gold_answer: str) -> float:
    if not isinstance(prediction, str) or not isinstance(gold_answer, str):
        raise EvaluationInputError("QA_SCORE_INPUT_INVALID")
    predicted = [_STEMMER.stem(token) for token in normalize_answer(prediction).split()]
    expected = [_STEMMER.stem(token) for token in normalize_answer(gold_answer).split()]
    common = sum((Counter(predicted) & Counter(expected)).values())
    if common == 0:
        return 0.0
    precision = common / len(predicted)
    recall = common / len(expected)
    return (2 * precision * recall) / (precision + recall)


def score_official_qa(category: int, adapted_prediction: str, gold_answer: str) -> float:
    """Score one non-error, Gold-present answer using pinned category rules."""
    if type(category) is not int or category not in (1, 2, 3, 4, 5):
        raise EvaluationInputError("QA_CATEGORY_INVALID")
    if not isinstance(adapted_prediction, str) or not isinstance(gold_answer, str):
        raise EvaluationInputError("QA_SCORE_INPUT_INVALID")
    if category == 1:
        predicted_parts = [part.strip() for part in adapted_prediction.split(",")]
        gold_parts = [part.strip() for part in gold_answer.split(",")]
        return sum(max(official_token_f1(predicted, gold) for predicted in predicted_parts)
                   for gold in gold_parts) / len(gold_parts)
    if category == 3:
        gold_answer = gold_answer.split(";")[0].strip()
    if category in (2, 3, 4):
        return official_token_f1(adapted_prediction, gold_answer)
    lowered = adapted_prediction.lower()
    return float("no information available" in lowered or "not mentioned" in lowered)
