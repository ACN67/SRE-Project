"""Auditable model-assisted review of a frozen private fact queue.

The model recommends evidence decisions. Source quotes, identities, response
completion and exact response bytes are checked before publication. A model
decision is not a proof of entailment; independent quality evaluation remains
necessary.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

SEMANTIC_REVIEWER_ID_V2 = "deepseek-v4.1-flash-semantic-v2"
SEMANTIC_AUTHORITY_VERSION_V2 = "deepseek-v4.1-flash-high-semantic-v2"
SEMANTIC_REVIEWER_ID = "deepseek-v4.1-flash-semantic-v3"
SEMANTIC_AUTHORITY_VERSION = "deepseek-v4.1-flash-high-semantic-v3"
SEMANTIC_API_MODEL = "deepseek-flash"  # Official V4.1 Flash API name.
_SYSTEM = (
    "You are an independent evidence reviewer for a memory system. Return JSON only. "
    "For each proposed fact, decide supported, contradicted, or unclear using ONLY its cited turns. "
    "Do not use outside knowledge or infer future plans as completed events. Resolve pronouns only "
    "when the cited turn speaker makes the reference unambiguous. Check negation, time, speaker, "
    "entity, modality and relation direction. A present-tense possession claim is unclear when "
    "the only evidence occurs inside a plan, wish, or hypothetical future situation; words like "
    "'my students' in a plan for a future school do not establish current students. A favorite or "
    "top preference must be explicitly attributed to that person; indirect agreement such as "
    "'me too' after another person's preference is not enough. If any required link is missing, "
    "choose unclear. "
    "For supported facts give an exact nonempty quote from EVERY cited turn, with its source_index. "
    "For multi-source facts set relation_confirmed=true only when the cited turns jointly establish "
    "the stated relationship. Return {\"items\":[{\"index\":0,\"decision\":\"supported\","
    "\"evidence\":[{\"source_index\":0,\"quote\":\"exact source substring\"}],"
    "\"relation_confirmed\":false,\"reason\":\"brief evidence rationale\"}]} "
    "with one item for every fact in input order. Do not quote from the fact itself."
)


def _encoded(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _prompt(window) -> str:
    return json.dumps({"window_id": window.window_id, "facts": [
        {"index": i, "draft": fact.draft.model_dump(mode="json"),
         "cited_turns": [{"source_index": j, "speaker": source.speaker,
                          "recorded_at": str(source.recorded_at), "text": source.text,
                          "image_caption": source.image_caption}
                         for j, source in enumerate(fact.cited_turns)]}
        for i, fact in enumerate(window.facts)]}, ensure_ascii=False, separators=(",", ":"))


def _semantic_guard(fact, quotes: list[str], queue) -> str | None:
    """Abstain on common scope, tense and attribution traps.

    These guards are general fail-closed rules, not a dataset-specific allowlist.
    Cross-turn conflicts need a stronger state resolver; the exit-event rule
    only catches an especially common contradictory account.
    """
    claim = fact.draft.content.casefold()
    evidence = " ".join(quotes).casefold()
    if "/" in claim:
        return "LLM_GUARD_COMPOSITE_REFERENT"
    if any(source.image_caption for source in fact.cited_turns) and re.search(
            r"\b(?:photo|picture|image|pictured)\b", claim):
        return "LLM_GUARD_VISUAL_CLAIM"
    if (fact.draft.assertion_type == "fact" and
            re.search(r"\b(?:has|owns|operates|runs)\b", claim) and
            re.search(r"\b(?:will|gonna|going to|would|could|might|hope|hoping)\b", evidence)):
        return "LLM_GUARD_FUTURE_AS_CURRENT"
    if (fact.draft.assertion_type == "plan" and
            re.search(r"\b(?:plans? to|will|going to)\b", claim) and
            re.search(r"\b(?:thinking of|considering|might|maybe|could)\b", evidence)):
        return "LLM_GUARD_CONSIDERATION_AS_PLAN"
    if (queue is not None and re.search(r"\bleft\b.*\bjob\b", claim) and
            any(other.draft.entity.casefold() == fact.draft.entity.casefold() and
                re.search(r"\blost\b.*\bjob\b", other.draft.content.casefold())
                for window in queue.windows for other in window.facts)):
        return "LLM_GUARD_EXIT_HISTORY_CONFLICT"
    return None


def _window_decision(window, content: str, *, queue=None):
    from dynamic_memory.memory.review_workflow import FactDecision, WindowDecision, _fp

    payload = json.loads(content)
    items = payload["items"]
    if not isinstance(items, list) or len(items) != len(window.facts):
        raise ValueError("semantic item count")
    decisions = []
    for i, (fact, item) in enumerate(zip(window.facts, items)):
        if not isinstance(item, dict) or type(item.get("index")) is not int or item["index"] != i:
            raise ValueError("semantic item order")
        verdict = item.get("decision")
        if verdict not in {"supported", "contradicted", "unclear"}:
            raise ValueError("semantic verdict")
        evidence = item.get("evidence", [])
        if not isinstance(evidence, list) or type(item.get("relation_confirmed")) is not bool:
            raise ValueError("semantic evidence shape")
        if not isinstance(item.get("reason"), str) or not item["reason"].strip():
            raise ValueError("semantic reason")
        seen = set()
        quotes = []
        valid_quotes = True
        for entry in evidence:
            if not isinstance(entry, dict) or type(entry.get("source_index")) is not int:
                valid_quotes = False
                break
            j, quote = entry["source_index"], entry.get("quote")
            if (j < 0 or j >= len(fact.cited_turns) or not isinstance(quote, str)
                    or len(quote.strip()) < 10 or quote not in fact.cited_turns[j].text):
                valid_quotes = False
                break
            seen.add(j)
            quotes.append(quote)
        supported = (verdict == "supported" and valid_quotes and
                     seen == set(range(len(fact.cited_turns))) and
                     (len(fact.cited_turns) == 1 or item["relation_confirmed"]))
        guard_reason = _semantic_guard(fact, quotes, queue) if supported and queue is not None else None
        supported = supported and guard_reason is None
        decision = "approved" if supported else ("rejected" if verdict == "contradicted" else "unclear")
        if supported:
            reason = "LLM_SUPPORTED"
        elif decision == "rejected":
            reason = "LLM_CONTRADICTED"
        elif guard_reason is not None:
            reason = guard_reason
        elif verdict == "supported":
            reason = "LLM_EVIDENCE_INVALID"
        else:
            reason = "LLM_UNCLEAR"
        decisions.append(FactDecision(
            draft_fingerprint=fact.draft_fingerprint,
            evidence_fingerprint=fact.evidence_fingerprint,
            decision=decision, reason_code=reason,
            confirmed_source_fingerprints=tuple(_fp(s.model_dump(mode="json"))
                for s in fact.cited_turns) if supported else (),
            relation_confirmed=supported and len(fact.cited_turns) > 1,
        ))
    return WindowDecision(window_id=window.window_id,
        source_refs_fingerprint=window.source_refs_fingerprint,
        decision="reviewed", facts=tuple(decisions))


def verify_semantic_receipts(queue, approval, receipt_bytes: bytes, *, guarded: bool) -> None:
    body = json.loads(receipt_bytes)
    if (body.get("format_version") != 1 or body.get("queue_fingerprint") != queue.fingerprint
            or body.get("api_model") != SEMANTIC_API_MODEL
            or body.get("reasoning_effort") != "high"
            or body.get("max_tokens") is not None):
        raise ValueError("semantic receipt identity")
    rows = body.get("windows")
    if not isinstance(rows, list) or len(rows) != len(queue.windows):
        raise ValueError("semantic receipt windows")
    for window, row, reviewed in zip(queue.windows, rows, approval.windows):
        if (row.get("window_id") != window.window_id or
                row.get("request_fingerprint") != _sha(_encoded({"system": _SYSTEM, "user": _prompt(window)})) or
                row.get("model") != SEMANTIC_API_MODEL or row.get("finish_reason") != "stop" or
                not isinstance(row.get("response"), str) or
                _window_decision(window, row["response"], queue=queue if guarded else None) != reviewed):
            raise ValueError("semantic receipt response")


def run_semantic_review(queue, output_dir: Path, *, max_new_calls: int,
                        api_key: str | None, run_kind: str = "real") -> dict:
    """Resume per-window receipts; send at most max_new_calls new HTTP calls."""
    from dynamic_memory.memory.review_workflow import (
        ReviewApproval, TrustedReviewAuthority, verify_review_files,
    )

    if max_new_calls < 0 or run_kind not in {"real", "offline_test"}:
        raise ValueError("invalid review configuration")
    output_dir = output_dir.resolve()
    if not api_key and max_new_calls:
        raise ValueError("missing API credential")
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoints = output_dir / "checkpoints"
    checkpoints.mkdir(exist_ok=True)
    new_calls = 0
    rows = []
    for window in queue.windows:
        path = checkpoints / (window.window_id + ".json")
        attempt = checkpoints / (window.window_id + ".attempt.json")
        if path.exists():
            row = json.loads(path.read_bytes())
        elif new_calls < max_new_calls:
            if attempt.exists():
                raise ValueError("unresolved prior semantic request; do not retry automatically")
            with attempt.open("xb") as stream:
                stream.write(_encoded({"window_id": window.window_id,
                    "request_fingerprint": _sha(_encoded({"system": _SYSTEM, "user": _prompt(window)})),
                    "started_at": datetime.now(timezone.utc).isoformat()}))
            from openai import OpenAI
            client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com",
                            timeout=180, max_retries=0)
            response = client.chat.completions.create(
                model=SEMANTIC_API_MODEL,
                messages=[{"role": "system", "content": _SYSTEM},
                          {"role": "user", "content": _prompt(window)}],
                response_format={"type": "json_object"},
                extra_body={"thinking": {"type": "enabled"}},
                reasoning_effort="high", stream=False,
            )  # Deliberately omit max_tokens per this run's user authorization.
            choice = response.choices[0]
            row = {"window_id": window.window_id,
                   "request_fingerprint": _sha(_encoded({"system": _SYSTEM, "user": _prompt(window)})),
                   "model": response.model, "finish_reason": choice.finish_reason,
                   "response": choice.message.content,
                   "usage": response.usage.model_dump(mode="json") if response.usage else None}
            # Validate before recording a completed call. A malformed response is
            # still recorded so the paid attempt cannot silently disappear.
            with path.open("xb") as stream:
                stream.write(_encoded(row))
            new_calls += 1
        else:
            break
        if (row.get("window_id") != window.window_id or
                row.get("request_fingerprint") != _sha(_encoded({"system": _SYSTEM, "user": _prompt(window)})) or
                row.get("model") != SEMANTIC_API_MODEL or row.get("finish_reason") != "stop" or
                not isinstance(row.get("response"), str)):
            raise ValueError("invalid semantic checkpoint")
        _window_decision(window, row["response"], queue=queue)
        rows.append(row)
    summary = {"windows_complete": len(rows), "windows_total": len(queue.windows),
               "new_http_calls": new_calls,
               "facts_reviewed": sum(len(w.facts) for w in queue.windows[:len(rows)]),
               "checkpoint_dir": str(checkpoints)}
    if len(rows) != len(queue.windows):
        return summary
    approval_path = output_dir / "semantic_review_approval.json"
    authority_path = output_dir / "semantic_review_authority.json"
    receipts_path = output_dir / "semantic_receipts.json"
    if authority_path.exists() and not approval_path.exists():
        raise ValueError("partial semantic authority without approval")
    now = (ReviewApproval.model_validate_json(approval_path.read_bytes()).reviewed_at
           if approval_path.exists() else datetime.now(timezone.utc))
    receipt_bytes = _encoded({"format_version": 1, "queue_fingerprint": queue.fingerprint,
        "api_model": SEMANTIC_API_MODEL, "reasoning_effort": "high",
        "max_tokens": None, "windows": rows})
    approval = ReviewApproval(run_fingerprint=queue.run_fingerprint,
        queue_fingerprint=queue.fingerprint, reviewer_id=SEMANTIC_REVIEWER_ID,
        reviewed_at=now, authority_version=SEMANTIC_AUTHORITY_VERSION,
        windows=tuple(_window_decision(w, r["response"], queue=queue)
                      for w, r in zip(queue.windows, rows)))
    approval_bytes = _encoded(approval.model_dump(mode="json"))
    authority = TrustedReviewAuthority(reviewer_id=SEMANTIC_REVIEWER_ID,
        run_fingerprint=queue.run_fingerprint, queue_fingerprint=queue.fingerprint,
        approval_file_sha256=_sha(approval_bytes), authority_version=SEMANTIC_AUTHORITY_VERSION,
        allowed_run_kind=run_kind, expires_at=now + timedelta(days=30),
        semantic_receipts_sha256=_sha(receipt_bytes))
    for path, data in ((receipts_path, receipt_bytes), (approval_path, approval_bytes),
                       (authority_path, _encoded(authority.model_dump(mode="json")))):
        if path.exists():
            if path.read_bytes() != data:
                raise ValueError("semantic finalization conflict")
        else:
            with path.open("xb") as stream:
                stream.write(data)
    verified = verify_review_files(queue, approval_path, authority_path, run_kind=run_kind)
    summary.update({"status": "SEMANTIC_REVIEW_VERIFIED",
        "approved_facts": sum(f.decision == "approved" for w in verified.approval.windows for f in w.facts),
        "rejected_facts": sum(f.decision == "rejected" for w in verified.approval.windows for f in w.facts),
        "unclear_facts": sum(f.decision == "unclear" for w in verified.approval.windows for f in w.facts),
        "approval_path": str(approval_path), "authority_path": str(authority_path)})
    return summary


def derive_guarded_review(queue, source_approval_path: Path,
                          source_authority_path: Path, output_dir: Path,
                          *, run_kind: str = "real") -> dict:
    """Reassess immutable v2 responses under v3 guards with zero API calls."""
    from dynamic_memory.memory.review_workflow import (
        ReviewApproval, TrustedReviewAuthority, verify_review_files,
    )

    source = verify_review_files(queue, source_approval_path, source_authority_path,
                                 run_kind=run_kind)
    if source.approval.reviewer_id != SEMANTIC_REVIEWER_ID_V2:
        raise ValueError("source must be v2 semantic review")
    receipt_bytes = (source_authority_path.parent / "semantic_receipts.json").read_bytes()
    body = json.loads(receipt_bytes)
    rows = body["windows"]
    now = datetime.now(timezone.utc)
    approval = ReviewApproval(run_fingerprint=queue.run_fingerprint,
        queue_fingerprint=queue.fingerprint, reviewer_id=SEMANTIC_REVIEWER_ID,
        reviewed_at=now, authority_version=SEMANTIC_AUTHORITY_VERSION,
        windows=tuple(_window_decision(w, row["response"], queue=queue)
                      for w, row in zip(queue.windows, rows)))
    approval_bytes = _encoded(approval.model_dump(mode="json"))
    authority = TrustedReviewAuthority(reviewer_id=SEMANTIC_REVIEWER_ID,
        run_fingerprint=queue.run_fingerprint, queue_fingerprint=queue.fingerprint,
        approval_file_sha256=_sha(approval_bytes), authority_version=SEMANTIC_AUTHORITY_VERSION,
        allowed_run_kind=run_kind, expires_at=now + timedelta(days=30),
        semantic_receipts_sha256=_sha(receipt_bytes))
    output_dir.mkdir(parents=True, exist_ok=False)
    receipt_path = output_dir / "semantic_receipts.json"
    approval_path = output_dir / "semantic_review_approval.json"
    authority_path = output_dir / "semantic_review_authority.json"
    for path, payload in ((receipt_path, receipt_bytes), (approval_path, approval_bytes),
                          (authority_path, _encoded(authority.model_dump(mode="json")))):
        with path.open("xb") as stream:
            stream.write(payload)
    verified = verify_review_files(queue, approval_path, authority_path, run_kind=run_kind)
    return {"status": "GUARDED_SEMANTIC_REVIEW_VERIFIED", "new_http_calls": 0,
        "facts": sum(len(w.facts) for w in queue.windows),
        "approved": sum(f.decision == "approved" for w in verified.approval.windows
                        for f in w.facts),
        "unclear": sum(f.decision == "unclear" for w in verified.approval.windows
                       for f in w.facts),
        "rejected": sum(f.decision == "rejected" for w in verified.approval.windows
                        for f in w.facts),
        "approval_path": str(approval_path), "authority_path": str(authority_path)}
