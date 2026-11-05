"""Conservative, reproducible source-text review without a model or human identity.

This policy intentionally sacrifices recall. It approves only short claims whose
content words occur in one cited utterance, with speaker, modality and time guards.
All other claims remain unclear and are excluded from publication.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from dynamic_memory.memory.normalization import EvidenceAssessment

AUTO_REVIEWER_ID = "auto-extractive-v1"
AUTO_AUTHORITY_VERSION = "auto-extractive-v1-" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
_WORDS = re.compile(r"[a-z]+|\d+", re.I)
_SENTENCES = re.compile(r"(?<=[.!?])\s+|\n+")
_FUTURE = re.compile(
    r"\b(?:will|would|gonna|going to|hope|hoping|plan|planning|intend|"
    r"intending|want|wanna|should|need|needs|can't wait)\b",
    re.I,
)
_HYPOTHETICAL = re.compile(
    r"\b(?:if|imagine|suppose|hypothetical|might|could|would have)\b",
    re.I,
)
_UNCERTAIN = re.compile(
    r"\b(?:maybe|perhaps|probably|possibly|not sure|unsure|might|could)\b",
    re.I,
)
_NEGATION = re.compile(
    r"\b(?:not|never|no|none|nobody|nothing|cannot|can't|didn't|doesn't|"
    r"isn't|wasn't|weren't|won't|wouldn't|no longer)\b",
    re.I,
)
_VISUAL = re.compile(r"\b(?:image|photo|picture|pictured|depicts|attached)\b", re.I)
_STOP = frozenset("a an the to at in on of and or is was are were be been being have has had do did does with for from by into out it this that these those his her their my your our me him them as so very really some all up down about now then he she they i we you".split())


def _stem(word: str) -> str:
    # Keep inflection handling deliberately small; no synonym expansion.
    if len(word) > 5 and word.endswith("ing"):
        word = word[:-3]
        if len(word) > 2 and word[-1] == word[-2]:
            word = word[:-1]
    elif len(word) > 4 and word.endswith("ed"):
        word = word[:-2]
    elif len(word) > 4 and word.endswith("es"):
        word = word[:-2]
    elif len(word) > 3 and word.endswith("s"):
        word = word[:-1]
    return word


def _terms(text: str) -> set[str]:
    return {_stem(w) for w in _WORDS.findall(text.casefold()) if w not in _STOP}


def _normal_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def assess_auto_fact(fact) -> EvidenceAssessment:
    """Decide from the exact cited turn; never infer support from a SourceRef alone."""
    draft = fact.draft
    sources = fact.cited_turns
    if len(sources) != 1:
        return EvidenceAssessment("unclear", "AUTO_MULTISOURCE_NEEDS_REVIEW")
    source = sources[0]
    if source.image_caption:
        caption = _normal_text(source.image_caption)
        content = _normal_text(draft.content)
        if (
            caption
            and content
            and (content == caption or _terms(content) <= _terms(caption))
        ):
            return EvidenceAssessment("supported", "AUTO_EXACT_IMAGE_CAPTION")
        if _VISUAL.search(draft.content):
            return EvidenceAssessment("unclear", "AUTO_IMAGE_NEEDS_REVIEW")
    if not source.text.strip() or not draft.content.strip():
        return EvidenceAssessment("unclear", "AUTO_EMPTY_TEXT")
    if draft.recorded_at is not None:
        source_time = (
            source.recorded_at.isoformat()
            if source.recorded_at is not None
            else None
        )
        if source_time is None or draft.recorded_at != source_time:
            return EvidenceAssessment("unclear", "AUTO_RECORDED_AT_MISMATCH")
    if draft.entity and draft.entity.casefold() not in source.text.casefold() and draft.entity.casefold() != source.speaker.casefold():
        return EvidenceAssessment("unclear", "AUTO_ENTITY_NOT_ANCHORED")
    if draft.event_time and draft.event_time.raw and draft.event_time.raw.casefold() not in source.text.casefold():
        return EvidenceAssessment("unclear", "AUTO_TIME_NOT_ANCHORED")
    claim_terms = _terms(draft.content)
    if len(claim_terms) < 2:
        return EvidenceAssessment("unclear", "AUTO_CLAIM_TOO_SHORT")
    # A claim must fit within one sentence. The speaker name is available as a
    # source attribute, so first-person utterances can support named claims.
    sentences = _SENTENCES.split(source.text.strip())
    for sentence in sentences:
        source_terms = _terms(sentence) | _terms(source.speaker)
        if not claim_terms.issubset(source_terms):
            continue
        if bool(_NEGATION.search(draft.content)) != bool(_NEGATION.search(sentence)):
            continue
        if draft.assertion_type == "fact" and _FUTURE.search(sentence):
            continue
        if draft.assertion_type == "plan" and not _FUTURE.search(sentence):
            continue
        if draft.assertion_type == "hypothetical" and not (
            _FUTURE.search(sentence) or _HYPOTHETICAL.search(sentence)
        ):
            continue
        if draft.assertion_type == "uncertain" and not (
            _FUTURE.search(sentence) or _UNCERTAIN.search(sentence)
        ):
            continue
        if draft.event_time and draft.event_time.raw and draft.event_time.raw.casefold() not in sentence.casefold():
            continue
        return EvidenceAssessment("supported", "AUTO_EXTRACTIVE_SENTENCE")
    return EvidenceAssessment("unclear", "AUTO_CLAIM_NOT_ENTAILED_EXTRACTIVELY")


def _encoded(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def write_auto_review_files(queue, output_dir: Path, *,
                            run_kind: Literal["real", "offline_test"] = "real") -> tuple[Path, Path]:
    """Write machine-identified decisions outside runner state; never overwrite."""
    from dynamic_memory.memory.review_workflow import (
        FactDecision, ReviewApproval, TrustedReviewAuthority, WindowDecision, _fp,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    if run_kind not in {"real", "offline_test"}:
        raise ValueError("invalid automatic review run kind")
    approval_path = output_dir / "auto_review_approval.json"
    authority_path = output_dir / "auto_review_authority.json"
    if approval_path.exists() or authority_path.exists():
        raise FileExistsError("automatic review output already exists")
    windows = []
    for window in queue.windows:
        decisions = []
        for fact in window.facts:
            assessed = assess_auto_fact(fact)
            approved = assessed.status == "supported"
            decisions.append(FactDecision(
                draft_fingerprint=fact.draft_fingerprint,
                evidence_fingerprint=fact.evidence_fingerprint,
                decision="approved" if approved else "unclear",
                reason_code=assessed.reason_code,
                confirmed_source_fingerprints=tuple(_fp(t.model_dump(mode="json")) for t in fact.cited_turns) if approved else (),
                relation_confirmed=False,
            ))
        windows.append(WindowDecision(window_id=window.window_id,
            source_refs_fingerprint=window.source_refs_fingerprint,
            decision="reviewed", facts=tuple(decisions)))
    now = datetime.now(timezone.utc)
    approval = ReviewApproval(run_fingerprint=queue.run_fingerprint,
        queue_fingerprint=queue.fingerprint, reviewer_id=AUTO_REVIEWER_ID,
        reviewed_at=now, authority_version=AUTO_AUTHORITY_VERSION,
        windows=tuple(windows))
    approval_bytes = _encoded(approval.model_dump(mode="json"))
    authority = TrustedReviewAuthority(reviewer_id=AUTO_REVIEWER_ID,
        run_fingerprint=queue.run_fingerprint, queue_fingerprint=queue.fingerprint,
        approval_file_sha256="sha256:" + hashlib.sha256(approval_bytes).hexdigest(),
        authority_version=AUTO_AUTHORITY_VERSION, allowed_run_kind=run_kind,
        expires_at=now + timedelta(days=30))
    with approval_path.open("xb") as stream:
        stream.write(approval_bytes)
    with authority_path.open("xb") as stream:
        stream.write(_encoded(authority.model_dump(mode="json")))
    return approval_path, authority_path
