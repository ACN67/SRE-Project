"""Private human review handoff. The runner never writes approval or authority files."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from dynamic_memory.contracts import SourceTurn
from dynamic_memory.data.repository import SourceRepository
from dynamic_memory.memory.extraction import AtomicFactDraft, WindowExtractionResult
from dynamic_memory.memory.normalization import EvidenceAssessment
from dynamic_memory.memory.strict_evidence import strict_evidence_checker
from dynamic_memory.memory.windows import ExtractionWindow

_FP = r"^sha256:[0-9a-f]{64}$"
_VERIFIED_SEAL = object()


class ReviewError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(f"{code}: independent review unavailable")


def _bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _fp(value: object) -> str:
    return "sha256:" + hashlib.sha256(_bytes(value)).hexdigest()


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)


class QueueFact(_Frozen):
    draft: AtomicFactDraft
    cited_turns: tuple[SourceTurn, ...]
    draft_fingerprint: str = Field(pattern=_FP)
    evidence_fingerprint: str = Field(pattern=_FP)
    automatic_status: Literal["supported", "unsupported", "unclear"]
    automatic_reason: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")


class QueueWindow(_Frozen):
    window_id: str = Field(pattern=r"^window_[0-9a-f]{24}$")
    call_fingerprint: str = Field(pattern=_FP)
    source_refs_fingerprint: str = Field(pattern=_FP)
    facts: tuple[QueueFact, ...]


class ReviewQueue(_Frozen):
    format_version: Literal[1] = 1
    run_fingerprint: str = Field(pattern=_FP)
    sample_id: str = Field(min_length=1)
    windows: tuple[QueueWindow, ...] = Field(min_length=1)

    @property
    def fingerprint(self) -> str:
        return _fp(self.model_dump(mode="json"))


class FactDecision(_Frozen):
    draft_fingerprint: str = Field(pattern=_FP)
    evidence_fingerprint: str = Field(pattern=_FP)
    decision: Literal["approved", "rejected", "unclear"]
    reason_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    confirmed_source_fingerprints: tuple[str, ...] = ()
    relation_confirmed: bool = False


class WindowDecision(_Frozen):
    window_id: str = Field(pattern=r"^window_[0-9a-f]{24}$")
    source_refs_fingerprint: str = Field(pattern=_FP)
    decision: Literal["reviewed"]
    facts: tuple[FactDecision, ...]


class ReviewApproval(_Frozen):
    format_version: Literal[1] = 1
    run_fingerprint: str = Field(pattern=_FP)
    queue_fingerprint: str = Field(pattern=_FP)
    reviewer_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
    reviewed_at: datetime
    authority_version: str = Field(min_length=1)
    windows: tuple[WindowDecision, ...] = Field(min_length=1)


class TrustedReviewAuthority(_Frozen):
    """Manually provisioned allowlist entry, stored separately from run state."""

    format_version: Literal[1] = 1
    reviewer_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
    run_fingerprint: str = Field(pattern=_FP)
    queue_fingerprint: str = Field(pattern=_FP)
    approval_file_sha256: str = Field(pattern=_FP)
    authority_version: str = Field(min_length=1)
    allowed_run_kind: Literal["real", "offline_test"]
    expires_at: datetime
    semantic_receipts_sha256: str | None = Field(default=None, pattern=_FP)

    def model_dump(self, *args, **kwargs):
        # Legacy authority fingerprints did not include this optional field.
        kwargs.setdefault("exclude_none", True)
        return super().model_dump(*args, **kwargs)


class VerifiedReview:
    """Produced by validation of two external files; no cryptographic identity claim."""

    def __init__(self, queue: ReviewQueue, approval: ReviewApproval,
                 authority: TrustedReviewAuthority, approval_fingerprint: str, *, seal: object):
        if seal is not _VERIFIED_SEAL:
            raise ReviewError("REVIEW_NOT_VERIFIED")
        self.queue = queue
        self.approval = approval
        self.authority = authority
        self.approval_fingerprint = approval_fingerprint
        self._approved = {
            (window.window_id, fact.draft_fingerprint, fact.evidence_fingerprint)
            for window in approval.windows for fact in window.facts if fact.decision == "approved"
        }

    def filter_for_window(self, window_id: str):
        def select(result: WindowExtractionResult) -> WindowExtractionResult:
            queued = next((w for w in self.queue.windows if w.window_id == window_id), None)
            reviewed = next((w for w in self.approval.windows if w.window_id == window_id), None)
            if queued is None or reviewed is None or len(result.facts) != len(queued.facts):
                raise ReviewError("REVIEW_QUEUE_STALE")
            accepted = []
            for draft, fact, decision in zip(result.facts, queued.facts, reviewed.facts):
                if _fp(draft.model_dump(mode="json")) != fact.draft_fingerprint:
                    raise ReviewError("REVIEW_QUEUE_STALE")
                if decision.decision == "approved":
                    accepted.append(draft)
            return WindowExtractionResult(result.sample_id, result.extraction_window_id,
                tuple(accepted), result.template_fingerprint,
                result.input_tokens_estimated, result.cache_status)
        return select

    def checker_for_window(self, window_id: str):
        from dynamic_memory.memory.auto_review import AUTO_REVIEWER_ID
        from dynamic_memory.memory.semantic_review import SEMANTIC_REVIEWER_ID, SEMANTIC_REVIEWER_ID_V2
        automatic = self.approval.reviewer_id == AUTO_REVIEWER_ID
        semantic = self.approval.reviewer_id in {SEMANTIC_REVIEWER_ID, SEMANTIC_REVIEWER_ID_V2}
        def check(draft: AtomicFactDraft, sources: tuple[SourceTurn, ...]) -> EvidenceAssessment:
            key = (window_id, _fp(draft.model_dump(mode="json")),
                   _fp([source.model_dump(mode="json") for source in sources]))
            if key in self._approved:
                return EvidenceAssessment("supported", "AUTO_EXTRACTIVE_APPROVED" if automatic else
                                          "LLM_SEMANTIC_APPROVED" if semantic else "HUMAN_APPROVED")
            return EvidenceAssessment("unclear", "REVIEW_NOT_APPROVED")
        check._trusted_verified = True
        check._human_verified = not (automatic or semantic)
        return check


def make_review_queue(*, run_fingerprint: str, sample_id: str,
                      windows: tuple[ExtractionWindow, ...],
                      call_fingerprints: dict[str, str],
                      results: dict[str, WindowExtractionResult],
                      repository: SourceRepository) -> ReviewQueue:
    """Export model drafts and their exact cited turns only to a private file."""
    try:
        if set(results) != {window.extraction_window_id for window in windows}:
            raise ValueError("missing result")
        queued = []
        for window in windows:
            result = results[window.extraction_window_id]
            if result.sample_id != sample_id or result.extraction_window_id != window.extraction_window_id:
                raise ValueError("wrong result")
            allowed = {(ref.session_id, ref.dia_id) for ref in window.source_refs}
            facts = []
            for draft in result.facts:
                refs = {(ref.session_id, ref.dia_id) for ref in draft.source_refs}
                if not refs.issubset(allowed):
                    raise ValueError("wrong refs")
                sources = tuple(repository.get_turn(sample_id, ref) for ref in sorted(
                    draft.source_refs, key=lambda item: (item.session_id, item.dia_id)))
                assessment = strict_evidence_checker(draft, sources)
                facts.append(QueueFact(draft=draft, cited_turns=sources,
                    draft_fingerprint=_fp(draft.model_dump(mode="json")),
                    evidence_fingerprint=_fp([source.model_dump(mode="json") for source in sources]),
                    automatic_status=assessment.status, automatic_reason=assessment.reason_code))
            queued.append(QueueWindow(window_id=window.extraction_window_id,
                call_fingerprint=call_fingerprints[window.extraction_window_id],
                source_refs_fingerprint=_fp([ref.model_dump(mode="json") for ref in window.source_refs]),
                facts=tuple(facts)))
        return ReviewQueue(run_fingerprint=run_fingerprint, sample_id=sample_id,
                           windows=tuple(queued))
    except (KeyError, ValueError, TypeError, ValidationError):
        raise ReviewError("REVIEW_QUEUE_INVALID") from None


def export_review_queue(queue: ReviewQueue, path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(_bytes(queue.model_dump(mode="json")))
            stream.flush()
    except (OSError, ValueError):
        raise ReviewError("REVIEW_QUEUE_WRITE_FAILED") from None


def verify_review_files(queue: ReviewQueue, approval_path: Path,
                        authority_path: Path, *, run_kind: Literal["real", "offline_test"],
                        now: datetime | None = None) -> VerifiedReview:
    """Read separately supplied human files. Never construct approvals here."""
    try:
        approval_bytes = approval_path.read_bytes()
        approval = ReviewApproval.model_validate_json(approval_bytes)
        authority = TrustedReviewAuthority.model_validate_json(authority_path.read_bytes())
        instant = now or datetime.now(timezone.utc)
        if (authority.allowed_run_kind != run_kind or authority.expires_at.tzinfo is None
                or instant >= authority.expires_at or approval.reviewed_at.tzinfo is None
                or approval.reviewed_at > instant or approval.reviewed_at >= authority.expires_at):
            raise ValueError("expired")
        if (approval.run_fingerprint != queue.run_fingerprint or
            approval.queue_fingerprint != queue.fingerprint or
            authority.run_fingerprint != queue.run_fingerprint or
            authority.queue_fingerprint != queue.fingerprint or
            authority.approval_file_sha256 != "sha256:" + hashlib.sha256(approval_bytes).hexdigest() or
            authority.reviewer_id != approval.reviewer_id or
            authority.authority_version != approval.authority_version or
            len(approval.windows) != len(queue.windows)):
            raise ValueError("identity")
        from dynamic_memory.memory.auto_review import (
            AUTO_AUTHORITY_VERSION, AUTO_REVIEWER_ID, assess_auto_fact,
        )
        from dynamic_memory.memory.semantic_review import (
            SEMANTIC_AUTHORITY_VERSION, SEMANTIC_AUTHORITY_VERSION_V2,
            SEMANTIC_REVIEWER_ID, SEMANTIC_REVIEWER_ID_V2,
            verify_semantic_receipts,
        )
        is_auto = approval.reviewer_id == AUTO_REVIEWER_ID
        is_semantic = approval.reviewer_id in {SEMANTIC_REVIEWER_ID, SEMANTIC_REVIEWER_ID_V2}
        if is_auto != (approval.authority_version == AUTO_AUTHORITY_VERSION):
            raise ValueError("review method")
        if (approval.authority_version in {SEMANTIC_AUTHORITY_VERSION, SEMANTIC_AUTHORITY_VERSION_V2}) != is_semantic:
            raise ValueError("semantic review method")
        if is_semantic and approval.authority_version != (
                SEMANTIC_AUTHORITY_VERSION if approval.reviewer_id == SEMANTIC_REVIEWER_ID
                else SEMANTIC_AUTHORITY_VERSION_V2):
            raise ValueError("semantic review version")
        if is_semantic:
            if authority.semantic_receipts_sha256 is None:
                raise ValueError("missing semantic receipts")
            receipt_path = authority_path.parent / "semantic_receipts.json"
            receipt_bytes = receipt_path.read_bytes()
            if "sha256:" + hashlib.sha256(receipt_bytes).hexdigest() != authority.semantic_receipts_sha256:
                raise ValueError("semantic receipt hash")
            verify_semantic_receipts(queue, approval, receipt_bytes,
                guarded=approval.reviewer_id == SEMANTIC_REVIEWER_ID)
        elif authority.semantic_receipts_sha256 is not None:
            raise ValueError("unexpected semantic receipts")
        for expected, reviewed in zip(queue.windows, approval.windows):
            if (reviewed.window_id != expected.window_id or
                reviewed.source_refs_fingerprint != expected.source_refs_fingerprint or
                reviewed.decision != "reviewed" or
                len(reviewed.facts) != len(expected.facts)):
                raise ValueError("window")
            for fact, decision in zip(expected.facts, reviewed.facts):
                if is_auto:
                    assessed = assess_auto_fact(fact)
                    if (decision.decision != ("approved" if assessed.status == "supported" else "unclear")
                            or decision.reason_code != assessed.reason_code):
                        raise ValueError("automatic decision mismatch")
                if (decision.draft_fingerprint != fact.draft_fingerprint or
                    decision.evidence_fingerprint != fact.evidence_fingerprint or
                    (decision.decision == "approved" and (
                        decision.confirmed_source_fingerprints != tuple(
                            _fp(source.model_dump(mode="json")) for source in fact.cited_turns) or
                        (len(fact.cited_turns) > 1 and not decision.relation_confirmed))) or
                    (decision.decision != "approved" and
                     (decision.confirmed_source_fingerprints or decision.relation_confirmed))):
                    raise ValueError("fact")
        return VerifiedReview(queue, approval, authority,
                              "sha256:" + hashlib.sha256(approval_bytes).hexdigest(),
                              seal=_VERIFIED_SEAL)
    except (OSError, ValueError, TypeError, ValidationError, AttributeError):
        raise ReviewError("REVIEW_REJECTED_OR_STALE") from None
