"""Full 63-window offline Fake SDK publication; explicitly never a real B run."""

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from dynamic_memory.contracts import (
    ConversationSample, ConversationSession, DatasetBundle, SourceTurn,
    memory_set_fingerprint,
)
from dynamic_memory.data.repository import SourceRepository
from dynamic_memory.integration.artifacts import ArtifactPorts
from dynamic_memory.memory.artifact_store import (
    ArtifactStoreError, ReviewedMemoryArtifactStore, ReviewAttestation, WindowReview,
)
from dynamic_memory.memory.build import MemoryBuilder
from dynamic_memory.memory.checkpoints import WindowCheckpointStore
from dynamic_memory.memory.extraction import MEMORY_DRAFT_PROMPT
from dynamic_memory.memory.strict_evidence import strict_evidence_checker
from dynamic_memory.pipeline.runtime import create_runtime
from dynamic_memory.pipeline.settings import load_settings


def fp(value):
    return "sha256:" + hashlib.sha256((json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()).hexdigest()


class FakeSDK:
    def __init__(self):
        self.calls = 0
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.calls += 1
        assert kwargs["model"] == "deepseek-flash"
        assert kwargs["reasoning_effort"] == "high"
        assert kwargs["extra_body"] == {"thinking": {"type": "enabled"}}
        assert "temperature" not in kwargs
        assert "max_tokens" not in kwargs
        window = json.loads(kwargs["messages"][-1]["content"].split("\n", 1)[1])
        item = window["turns"][0]
        record = {"content": item["visible_text"], "entity": "Ada", "attribute": "note",
                  "value": "note", "fact_kind": "state", "assertion_type": "fact",
                  "polarity": "positive", "recorded_at": None, "event_time": None,
                  "source_refs": [item["source_ref"]]}
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
            message=SimpleNamespace(content=json.dumps({"facts": [record]}), reasoning_content="private fake thought"))],
            model="deepseek-flash", usage=SimpleNamespace(prompt_tokens=100, completion_tokens=80,
                completion_tokens_details=SimpleNamespace(reasoning_tokens=10)))


def fixture_63(checkpoint_store=None, expected_calls=63, expect_complete=True, return_context=False):
    sessions = [ConversationSession(session_id=f"session_{i}", turns=[SourceTurn(
        sample_id="synthetic-0", session_id=f"session_{i}", dia_id="d1", speaker="Ada",
        text=f"Ada kept note number {i}.")]) for i in range(1, 64)]
    sample = ConversationSample(sample_id="synthetic-0", source_order_1_based=1, sessions=sessions)
    bundle = DatasetBundle(snapshot_sha256="a" * 64, samples=[sample],
                           split_by_sample_id={"synthetic-0": "main_dev"})
    settings = load_settings(models_profile=__import__("pathlib").Path(__file__).resolve().parents[2] /
                             "configs" / "models_high_pilot.yaml")
    client = FakeSDK()
    runtime = create_runtime(settings, backend="deepseek", api_enabled=True,
                             deepseek_client=client, model_id="deepseek-flash", cache_enabled=False)
    def identity(window):
        prepared = runtime.make_request(MEMORY_DRAFT_PROMPT, {"conversation": window.input_text})
        return runtime.make_identity(prepared, sample_id=window.sample_id,
            scope_id=window.extraction_window_id, run_kind="development", method=None,
            data_fingerprint=fp(window.input_text), schema_id="atomic_fact_draft",
            schema_version="1", retriever_fingerprint=None, query_analysis_version=None,
            git_sha="a" * 40)
    builder = MemoryBuilder(SourceRepository(bundle), runtime.recorder, settings.models,
                            settings.extraction, identity, extractor_version="offline-test-v1",
                            evidence_checker=strict_evidence_checker,
                            checkpoint_store=checkpoint_store)
    report = builder.build_memories_with_report(sample)
    assert client.calls == expected_calls
    if not expect_complete:
        return bundle, settings, report, None
    assert report.build_status == "complete" and report.successful_windows == 63
    assert len(report.memories) == 63
    receipt = ReviewAttestation(
        run_kind="offline_test", reviewer_id="offline-test",
        execution_policy_fingerprint=runtime.execution_policy_fingerprint,
        reviewed_memory_set_fingerprint=memory_set_fingerprint(report.memories),
        review_log_fingerprint=fp("explicit synthetic review ledger"),
        windows=tuple(WindowReview(
            extraction_window_id=d.extraction_window_id,
            source_refs_fingerprint=fp([r.model_dump(mode="json") for r in d.source_refs]),
            approved_fact_count=d.accepted_fact_count, decision="approved")
            for d in report.window_diagnostics),
    )
    if return_context:
        return bundle, settings, report, receipt, builder, runtime, client
    return bundle, settings, report, receipt


