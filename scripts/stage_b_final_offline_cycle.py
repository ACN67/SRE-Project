"""Synthetic Fake High 63-window review and real local BGE cycle; zero API calls."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from dynamic_memory.integration.artifacts import ArtifactPorts
from dynamic_memory.memory.review_workflow import ReviewQueue
from dynamic_memory.retrieval import EmbeddingConfig, LocalIndexEncoder
from scripts.stage_b_repair_runner import run_plan
from tests.integration.test_batch_runner import approved_plan
from tests.integration.test_review_workflow import files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    weight_sha256 = hashlib.sha256((args.model / "model.safetensors").read_bytes()).hexdigest()
    if weight_sha256 != "3c9f31665447c8911517620762200d2245a2518d6e7208acc78cd9db317e21ad":
        raise RuntimeError("local BGE weights drifted")
    plan, sdk, approval, digest = approved_plan(output)
    state = output / "state"
    extracted = run_plan(plan, state_dir=state, approval=approval,
                         approval_fingerprint=digest, allow_new_calls=True)
    if extracted["status"] != "PENDING_REVIEW" or extracted["completed_windows"] != 63:
        raise RuntimeError("synthetic extraction incomplete")
    queue = ReviewQueue.model_validate_json((state / "private_review" / "queue.json").read_bytes())
    review_file, authority_file = files(output, queue, reject=True)
    encoder = LocalIndexEncoder(EmbeddingConfig(str(args.model.resolve()), args.revision,
        query_prompt="Represent this sentence for searching relevant passages: ",
        document_prompt=""))
    probe = encoder.embed_query("synthetic Ada note")
    if len(probe) != 384 or abs(sum(value * value for value in probe) - 1) > 0.001:
        raise RuntimeError("local BGE dimension or normalization drifted")
    published = run_plan(plan, state_dir=state, approval=approval,
        approval_fingerprint=digest, allow_new_calls=False,
        review_file=review_file, authority_file=authority_file,
        index_encoder=encoder)
    if published["status"] != "PUBLISHED" or sdk.calls != 63:
        raise RuntimeError("synthetic reviewed publication incomplete")
    artifact = ArtifactPorts(state / "artifacts", plan.bundle, plan.settings,
                             production=True).verified("synthetic-0").artifact
    index_path = Path(published["index_path"])
    print(json.dumps({"run_kind": "offline_test", "api_network_calls": 0,
        "fake_sdk_calls": sdk.calls, "planned_windows": 63,
        "reviewed_facts": sum(len(w.facts) for w in queue.windows),
        "published_memories": published["memory_count"], "rejected_facts": 1,
        "build_id": published["build_id"],
        "source_manifest_fingerprint": artifact.source_manifest_fingerprint,
        "memory_set_fingerprint": artifact.manifest.memory_set_fingerprint,
        "index_path": str(index_path),
        "index_sha256": hashlib.sha256(index_path.read_bytes()).hexdigest(),
        "index_fingerprint": published["index_fingerprint"],
        "candidate_count": published["candidate_count"],
        "encoder_fingerprint": published["encoder_fingerprint"],
        "bge_weight_sha256": weight_sha256, "dimension": len(probe)},
        ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
