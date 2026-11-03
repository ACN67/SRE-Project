"""Conservative independent review: only explicit verbatim source sentences pass.

This is intentionally narrower than semantic entailment. Anything that needs
interpretation remains unclear for a separate human review; existence of a
SourceRef or LLM confidence is never sufficient.
"""

from __future__ import annotations

import re
import unicodedata

from dynamic_memory.contracts import SourceTurn
from dynamic_memory.memory.extraction import AtomicFactDraft
from dynamic_memory.memory.normalization import EvidenceAssessment

_NEGATION = re.compile(r"\b(?:not|never|no|didn't|doesn't|isn't|wasn't|cannot|can't)\b", re.I)
_PLAN = re.compile(r"\b(?:plans? to|intends? to|hopes? to|going to|will)\b", re.I)


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value).strip()).casefold()


def _contains_word(text: str, value: str) -> bool:
    return re.search(r"(?<!\w)" + re.escape(_norm(value)) + r"(?!\w)", text) is not None


def strict_evidence_checker(draft: AtomicFactDraft, sources: tuple[SourceTurn, ...]) -> EvidenceAssessment:
    """Return a sanitized decision for one cited source, without logging its text."""
    if len(sources) != 1 or not isinstance(draft, AtomicFactDraft) or not isinstance(sources[0], SourceTurn):
        return EvidenceAssessment("unclear", "SOURCE_SCOPE_INVALID")
    source = sources[0]
    content = _norm(draft.content)
    speech = _norm(source.text)
    caption = _norm(source.image_caption or "")
    if not speech or (caption and content in caption and content not in speech):
        return EvidenceAssessment("unclear", "CAPTION_NOT_SPEECH")
    if draft.entity and not _contains_word(speech, draft.entity):
        return EvidenceAssessment("unclear", "ENTITY_NOT_VISIBLE")
    if draft.value and not _contains_word(speech, draft.value):
        return EvidenceAssessment("unclear", "VALUE_NOT_VISIBLE")
    if draft.event_time is not None and draft.event_time.raw is not None:
        if _norm(draft.event_time.raw) not in speech:
            return EvidenceAssessment("unclear", "TIME_NOT_VISIBLE")
    if bool(_NEGATION.search(speech)) != bool(_NEGATION.search(content)):
        return EvidenceAssessment("unclear", "NEGATION_NEEDS_REVIEW")
    if _PLAN.search(speech) and draft.assertion_type == "fact":
        return EvidenceAssessment("unclear", "PLAN_NEEDS_REVIEW")
    sentences = [_norm(item) for item in re.split(r"(?<=[.!?])\s+", source.text)]
    if content not in sentences:
        return EvidenceAssessment("unclear", "PARAPHRASE_NEEDS_REVIEW")
    if draft.entity and not _contains_word(content, draft.entity):
        return EvidenceAssessment("unclear", "ENTITY_NOT_VISIBLE")
    if draft.attribute and not _contains_word(content, draft.attribute):
        return EvidenceAssessment("unclear", "ATTRIBUTE_NOT_VISIBLE")
    if draft.value and not _contains_word(content, draft.value):
        return EvidenceAssessment("unclear", "VALUE_NOT_VISIBLE")
    if draft.event_time is not None and draft.event_time.raw is not None:
        if _norm(draft.event_time.raw) not in content:
            return EvidenceAssessment("unclear", "TIME_NOT_VISIBLE")
    if (draft.polarity == "negative") != bool(_NEGATION.search(content)):
        return EvidenceAssessment("unclear", "POLARITY_NEEDS_REVIEW")
    if draft.assertion_type == "plan" and not _PLAN.search(content):
        return EvidenceAssessment("unclear", "PLAN_NEEDS_REVIEW")
    if len(re.findall(r"\w+", content)) < 3:
        return EvidenceAssessment("unclear", "CLAIM_TOO_SHORT")
    return EvidenceAssessment("supported", "VERBATIM_SOURCE_SENTENCE")