def test_63_fake_windows_publish_and_c_handoff(tmp_path):
    bundle, settings, report, receipt = fixture_63()
    ports = ArtifactPorts(tmp_path / "artifacts", bundle, settings, production=True)
    store = ports.store("synthetic-0")
    assert isinstance(store, ReviewedMemoryArtifactStore)
    with pytest.raises(ArtifactStoreError, match="ARTIFACT_INCOMPLETE"):
        store.publish(report)  # legacy Mock writer is still closed here
    artifact = store.publish_reviewed(report, receipt)
    assert artifact.manifest.backend == "deepseek"
    assert artifact.manifest.production_provenance.run_kind == "offline_test"
    assert ports.load("synthetic-0") == artifact
    downstream = ReviewedMemoryArtifactStore(
        store.root, bundle, settings.extraction,
        settings.models.model_copy(update={
            "qa_max_input_tokens": 16384,
            "qa_max_output_tokens": 512,
            "qa_omit_max_tokens": False,
        }),
    )
    assert downstream.load() == artifact
    assert ports.verified("synthetic-0").artifact == artifact
    artifact.require_complete_for_index()
    assert len(store.import_memories()) == 63
    assert len(list((store.root / "builds").iterdir())) == 1
    assert not (store.root / "current.json").read_text().find("private fake thought") >= 0


def test_partial_or_bad_review_never_switches_pointer(tmp_path):
    bundle, settings, report, receipt = fixture_63()
    store = ReviewedMemoryArtifactStore(tmp_path / "artifacts", bundle,
        settings.extraction, settings.models)
    first = store.publish_reviewed(report, receipt)
    before = (store.root / "current.json").read_bytes()
    bad = receipt.model_copy(update={"reviewed_memory_set_fingerprint": fp("wrong")})
    with pytest.raises(ArtifactStoreError, match="REVIEW_MISMATCH"):
        store.publish_reviewed(report, bad)
    bad_window = receipt.windows[0].model_copy(update={"approved_fact_count": 0})
    bad = receipt.model_copy(update={"windows": (bad_window,) + receipt.windows[1:]})
    with pytest.raises(ArtifactStoreError, match="REVIEW_MISMATCH"):
        store.publish_reviewed(report, bad)
    diagnostics = list(report.window_diagnostics)
    diagnostics[-1] = replace(diagnostics[-1], status="failed", error_code="SYNTHETIC_FAILURE",
                              accepted_fact_count=0, output_memory_count=0)
    partial = replace(report, build_status="partial", partial_build=True,
                      successful_windows=62, window_diagnostics=tuple(diagnostics))
    with pytest.raises(ArtifactStoreError, match="ARTIFACT_INCOMPLETE"):
        store.publish_reviewed(partial, receipt)
    assert (store.root / "current.json").read_bytes() == before
    assert store.load().manifest.build_id == first.manifest.build_id


def test_tampered_review_receipt_fails_readback(tmp_path):
    bundle, settings, report, receipt = fixture_63()
    store = ReviewedMemoryArtifactStore(tmp_path / "artifacts", bundle,
        settings.extraction, settings.models)
    artifact = store.publish_reviewed(report, receipt)
    review_file = store.root / "builds" / artifact.manifest.build_id / "review_receipt.json"
    review_file.write_text("{}", encoding="utf-8")
    with pytest.raises(ArtifactStoreError, match="ARTIFACT_CORRUPT"):
        store.load()


