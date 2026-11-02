"""B-06: fail-closed evidence review, conservative deduplication and public IDs."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import ValidationError

from dynamic_memory.contracts import MemoryRecord, SourceRef, SourceTurn, TimeSpan, render_visible_turn
from dynamic_memory.contracts.memory_id import (
    canonical_memory_payload, compute_memory_id, validate_memory_id,
)
from dynamic_memory.data.repository import SourceRepository, SourceRepositoryError
from dynamic_memory.memory.extraction import AtomicFactDraft, WindowExtractionResult


@dataclass(frozen=True, slots=True)
class EvidenceAssessment:
    """A caller's explicit per-source review, never inferred from source existence."""

    status: Literal["supported", "unsupported", "unclear"]
    reason_code: str

    def __post_init__(self) -> None:
        if self.status not in ("supported", "unsupported", "unclear"):
            raise ValueError("invalid evidence status")
        if not isinstance(self.reason_code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", self.reason_code):
            raise ValueError("evidence reason must be a sanitized code")


EvidenceChecker = Callable[[AtomicFactDraft, tuple[SourceTurn, ...]], EvidenceAssessment]


class MemoryNormalizationError(ValueError):
    """No raw conversation, draft content or model output in diagnostics."""

    def __init__(
        self, code: str, extraction_window_id: str | None = None,
        reason_code: str | None = None,
    ) -> None:
        self.code = code
        self.extraction_window_id = extraction_window_id
        self.reason_code = reason_code
        super().__init__(f"{code}: Memory normalization failed")


def _fail(code: str, window_id: str | None = None, reason_code: str | None = None) -> None:
    raise MemoryNormalizationError(code, window_id, reason_code) from None


def _text(value: str | None) -> str | None:
    if value is None:
        return None
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value).strip())


def _time(value: TimeSpan | None) -> TimeSpan | None:
    if value is None:
        return None
    raw = value.model_dump(mode="python", warnings="error")
    raw["raw"] = _text(raw["raw"])
    return TimeSpan.model_validate(raw)


def _conflict_code(draft: AtomicFactDraft, source: SourceTurn) -> tuple[str, str] | None:
    """Only obvious lexical contradictions; this is not semantic entailment."""
    source_text = _text(render_visible_turn(source)).casefold()
    content = _text(draft.content).casefold()
    if draft.entity is not None and _text(draft.entity).casefold() not in source_text:
        return ("unclear", "ENTITY_NOT_VISIBLE")
    words = lambda value: set(re.findall(r"\w+", value))
    entity_words = words(_text(draft.entity).casefold()) if draft.entity is not None else set()
    stop_words = {"a", "an", "the", "to", "at", "in", "on", "of", "and", "or", "is", "was", "she", "he", "they"}
    content_words = words(content)
    source_words = words(source_text)
    if not (content_words - entity_words - stop_words) & (source_words - entity_words - stop_words):
        return ("unclear", "NO_SHARED_CLAIM_TERMS")
    if draft.value is not None:
        value = _text(draft.value).casefold()
        if re.fullmatch(r"[\w-]+", value) and value in content_words and value not in source_words:
            return ("unclear", "VALUE_NOT_VISIBLE")
    negation = re.compile(r"\b(?:not|never|no longer|didn't|doesn't|isn't|wasn't|can't|cannot)\b")
    if draft.polarity == "positive" and negation.search(source_text) and not negation.search(content):
        return ("unclear", "NEGATION_NEEDS_REVIEW")
    plan = re.compile(r"\b(?:plans? to|intends? to|hopes? to|going to|will)\b")
    if draft.assertion_type == "fact" and draft.fact_kind == "event" and plan.search(source_text) and not plan.search(content):
        return ("unclear", "PLAN_NEEDS_REVIEW")
    if draft.event_time is not None and draft.event_time.start is not None:
        dates = set(re.findall(r"\b\d{4}-\d{2}-\d{2}\b", source_text))
        if len(dates) == 1 and draft.event_time.start.date().isoformat() not in dates:
            return ("unsupported", "EVENT_TIME_CONFLICT")
    return None


def _check_evidence(
    draft: AtomicFactDraft, sources: tuple[SourceTurn, ...],
    checker: EvidenceChecker | None, window_id: str,
) -> None:
    for source in sources:
        conflict = _conflict_code(draft, source)
        if conflict is not None:
            _fail("EVIDENCE_UNSUPPORTED" if conflict[0] == "unsupported" else "EVIDENCE_UNCLEAR",
                  window_id, conflict[1])
        if checker is None:
            _fail("EVIDENCE_UNCLEAR", window_id, "NO_CHECKER")
        try:
            assessment = checker(draft, (source,))
        except Exception:
            _fail("EVIDENCE_CHECK_FAILED", window_id, "CHECKER_EXCEPTION")
        if not isinstance(assessment, EvidenceAssessment):
            _fail("EVIDENCE_CHECK_FAILED", window_id, "INVALID_ASSESSMENT")
        if assessment.status != "supported":
            _fail("EVIDENCE_UNSUPPORTED" if assessment.status == "unsupported" else "EVIDENCE_UNCLEAR",
                  window_id, assessment.reason_code)


