"""Synthetic query semantics and time safety checks; no API or dataset."""

from datetime import date

import pytest

from dynamic_memory.contracts.models import QueryAnalysis, QueryRequest
from dynamic_memory.retrieval import QueryAnalyzer, analyze_query


def request(question, as_of=None):
    return QueryRequest(query_id="q1", sample_id="synthetic_001", question=question, as_of=as_of)


@pytest.mark.parametrize("question,intent", [
    ("Where does Sam work now?", "current_state"),
    ("Where does Sam work?", "current_state"),
    ("Where did Sam work?", "state_history"),
    ("What was Sam's job?", "historical_state"),
    ("Sam现在在哪里工作？", "current_state"),
    ("Where did Sam work on 2023-05-01?", "historical_state"),
    ("小王在2023年5月1日在哪里工作？", "historical_state"),
    ("Where has Sam ever worked?", "state_history"),
    ("小王曾经在哪里工作过？", "state_history"),
    ("When did Sam resign?", "event_fact"),
    ("Did Sam open a shop?", "event_fact"),
    ("What happened on May 1, 2023?", "event_fact"),
    ("小王什么时候离职？", "event_fact"),
    ("What is Sam's favorite movie?", "general_fact"),
    ("小王最喜欢哪部电影？", "general_fact"),
    ("???", "unknown"),
])
def test_intent(question, intent):
    result = analyze_query(request(question))
    assert result.query_intent == intent
    assert result.query_id == "q1"
    assert result.sample_id == "synthetic_001"
    assert QueryAnalysis.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize("question", ["Where has Sam ever worked?", "When did Sam resign?", "What is Sam's favorite movie?", "???"])
def test_latest_known_does_not_force_current_state(question):
    result = analyze_query(request(question))
    assert result.time_mode == "latest_known"
    assert result.resolved_as_of is None
    assert result.time_issue is None
    assert result.query_intent != "current_state"


@pytest.mark.parametrize("raw", ["2023-05-01", "2023年5月1日", "May 1, 2023", "1 May 2023", "May 1st 2023", "2024-02-29"])
def test_exact_dates(raw):
    result = analyze_query(request(f"Where did Sam work on {raw}?"))
    assert result.time_mode == "as_of"
    assert result.resolved_as_of == (date(2024, 2, 29) if raw == "2024-02-29" else date(2023, 5, 1))


def test_explicit_date_and_matching_question_time():
    explicit = date(2023, 5, 1)
    for text in ("Where does Sam work?", "Where did Sam work on May 1, 2023?"):
        result = analyze_query(request(text, explicit))
        assert result.time_mode == "as_of"
        assert result.resolved_as_of == explicit
        assert result.time_issue is None


@pytest.mark.parametrize("raw", ["May 1, 2023", "2022", "2022年5月"])
def test_explicit_date_conflict_preserves_both_inputs(raw):
    result = analyze_query(request(f"Where did Sam work in {raw}?", date(2023, 6, 1)))
    assert result.time_mode == "conflict"
    assert result.resolved_as_of is None
    assert result.time_issue.code == "QUERY_TIME_CONFLICT"
    assert result.time_issue.explicit_as_of == "2023-06-01"
    assert result.time_issue.question_time == raw


@pytest.mark.parametrize("raw", [
    "2023", "2023-05", "May 2023", "2023年5月", "last year", "yesterday", "去年", "上周",
    "2023-02-29", "2023-13-01", "03/04/2023", "May", "5月",
    "before 2023-05-01", "after 2023-05-01", "since 2023-05-01",
    "between 2023-05-01 and 2023-06-01", "2023年5月1日之前", "从2023年5月1日起",
    "before graduation", "two years ago", "last summer", "around 2023-05-01",
])
def test_ambiguous_invalid_relative_or_interval_dates_are_not_guessed(raw):
    result = analyze_query(request(f"Where did Sam work {raw}?"))
    assert result.time_mode == "unknown"
    assert result.resolved_as_of is None
    assert result.time_issue.code == "TIME_PARSE_UNCERTAIN"
    assert raw in result.time_issue.question_time


