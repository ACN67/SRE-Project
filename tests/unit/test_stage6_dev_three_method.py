"""Contract checks for the fixed development three-method runner."""

from scripts import stage6_dev_three_method as runner


def test_question_manifest_is_fixed_to_four_main_dev_questions():
    assert runner.MAIN_DEV_SAMPLES == (
        "conv-26", "conv-30", "conv-41", "conv-42",
    )
    assert runner.TEST_SAMPLES == ("conv-49", "conv-50")
    assert runner.ALLOWED_SAMPLES == runner.MAIN_DEV_SAMPLES + runner.TEST_SAMPLES
    assert tuple(runner.QUESTIONS) == (
        "conv-26", "conv-30", "conv-41", "conv-42",
    )
    for sample_id, questions in runner.QUESTIONS.items():
        assert sample_id in {"conv-26", "conv-30", "conv-41", "conv-42"}
        assert len(questions) == 4
        assert tuple(item["index"] for item in questions) == (2, 25, 54, 80)
        assert all(item["question"].strip() for item in questions)


def test_budget_excludes_deterministic_state_resolution_calls():
    budget = runner._budget(4)
    assert budget == {
        "max_new_http_attempts": 12,
        "qa_calls": 12,
        "state_reasoning_calls": 0,
        "qa_input_tokens_per_call": 32768,
        "state_reasoning_input_tokens_per_call": 934464,
        "provider_default_output_tokens_per_call": 65536,
        "worst_case_usd_per_qa_call": "0.0884736",
        "worst_case_usd_per_reasoning_call": "0.3589824",
        "worst_case_usd_total": "1.0616832",
        "retry_attempts": 0,
    }

    full = runner._budget(199)
    assert full["max_new_http_attempts"] == 597
    assert full["qa_calls"] == 597
    assert full["state_reasoning_calls"] == 0
