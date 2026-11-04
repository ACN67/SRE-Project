"""Model review uses recorded quotes and never inherits human identity."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from dynamic_memory.contracts import SourceRef, SourceTurn
from dynamic_memory.memory.extraction import AtomicFactDraft
from dynamic_memory.memory.review_workflow import (
    QueueFact, QueueWindow, ReviewError, ReviewQueue, _fp, verify_review_files,
)
from dynamic_memory.memory.semantic_review import (
    SEMANTIC_REVIEWER_ID, _window_decision, run_semantic_review,
)


def _queue():
    turn = SourceTurn(sample_id="synthetic-semantic", session_id="session_1",
        dia_id="D1:1", speaker="Jon", text="I rehearsed dancing yesterday.")
    draft = AtomicFactDraft(content="Jon rehearsed dancing yesterday.", entity="Jon",
        attribute="activity", value="rehearsed dancing", fact_kind="event",
        assertion_type="fact", polarity="positive", recorded_at=None,
        event_time=None, source_refs=[SourceRef(session_id="session_1", dia_id="D1:1")])
    fact = QueueFact(draft=draft, cited_turns=(turn,),
        draft_fingerprint=_fp(draft.model_dump(mode="json")),
        evidence_fingerprint=_fp([turn.model_dump(mode="json")]),
        automatic_status="unclear", automatic_reason="PARAPHRASE_NEEDS_REVIEW")
    return ReviewQueue(run_fingerprint=_fp("run"), sample_id="synthetic-semantic",
        windows=(QueueWindow(window_id="window_" + "a" * 24,
            call_fingerprint=_fp("call"), source_refs_fingerprint=_fp("refs"),
            facts=(fact,)),))


def _content(quote):
    return json.dumps({"items": [{"index": 0, "decision": "supported",
        "evidence": [{"source_index": 0, "quote": quote}],
        "relation_confirmed": False, "reason": "The speaker confirms rehearsal."}]})


def _risk_queue(content, text, *, assertion_type="fact", caption=None):
    turn = SourceTurn(sample_id="synthetic-semantic", session_id="session_1",
        dia_id="D1:1", speaker="Jon", text=text, image_caption=caption,
        caption_origin="blip_caption" if caption else None)
    draft = AtomicFactDraft(content=content, entity="Jon", attribute="activity",
        value="activity", fact_kind="event", assertion_type=assertion_type,
        polarity="positive", recorded_at=None, event_time=None,
        source_refs=[SourceRef(session_id="session_1", dia_id="D1:1")])
    fact = QueueFact(draft=draft, cited_turns=(turn,),
        draft_fingerprint=_fp(draft.model_dump(mode="json")),
        evidence_fingerprint=_fp([turn.model_dump(mode="json")]),
        automatic_status="unclear", automatic_reason="PARAPHRASE_NEEDS_REVIEW")
    return ReviewQueue(run_fingerprint=_fp("run"), sample_id="synthetic-semantic",
        windows=(QueueWindow(window_id="window_" + "a" * 24,
            call_fingerprint=_fp("call"), source_refs_fingerprint=_fp("refs"),
            facts=(fact,)),))


def test_semantic_review_records_response_and_verifies_receipt(tmp_path, monkeypatch):
    calls = []
    class Client:
        def __init__(self, **kwargs):
            assert kwargs["max_retries"] == 0
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(model="deepseek-flash", usage=None,
                choices=[SimpleNamespace(finish_reason="stop",
                    message=SimpleNamespace(content=_content("I rehearsed dancing yesterday.")))])
        @property
        def chat(self):
            return SimpleNamespace(completions=self)
    monkeypatch.setattr("openai.OpenAI", Client)
    queue = _queue()
    summary = run_semantic_review(queue, tmp_path / "review", max_new_calls=1,
        api_key="synthetic-key", run_kind="offline_test")
    assert summary["approved_facts"] == 1
    assert summary["new_http_calls"] == 1
    assert len(calls) == 1 and "max_tokens" not in calls[0]
    assert calls[0]["reasoning_effort"] == "high"
    approval = tmp_path / "review" / "semantic_review_approval.json"
    authority = tmp_path / "review" / "semantic_review_authority.json"
    verified = verify_review_files(queue, approval, authority, run_kind="offline_test")
    assert verified.approval.reviewer_id == SEMANTIC_REVIEWER_ID
    assert verified.checker_for_window(queue.windows[0].window_id)(
        queue.windows[0].facts[0].draft,
        queue.windows[0].facts[0].cited_turns).reason_code == "LLM_SEMANTIC_APPROVED"
    replay = run_semantic_review(queue, tmp_path / "review", max_new_calls=0,
        api_key=None, run_kind="offline_test")
    assert replay["new_http_calls"] == 0 and replay["approved_facts"] == 1
    receipt = tmp_path / "review" / "semantic_receipts.json"
    receipt.write_bytes(receipt.read_bytes().replace(b"deepseek-flash", b"wrong-model"))
    with pytest.raises(ReviewError, match="REVIEW_REJECTED_OR_STALE"):
        verify_review_files(queue, approval, authority, run_kind="offline_test")


def test_semantic_resealed_approval_must_match_recorded_response(tmp_path, monkeypatch):
    class Client:
        def __init__(self, **kwargs):
            pass
        def create(self, **kwargs):
            return SimpleNamespace(model="deepseek-flash", usage=None,
                choices=[SimpleNamespace(finish_reason="stop",
                    message=SimpleNamespace(content=_content("I rehearsed dancing yesterday.")))])
        @property
        def chat(self):
            return SimpleNamespace(completions=self)
    monkeypatch.setattr("openai.OpenAI", Client)
    queue = _queue()
    run_semantic_review(queue, tmp_path / "review", max_new_calls=1,
        api_key="synthetic-key", run_kind="offline_test")
    approval_path = tmp_path / "review" / "semantic_review_approval.json"
    authority_path = tmp_path / "review" / "semantic_review_authority.json"
    approval = json.loads(approval_path.read_bytes())
    approval["windows"][0]["facts"][0].update(
        decision="rejected", reason_code="LLM_CONTRADICTED",
        confirmed_source_fingerprints=[])
    payload = (json.dumps(approval, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")) + "\n").encode()
    approval_path.write_bytes(payload)
    authority = json.loads(authority_path.read_bytes())
    authority["approval_file_sha256"] = "sha256:" + hashlib.sha256(payload).hexdigest()
    authority_path.write_text(json.dumps(authority), encoding="utf-8")
    with pytest.raises(ReviewError, match="REVIEW_REJECTED_OR_STALE"):
        verify_review_files(queue, approval_path, authority_path, run_kind="offline_test")


def test_semantic_review_rejects_unanchored_support():
    decision = _window_decision(_queue().windows[0], _content("a quote the source never said"))
    assert decision.facts[0].decision == "unclear"
    assert decision.facts[0].reason_code == "LLM_EVIDENCE_INVALID"


@pytest.mark.parametrize("claim,source,assertion_type,caption,reason", [
    ("Jon has a dance studio.", "You're gonna do great with your dance studio.",
     "fact", None, "LLM_GUARD_FUTURE_AS_CURRENT"),
    ("Jon plans to work with artists.", "I'm thinking of working with artists soon.",
     "plan", None, "LLM_GUARD_CONSIDERATION_AS_PLAN"),
    ("Jon was dancing in the photo.", "Here's a photo from when I was dancing.",
     "fact", "a person posing for a photo", "LLM_GUARD_VISUAL_CLAIM"),
    ("Jon loves building/running the shop.", "It's been a wild ride but I love it.",
     "fact", None, "LLM_GUARD_COMPOSITE_REFERENT"),
])
def test_guarded_semantic_review_abstains_on_scope_traps(
        claim, source, assertion_type, caption, reason):
    queue = _risk_queue(claim, source, assertion_type=assertion_type,
                        caption=caption)
    decision = _window_decision(queue.windows[0], _content(source), queue=queue)
    assert decision.facts[0].decision == "unclear"
    assert decision.facts[0].reason_code == reason


def test_guarded_semantic_review_abstains_on_exit_history_conflict():
    left = _risk_queue("Jon left his banker job.", "I left my banker job last year.")
    lost = _risk_queue("Jon lost his banker job.", "I lost my banker job last year.")
    window = left.windows[0].model_copy(update={
        "facts": (left.windows[0].facts[0], lost.windows[0].facts[0])})
    queue = left.model_copy(update={"windows": (window,)})
    content = json.dumps({"items": [
        {"index": 0, "decision": "supported", "evidence": [
            {"source_index": 0, "quote": "I left my banker job last year."}],
         "relation_confirmed": False, "reason": "Direct assertion."},
        {"index": 1, "decision": "supported", "evidence": [
            {"source_index": 0, "quote": "I lost my banker job last year."}],
         "relation_confirmed": False, "reason": "Direct assertion."}]})
    decision = _window_decision(window, content, queue=queue)
    assert decision.facts[0].reason_code == "LLM_GUARD_EXIT_HISTORY_CONFLICT"