def _dedup_key(memory: MemoryRecord) -> str:
    payload = canonical_memory_payload(memory)
    payload.pop("source_refs")
    payload["session_id"] = memory.source_refs[0].session_id
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_memories(
    sample_id: str,
    results: Sequence[WindowExtractionResult],
    repository: SourceRepository,
    *,
    extractor_version: str,
    evidence_checker: EvidenceChecker | None = None,
) -> list[MemoryRecord]:
    """Return only reviewed records; one bad draft fails the batch for B-07 to report."""
    if not isinstance(sample_id, str) or not sample_id.strip():
        _fail("INVALID_SAMPLE")
    if not isinstance(repository, SourceRepository):
        _fail("INVALID_REPOSITORY")
    if not isinstance(results, (list, tuple)) or not all(isinstance(item, WindowExtractionResult) for item in results):
        _fail("INVALID_DRAFT_BATCH")
    if not isinstance(extractor_version, str) or not extractor_version.strip():
        _fail("INVALID_EXTRACTOR_VERSION")
    extractor_version = _text(extractor_version)
    try:
        if sample_id not in repository.list_samples():
            _fail("SAMPLE_NOT_FOUND")
    except SourceRepositoryError as exc:
        _fail(exc.code)

    groups: dict[str, tuple[MemoryRecord, set[tuple[str, str]]]] = {}
    for result in results:
        if result.sample_id != sample_id:
            _fail("SAMPLE_MISMATCH", result.extraction_window_id)
        for supplied in result.facts:
            try:
                if not isinstance(supplied, AtomicFactDraft):
                    _fail("DRAFT_INVALID", result.extraction_window_id)
                draft = AtomicFactDraft.model_validate(supplied.model_dump(mode="python", warnings="error"))
                if len({ref.session_id for ref in draft.source_refs}) != 1:
                    _fail("SOURCE_SESSION_MISMATCH", result.extraction_window_id)
                refs = sorted({(ref.session_id, ref.dia_id) for ref in draft.source_refs})
                sources = tuple(repository.get_turn(sample_id, SourceRef(session_id=s, dia_id=d))
                                for s, d in refs)
                recorded_at = datetime.fromisoformat(draft.recorded_at) if draft.recorded_at is not None else None
                if recorded_at is not None and recorded_at not in {source.recorded_at for source in sources}:
                    _fail("RECORDED_AT_MISMATCH", result.extraction_window_id)
                _check_evidence(draft, sources, evidence_checker, result.extraction_window_id)
                memory = MemoryRecord(
                    memory_id="mem_" + "0" * 20, sample_id=sample_id,
                    content=_text(draft.content), entity=_text(draft.entity),
                    attribute=_text(draft.attribute), value=_text(draft.value),
                    fact_kind=draft.fact_kind, assertion_type=draft.assertion_type,
                    polarity=draft.polarity, recorded_at=recorded_at,
                    event_time=_time(draft.event_time),
                    source_refs=[SourceRef(session_id=s, dia_id=d) for s, d in refs],
                    extractor_version=extractor_version,
                )
            except MemoryNormalizationError:
                raise
            except SourceRepositoryError as exc:
                _fail(exc.code, result.extraction_window_id)
            except (ValidationError, ValueError, TypeError, AttributeError, UnicodeError, RecursionError):
                _fail("DRAFT_INVALID", result.extraction_window_id)
            key = _dedup_key(memory)
            if key in groups:
                prior, prior_refs = groups[key]
                prior_refs.update(refs)
            else:
                groups[key] = (memory, set(refs))

    return _finish_groups(groups)


def _finish_groups(groups: dict[str, tuple[MemoryRecord, set[tuple[str, str]]]]) -> list[MemoryRecord]:
    """Finalize reviewed groups after all real source and evidence checks."""
    output: list[MemoryRecord] = []
    seen_ids: dict[str, dict] = {}
    for key in sorted(groups):
        memory, refs = groups[key]
        merged = memory.model_copy(update={
            "source_refs": [SourceRef(session_id=s, dia_id=d) for s, d in sorted(refs)],
        })
        merged = merged.model_copy(update={"memory_id": compute_memory_id(merged)})
        try:
            merged = MemoryRecord.model_validate(merged.model_dump(mode="python", warnings="error"))
            validate_memory_id(merged)
        except (ValidationError, ValueError, TypeError):
            _fail("MEMORY_INVALID")
        payload = canonical_memory_payload(merged)
        prior_payload = seen_ids.get(merged.memory_id)
        if prior_payload is not None and prior_payload != payload:
            _fail("MEMORY_ID_COLLISION")
        seen_ids[merged.memory_id] = payload
        output.append(merged)
    return output


def merge_reviewed_memories(memories: Sequence[MemoryRecord]) -> list[MemoryRecord]:
    """Merge individually reviewed windows without repeating semantic review."""
    groups: dict[str, tuple[MemoryRecord, set[tuple[str, str]]]] = {}
    for memory in memories:
        try:
            if not isinstance(memory, MemoryRecord):
                _fail("MEMORY_INVALID")
            checked = MemoryRecord.model_validate(memory.model_dump(mode="python", warnings="error"))
            validate_memory_id(checked)
            refs = {(ref.session_id, ref.dia_id) for ref in checked.source_refs}
            key = _dedup_key(checked)
            if key in groups:
                groups[key][1].update(refs)
            else:
                groups[key] = (checked, refs)
        except MemoryNormalizationError:
            raise
        except (ValidationError, ValueError, TypeError, AttributeError):
            _fail("MEMORY_INVALID")
    return _finish_groups(groups)