def test_lineage_receipt_is_required_and_tampering_fails_readback(tmp_path):
    from dynamic_memory.memory.review_workflow import _fp
    from dynamic_memory.memory.windows import plan_extraction_windows
    bundle, settings, report, receipt = fixture_63()
    sample = SourceRepository(bundle).get_sample("synthetic-0")
    windows = plan_extraction_windows(sample, settings.extraction, reserved_prompt_tokens=1800)
    body = {"sample_id": sample.sample_id, "dataset_sha256": bundle.snapshot_sha256,
        "windows": [{"window_id": w.extraction_window_id,
                     "source_refs_fingerprint": _fp([r.model_dump(mode="json") for r in w.source_refs])}
                    for w in windows]}
    lineage = {"body": body, "digest": _fp(body)}
    payload = (json.dumps(lineage, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    receipt = receipt.model_copy(update={
        "extraction_lineage_fingerprint": "sha256:" + hashlib.sha256(payload).hexdigest()})
    store = ReviewedMemoryArtifactStore(tmp_path / "artifacts", bundle,
        settings.extraction, settings.models)
    with pytest.raises(ArtifactStoreError, match="REVIEW_MISMATCH"):
        store.publish_reviewed(report, receipt)
    artifact = store.publish_reviewed(report, receipt, lineage_receipt=payload)
    assert store.load().manifest.build_id == artifact.manifest.build_id
    path = store.root / "builds" / artifact.manifest.build_id / "extraction_lineage.json"
    path.write_bytes(payload + b" ")
    with pytest.raises(ArtifactStoreError, match="ARTIFACT_CORRUPT"):
        store.load()


def test_checkpoint_replay_and_stale_identity_fail_closed(tmp_path):
    checkpoint = WindowCheckpointStore(tmp_path / "private-checkpoints", run_fingerprint=fp("run-a"),
                                       max_new_attempts=63)
    bundle, settings, report, receipt = fixture_63(checkpoint)
    assert len(list(checkpoint.root.glob("window_*.json"))) == 63
    _, _, replay, _ = fixture_63(checkpoint, expected_calls=0)
    assert replay.memories == report.memories
    stale = WindowCheckpointStore(checkpoint.root, run_fingerprint=fp("run-b"))
    _, _, failed, _ = fixture_63(stale, expected_calls=0, expect_complete=False)
    assert failed.build_status == "failed"
    assert all(d.error_code == "CHECKPOINT_CORRUPT_OR_STALE" for d in failed.window_diagnostics)


def test_attempt_claim_is_durable_and_never_implicitly_retried(tmp_path):
    from dynamic_memory.memory.checkpoints import CheckpointError
    from dynamic_memory.memory.windows import plan_extraction_windows
    bundle, settings, _, _ = fixture_63()
    sample = SourceRepository(bundle).get_sample("synthetic-0")
    first, second = plan_extraction_windows(sample, settings.extraction,
                                             reserved_prompt_tokens=1800)[:2]
    root = tmp_path / "ledger"
    store = WindowCheckpointStore(root, run_fingerprint=fp("run-a"), max_new_attempts=1)
    store.claim_new_attempt(first, fp("call-a"))
    reopened = WindowCheckpointStore(root, run_fingerprint=fp("run-a"), max_new_attempts=1)
    with pytest.raises(CheckpointError, match="CHECKPOINT_ATTEMPT_SPENT"):
        reopened.claim_new_attempt(first, fp("call-a"))
    with pytest.raises(CheckpointError, match="CHECKPOINT_BUDGET_EXHAUSTED"):
        reopened.claim_new_attempt(second, fp("call-b"))
    (root / "attempts.json").write_text("{}", encoding="utf-8")
    with pytest.raises(CheckpointError, match="CHECKPOINT_CORRUPT_OR_STALE"):
        reopened.claim_new_attempt(second, fp("call-b"))
