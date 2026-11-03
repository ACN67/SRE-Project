"""Conservative human-review boundary, with no raw source text in errors."""

from dynamic_memory.contracts import SourceTurn, TimeSpan
from dynamic_memory.memory.extraction import AtomicFactDraft
from dynamic_memory.memory.strict_evidence import strict_evidence_checker


def turn(text="Ada moved to Paris.", *, caption=None):
    return SourceTurn(sample_id="synthetic", session_id="session_1", dia_id="d1",
                      speaker="Ada", text=text, image_caption=caption,
                      caption_origin="blip_caption" if caption else None)


def draft(**changes):
    fields = dict(content="Ada moved to Paris.", entity="Ada", attribute=None,
                  value="Paris", fact_kind="event", assertion_type="fact",
                  polarity="positive", recorded_at=None, event_time=None,
                  source_refs=[{"session_id": "session_1", "dia_id": "d1"}])
    fields.update(changes)
    return AtomicFactDraft(**fields)


def test_exact_supported_sentence_and_negative_cases_are_reviewed():
    assert strict_evidence_checker(draft(), (turn(),)).status == "supported"
    assert strict_evidence_checker(draft(), (turn("Ada moved to London."),)).reason_code == "VALUE_NOT_VISIBLE"
    assert strict_evidence_checker(draft(), (turn("Bob moved to Paris."),)).reason_code == "ENTITY_NOT_VISIBLE"
    assert strict_evidence_checker(draft(content="Ada lives in Paris."), (turn(),)).reason_code == "PARAPHRASE_NEEDS_REVIEW"


def test_negation_plan_time_caption_and_relation_need_evidence():
    assert strict_evidence_checker(draft(), (turn("Ada did not move to Paris."),)).reason_code == "NEGATION_NEEDS_REVIEW"
    assert strict_evidence_checker(draft(content="Ada plans to move to Paris."),
                                   (turn("Ada plans to move to Paris."),)).reason_code == "PLAN_NEEDS_REVIEW"
    date = TimeSpan(raw="2024-01-01", precision="day", resolution="explicit")
    assert strict_evidence_checker(draft(event_time=date), (turn(),)).reason_code == "TIME_NOT_VISIBLE"
    assert strict_evidence_checker(draft(), (turn("", caption="Ada moved to Paris."),)).reason_code == "CAPTION_NOT_SPEECH"
    assert strict_evidence_checker(draft(), (turn("Ada visited Paris. Bob moved to Berlin."),)).status == "unclear"


def test_no_source_and_short_claim_fail_closed_without_text_leak():
    decision = strict_evidence_checker(draft(), ())
    assert decision.reason_code == "SOURCE_SCOPE_INVALID"
    short = strict_evidence_checker(draft(content="Ada moved.", value=None),
                                    (turn("Ada moved."),))
    assert short.reason_code == "CLAIM_TOO_SHORT"
    assert "Paris" not in repr(decision)


def test_entity_and_value_must_be_in_same_cited_sentence():
    source = turn("Ada moved to Paris. Bob filed a report.")
    assert strict_evidence_checker(draft(value="report"), (source,)).reason_code == "VALUE_NOT_VISIBLE"
    assert strict_evidence_checker(draft(entity="Bob"), (source,)).reason_code == "ENTITY_NOT_VISIBLE"
    assert strict_evidence_checker(draft(attribute="residence"), (source,)).reason_code == "ATTRIBUTE_NOT_VISIBLE"
    assert strict_evidence_checker(draft(assertion_type="plan"), (turn(),)).reason_code == "PLAN_NEEDS_REVIEW"
