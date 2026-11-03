"""A deliberately fake human file is accepted only for synthetic offline testing."""

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from dynamic_memory.data.repository import SourceRepository
from dynamic_memory.contracts import ConversationSample, ConversationSession, DatasetBundle, SourceTurn
from dynamic_memory.integration.artifacts import ArtifactPorts
from dynamic_memory.memory.checkpoints import WindowCheckpointStore, call_identity_fingerprint
from dynamic_memory.memory.extraction import AtomicFactDraft, WindowExtractionResult, MEMORY_DRAFT_PROMPT
from dynamic_memory.memory.normalization import normalize_memories
from dynamic_memory.memory.review_workflow import (
    FactDecision, ReviewApproval, ReviewError, TrustedReviewAuthority,
    WindowDecision, export_review_queue, make_review_queue, verify_review_files, _fp,
)
from dynamic_memory.memory.windows import plan_extraction_windows
from tests.integration.test_reviewed_production_store import fixture_63, fp
from scripts.stage_b_review_tool import main as review_main


def files(tmp_path, queue, *, reject=False, unclear=False, reviewer="offline-test", stale=False):
    now = datetime.now(timezone.utc)
    decisions = []
    for number, window in enumerate(queue.windows):
        facts = tuple(FactDecision(draft_fingerprint=f.draft_fingerprint,
            evidence_fingerprint=f.evidence_fingerprint,
            decision=("rejected" if reject and number == 0 else
                      "unclear" if unclear and number == 0 else "approved"),
            reason_code="FAKE_HUMAN_REVIEW",
            confirmed_source_fingerprints=(() if (reject or unclear) and number == 0 else tuple(
                _fp(source.model_dump(mode="json")) for source in f.cited_turns)),
            relation_confirmed=(not (reject or unclear) or number != 0) and len(f.cited_turns) > 1)
            for f in window.facts)
        decisions.append(WindowDecision(window_id=window.window_id,
            source_refs_fingerprint=window.source_refs_fingerprint,
            decision="reviewed", facts=facts))
    approval = ReviewApproval(run_fingerprint=queue.run_fingerprint,
        queue_fingerprint=fp("other queue") if stale else queue.fingerprint,
        reviewer_id=reviewer, reviewed_at=now, authority_version="offline-v1",
        windows=tuple(decisions))
    approval_path = tmp_path / "human" / "approval.json"
    approval_path.parent.mkdir(parents=True, exist_ok=True)
    approval_bytes = (json.dumps(approval.model_dump(mode="json"),
        ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    approval_path.write_bytes(approval_bytes)
    authority = TrustedReviewAuthority(reviewer_id="offline-test",
        run_fingerprint=queue.run_fingerprint, queue_fingerprint=queue.fingerprint,
        approval_file_sha256="sha256:" + hashlib.sha256(approval_bytes).hexdigest(),
        authority_version="offline-v1", allowed_run_kind="offline_test",
        expires_at=now + timedelta(days=1))
    authority_path = tmp_path / "independent-authority" / "allowlist.json"
    authority_path.parent.mkdir(parents=True, exist_ok=True)
    authority_path.write_text(authority.model_dump_json(), encoding="utf-8")
    return approval_path, authority_path


def full_queue(tmp_path):
    checkpoint = WindowCheckpointStore(tmp_path / "private-checkpoints",
        run_fingerprint=fp("synthetic-run"), max_new_attempts=63)
    bundle, settings, report, receipt, builder, runtime, client = fixture_63(
        checkpoint, return_context=True)
    sample = SourceRepository(bundle).get_sample("synthetic-0")
    windows = tuple(plan_extraction_windows(sample, settings.extraction,
                                           reserved_prompt_tokens=1800))
    identities = {w.extraction_window_id: call_identity_fingerprint(builder.identity_factory(w))
                  for w in windows}
    results = {w.extraction_window_id: checkpoint.load(w, identities[w.extraction_window_id])
               for w in windows}
    queue = make_review_queue(run_fingerprint=checkpoint.run_fingerprint,
        sample_id="synthetic-0", windows=windows, call_fingerprints=identities,
        results=results, repository=SourceRepository(bundle))
    assert len(queue.windows) == 63 and sum(len(w.facts) for w in queue.windows) == 63
    export_review_queue(queue, tmp_path / "private-review" / "queue.json")
    assert client.calls == 63
    return bundle, settings, report, receipt, builder, client, queue


def test_63_window_private_queue_external_approval_publish_and_reload(tmp_path):
    bundle, settings, report, receipt, builder, client, queue = full_queue(tmp_path)
    approval_path, authority_path = files(tmp_path, queue)
    verified = verify_review_files(queue, approval_path, authority_path,
                                   run_kind="offline_test")
    builder.window_evidence_checker = verified.checker_for_window
    replay = builder.build_memories_with_report(SourceRepository(bundle).get_sample("synthetic-0"))
    assert client.calls == 63 and replay.build_status == "complete"
    assert replay.memories == report.memories
    receipt = receipt.model_copy(update={
        "review_log_fingerprint": verified.approval_fingerprint,
        "review_authority_fingerprint": _fp(verified.authority.model_dump(mode="json")),
    })
    ports = ArtifactPorts(tmp_path / "published", bundle, settings, production=True)
    artifact = ports.store("synthetic-0").publish_reviewed(replay, receipt,
                                                            verified_review=verified)
    assert ports.load("synthetic-0") == artifact
    assert artifact.manifest.production_provenance.run_kind == "offline_test"


def test_rejected_fact_is_audited_and_excluded_before_publication(tmp_path):
    bundle, settings, report, receipt, builder, client, queue = full_queue(tmp_path)
    approval_path, authority_path = files(tmp_path, queue, reject=True)
    verified = verify_review_files(queue, approval_path, authority_path,
                                   run_kind="offline_test")
    builder.window_evidence_checker = verified.checker_for_window
    builder.window_fact_filter = verified.filter_for_window
    replay = builder.build_memories_with_report(SourceRepository(bundle).get_sample("synthetic-0"))
    assert replay.build_status == "complete" and len(replay.memories) == 62
    assert replay.window_diagnostics[0].input_fact_count == 1
    assert replay.window_diagnostics[0].accepted_fact_count == 0
    assert replay.window_diagnostics[0].rejected_fact_count == 1
    assert client.calls == 63


def test_unclear_fact_is_excluded_and_multi_source_needs_explicit_relation(tmp_path):
    sample = ConversationSample(sample_id="synthetic-multi", source_order_1_based=1,
        sessions=[ConversationSession(session_id="session_1", turns=[
            SourceTurn(sample_id="synthetic-multi", session_id="session_1",
                       dia_id="d1", speaker="Ada", text="Ada moved to Paris."),
            SourceTurn(sample_id="synthetic-multi", session_id="session_1",
                       dia_id="d2", speaker="Ada", text="Ada now lives in Paris."),
        ])])
    bundle = DatasetBundle(snapshot_sha256="a" * 64, samples=[sample],
                           split_by_sample_id={"synthetic-multi": "main_dev"})
    repository = SourceRepository(bundle)
    settings = __import__("dynamic_memory.pipeline.settings", fromlist=["load_settings"]).load_settings()
    window = plan_extraction_windows(sample, settings.extraction, reserved_prompt_tokens=1800)[0]
    draft = AtomicFactDraft(content="Ada moved to Paris and now lives there.",
        entity="Ada", attribute="residence", value="Paris", fact_kind="state",
        assertion_type="fact", polarity="positive", recorded_at=None, event_time=None,
        source_refs=list(window.source_refs))
    result = WindowExtractionResult(sample.sample_id, window.extraction_window_id,
        (draft,), MEMORY_DRAFT_PROMPT.template_fingerprint, 100, "synthetic")
    queue = make_review_queue(run_fingerprint=fp("multi-source"),
        sample_id=sample.sample_id, windows=(window,),
        call_fingerprints={window.extraction_window_id: fp("call")},
        results={window.extraction_window_id: result}, repository=repository)
    assert queue.windows[0].facts[0].automatic_status == "unclear"
    approval_path, authority_path = files(tmp_path, queue)
    verified = verify_review_files(queue, approval_path, authority_path,
                                   run_kind="offline_test")
    chosen = verified.filter_for_window(window.extraction_window_id)(result)
    assert len(chosen.facts) == 1
    assert len(normalize_memories(sample.sample_id, [chosen], repository,
        extractor_version="synthetic", evidence_checker=verified.checker_for_window(
            window.extraction_window_id))) == 1
    approval = ReviewApproval.model_validate_json(approval_path.read_bytes())
    bad_fact = approval.windows[0].facts[0].model_copy(update={"relation_confirmed": False})
    bad_window = approval.windows[0].model_copy(update={"facts": (bad_fact,)})
    approval_path.write_text(approval.model_copy(update={"windows": (bad_window,)}).model_dump_json(),
                             encoding="utf-8")
    authority = TrustedReviewAuthority.model_validate_json(authority_path.read_bytes())
    authority_path.write_text(authority.model_copy(update={
        "approval_file_sha256": "sha256:" + hashlib.sha256(approval_path.read_bytes()).hexdigest()
    }).model_dump_json(), encoding="utf-8")
    with pytest.raises(ReviewError, match="REVIEW_REJECTED_OR_STALE"):
        verify_review_files(queue, approval_path, authority_path, run_kind="offline_test")

    unclear_approval, unclear_authority = files(tmp_path / "unclear", queue, unclear=True)
    unclear_verified = verify_review_files(queue, unclear_approval, unclear_authority,
                                           run_kind="offline_test")
    assert not unclear_verified.filter_for_window(window.extraction_window_id)(result).facts


@pytest.mark.parametrize("change", ["wrong_reviewer", "stale_queue", "tamper", "expired", "source_tamper"])
def test_review_rejects_untrusted_or_stale_files(tmp_path, change):
    _, _, _, _, _, _, queue = full_queue(tmp_path)
    approval_path, authority_path = files(tmp_path, queue,
        reject=change == "reject", reviewer="other" if change == "wrong_reviewer" else "offline-test",
        stale=change == "stale_queue")
    if change == "tamper":
        approval_path.write_bytes(approval_path.read_bytes() + b" ")
    if change == "expired":
        authority = TrustedReviewAuthority.model_validate_json(authority_path.read_bytes())
        authority_path.write_text(authority.model_copy(update={
            "expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)
        }).model_dump_json(), encoding="utf-8")
    if change == "source_tamper":
        window = queue.windows[0]
        fact = window.facts[0]
        changed_turn = fact.cited_turns[0].model_copy(update={"text": "Altered private source."})
        changed_fact = fact.model_copy(update={"cited_turns": (changed_turn,)})
        changed_window = window.model_copy(update={"facts": (changed_fact,)})
        queue = queue.model_copy(update={"windows": (changed_window,) + queue.windows[1:]})
    with pytest.raises(ReviewError, match="REVIEW_REJECTED_OR_STALE"):
        verify_review_files(queue, approval_path, authority_path, run_kind="offline_test")


def test_review_cli_only_scaffolds_pending_decisions(tmp_path, monkeypatch, capsys):
    _, _, _, _, _, _, queue = full_queue(tmp_path)
    queue_path = tmp_path / "private-review/queue.json"
    template = tmp_path / "human/pending.json"
    monkeypatch.setattr("sys.argv", ["review-tool", "--queue", str(queue_path),
                                    "--pending-template", str(template), "--run-kind", "offline_test"])
    assert review_main() == 0
    data = json.loads(template.read_text(encoding="utf-8"))
    assert data["reviewer_id"] is None and data["windows"][0]["facts"][0]["decision"] == "PENDING"
    assert "PENDING_HUMAN_REVIEW" in capsys.readouterr().out
    approval_path, authority_path = files(tmp_path, queue)
    monkeypatch.setattr("sys.argv", ["review-tool", "--queue", str(queue_path),
        "--verify", "--approval", str(approval_path), "--authority", str(authority_path),
        "--run-kind", "offline_test"])
    assert review_main() == 0
    assert "VERIFIED_FILE_CONSISTENCY" in capsys.readouterr().out
