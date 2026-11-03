"""Explicitly synthetic 63-window Fake B artifact with real local BGE C index."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

from dynamic_memory.contracts.models import QueryRequest
from dynamic_memory.integration.artifacts import ArtifactPorts
from dynamic_memory.retrieval import (
    EmbeddingConfig, IndexManager, LocalIndexEncoder, VectorRetriever,
    analyze_query, save_index,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # Reuse the deliberately labeled offline Fake SDK fixture; no network model calls.
    fixture_path = Path(__file__).resolve().parents[1] / "tests/integration/test_reviewed_production_store.py"
    spec = importlib.util.spec_from_file_location("stage_b_fake_fixture", fixture_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    bundle, settings, report, receipt = module.fixture_63()
    ports = ArtifactPorts(args.output / "artifacts", bundle, settings, production=True)
    store = ports.store("synthetic-0")
    artifact = store.publish_reviewed(report, receipt)
    artifact = ports.verified("synthetic-0").artifact
    config = EmbeddingConfig(str(args.model), args.revision,
        query_prompt="Represent this sentence for searching relevant passages: ",
        document_prompt="")
    encoder = LocalIndexEncoder(config)
    retrieval = settings.retrieval.model_copy(update={"embedding_model": str(args.model)})
    manager = IndexManager(encoder)
    index = manager.build_index(artifact, retrieval)
    args.output.mkdir(parents=True, exist_ok=True)
    target = args.output / "synthetic-index.json"
    save_index(index, target)
    loaded = manager.load_index(target, artifact, retrieval)
    request = QueryRequest(query_id="synthetic-q1", sample_id="synthetic-0",
                           question="What note did Ada keep?")
    analysis = analyze_query(request)
    snapshot = VectorRetriever(encoder, retrieval).retrieve_with_trace(request, analysis, loaded)
    assert loaded.index_fingerprint == index.index_fingerprint
    assert snapshot.candidates.items and all(item.memory_id in {m.memory_id for m in artifact.memories}
                                             for item in snapshot.candidates.items)
    print(json.dumps({"run_kind": "synthetic_fake_b_real_bge_c", "sample_id": "synthetic-0",
        "memory_count": len(artifact.memories), "planned_windows": report.total_windows,
        "source_manifest_fingerprint": artifact.source_manifest_fingerprint,
        "index_fingerprint": index.index_fingerprint, "encoder_identity": encoder.identity.model_dump(mode="json"),
        "dimension": len(encoder.embed_query("note")), "candidate_count": len(snapshot.candidates.items),
        "reload_equal": True, "index_path": str(target)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