def test_explicit_date_does_not_hide_unresolved_time_or_collapse_interval():
    for raw in ("last year", "May 2023", "between 2023-05-01 and 2023-06-01"):
        result = analyze_query(request(f"Where did Sam work {raw}?", date(2023, 5, 10)))
        assert result.time_mode == "unknown"
        assert result.resolved_as_of is None
        assert result.time_issue.explicit_as_of == "2023-05-10"


def test_same_question_different_explicit_dates_and_no_mutation():
    first = request("Where has Sam ever worked?", date(2023, 1, 1))
    before = first.model_dump()
    result = analyze_query(first)
    later = analyze_query(request(first.question, date(2024, 1, 1)))
    assert first.model_dump() == before
    assert result.query_intent == later.query_intent == "state_history"
    assert result.resolved_as_of != later.resolved_as_of


def test_entity_and_attribute_hints():
    result = analyze_query(request("Where does Sam work now?"))
    assert result.entity_hints == ["Sam"]
    assert result.attribute_hints == ["employment"]
    assert analyze_query(request("Where does he live?")).entity_hints == []


def test_sample_local_entity_vocabulary_and_word_boundaries():
    first = QueryAnalyzer(["Sam", "小王", "Sam"])
    second = QueryAnalyzer(["Alex"])
    assert first.analyze(request("小王现在在哪里工作？")).entity_hints == ["小王"]
    assert first.analyze(request("Where does Samantha work?")).entity_hints == []
    assert first.analyze(request("Where does sam work?")).entity_hints == ["Sam"]
    assert second.analyze(request("Where does Sam work?")).entity_hints == []


def test_date_tokens_do_not_become_entities():
    result = analyze_query(request("Where did Sam work on May 1, 2023?"))
    assert result.entity_hints == ["Sam"]


def test_multiple_question_dates_not_treated_as_single_conflicting_date():
    result = analyze_query(request("Where did Sam work on 2023-05-01 and 2023-06-01?", date(2023, 5, 1)))
    assert result.time_mode == "unknown"
    assert result.time_issue.code == "TIME_PARSE_UNCERTAIN"


def test_preference_with_explicit_date_keeps_independent_intent():
    result = analyze_query(request("What was Sam's favorite movie on 2023-05-01?"))
    assert result.query_intent == "general_fact"
    assert result.time_mode == "as_of"


def test_chinese_move_to_is_not_a_date_range():
    result = analyze_query(request("小王在2023年5月1日搬到哪里？"))
    assert result.time_mode == "as_of"


@pytest.mark.parametrize("names", ["Sam", [""], [None]])
def test_invalid_entity_vocabulary(names):
    with pytest.raises(ValueError):
        QueryAnalyzer(names)


@pytest.mark.parametrize("question", [
    "Where did Sam work on 03/04/2023?",
    "Where did Sam work on May 1, 2023 or last year?",
])
def test_unresolved_question_time_cannot_prove_explicit_date_conflict(question):
    result = analyze_query(request(question, date(2024, 6, 1)))
    assert result.time_mode == "unknown"
    assert result.time_issue.code == "TIME_PARSE_UNCERTAIN"
    assert result.time_issue.explicit_as_of == "2024-06-01"
    assert result.resolved_as_of is None


@pytest.mark.parametrize("question", [
    "Where did Sam work by May 1, 2023?",
    "Where did Sam work not on May 1, 2023?",
    "Where did Sam work on Friday?",
])
def test_bounds_exclusions_and_unanchored_weekdays_are_not_exact_dates(question):
    result = analyze_query(request(question))
    assert result.time_mode == "unknown"
    assert result.time_issue.code == "TIME_PARSE_UNCERTAIN"
    assert result.resolved_as_of is None


def test_surrounding_whitespace_does_not_change_interpretation():
    plain = request("What is Sam's identity?")
    padded = request("  What is Sam's identity?  ")
    assert analyze_query(plain) == analyze_query(padded)
    assert padded.question == "  What is Sam's identity?  "
