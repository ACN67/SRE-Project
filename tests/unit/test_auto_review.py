"""Automatic review stays machine-identified and fails closed on weak evidence."""

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from dynamic_memory.contracts import SourceRef, SourceTurn
from dynamic_memory.memory.auto_review import AUTO_REVIEWER_ID, assess_auto_fact, write_auto_review_files
from dynamic_memory.memory.extraction import AtomicFactDraft
from dynamic_memory.memory.review_workflow import (
    QueueFact, QueueWindow, ReviewError, ReviewQueue, TrustedReviewAuthority,
    _fp, verify_review_files,
)


def _fact(content, text, *, speaker="Jon", assertion_type="fact", caption=None):
    turn = SourceTurn(sample_id="synthetic-auto", session_id="session_1", dia_id="D1:1",
                      speaker=speaker, text=text, image_caption=caption,
                      caption_origin="blip_caption" if caption else None)
    draft = AtomicFactDraft(content=content, entity=speaker, attribute="activity",
        value="dance", fact_kind="event", assertion_type=assertion_type,
        polarity="positive", recorded_at=None, event_time=None,
        source_refs=[SourceRef(session_id="session_1", dia_id="D1:1")])
    return QueueFact(draft=draft, cited_turns=(turn,),
        draft_fingerprint=_fp(draft.model_dump(mode="json")),
        evidence_fingerprint=_fp([turn.model_dump(mode="json")]),
        automatic_status="unclear", automatic_reason="PARAPHRASE_NEEDS_REVIEW")


def test_auto_review_supports_only_anchored_extracts(tmp_path):
    supported = _fact("Jon rehearsed dance yesterday.", "I rehearsed dance yesterday.")
    future = _fact("Jon has dance students.", "We need a good floor for my dance students.")
    image = _fact("Jon shared a photo of dance.", "Look at this photo!",
                  caption="a photo of dance")
    assert assess_auto_fact(supported).status == "supported"
    assert assess_auto_fact(future).status == "unclear"
    assert assess_auto_fact(image).reason_code == "AUTO_IMAGE_NEEDS_REVIEW"
    queue = ReviewQueue(run_fingerprint=_fp("run"), sample_id="synthetic-auto",
        windows=(QueueWindow(window_id="window_" + "a" * 24,
            call_fingerprint=_fp("call"), source_refs_fingerprint=_fp("refs"),
            facts=(supported, future, image)),))
    approval_path, authority_path = write_auto_review_files(queue, tmp_path / "auto")
    verified = verify_review_files(queue, approval_path, authority_path, run_kind="real")
    assert verified.approval.reviewer_id == AUTO_REVIEWER_ID
    assert [f.decision for f in verified.approval.windows[0].facts] == ["approved", "unclear", "unclear"]
    assert verified.checker_for_window(queue.windows[0].window_id)(
        supported.draft, supported.cited_turns).reason_code == "AUTO_EXTRACTIVE_APPROVED"
    with pytest.raises(FileExistsError):
        write_auto_review_files(queue, tmp_path / "auto")


def test_auto_review_recomputes_decisions_even_with_resealed_file(tmp_path):
    unsupported = _fact("Jon has dance students.", "We need a good floor for my dance students.")
    queue = ReviewQueue(run_fingerprint=_fp("run"), sample_id="synthetic-auto",
        windows=(QueueWindow(window_id="window_" + "a" * 24,
            call_fingerprint=_fp("call"), source_refs_fingerprint=_fp("refs"),
            facts=(unsupported,)),))
    approval_path, authority_path = write_auto_review_files(queue, tmp_path / "auto")
    approval = json.loads(approval_path.read_text(encoding="utf-8"))
    decision = approval["windows"][0]["facts"][0]
    decision["decision"] = "approved"
    decision["reason_code"] = "AUTO_EXTRACTIVE_SENTENCE"
    decision["confirmed_source_fingerprints"] = [_fp(unsupported.cited_turns[0].model_dump(mode="json"))]
    payload = (json.dumps(approval, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    approval_path.write_bytes(payload)
    authority = TrustedReviewAuthority.model_validate_json(authority_path.read_bytes())
    authority_path.write_text(authority.model_copy(update={
        "approval_file_sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
    }).model_dump_json(), encoding="utf-8")
    with pytest.raises(ReviewError, match="REVIEW_REJECTED_OR_STALE"):
        verify_review_files(queue, approval_path, authority_path, run_kind="real")
