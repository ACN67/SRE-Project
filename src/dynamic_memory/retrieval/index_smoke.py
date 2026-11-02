"""Build/save/reload a real local-model index using explicitly synthetic Memory."""

import argparse
from datetime import datetime, timezone
import hashlib
import json

from dynamic_memory.contracts import MemoryArtifact, MemoryManifest, MemoryRecord, SourceRef, memory_set_fingerprint
from dynamic_memory.contracts.memory_id import compute_memory_id
from dynamic_memory.pipeline.settings import load_settings

from .embedding import EmbeddingConfig
from .index import IndexManager, RetrievalIndexError, fingerprint, save_index
from .index_embedding import LocalIndexEncoder


def synthetic_artifact() -> MemoryArtifact:
    memories = []
    for number, text in enumerate(("Sam works as a teacher in Shanghai.", "Sam likes playing football.")):
        memory = MemoryRecord(
            memory_id="mem_" + "0" * 20, sample_id="synthetic_index_demo", content=text,
            fact_kind="state", assertion_type="fact", polarity="positive",
            source_refs=[SourceRef(session_id="synthetic_session", dia_id=f"synthetic_turn_{number}")],
            extractor_version="synthetic-handwritten-v1",
        )
        memories.append(memory.model_copy(update={"memory_id": compute_memory_id(memory)}))
    jsonl = "".join(record.model_dump_json() + "\n" for record in memories).encode("utf-8")
    manifest = MemoryManifest(
        build_id="synthetic-index-demo-v1", sample_id="synthetic_index_demo",
        created_at=datetime(2026, 10, 26, tzinfo=timezone.utc),
        dataset_snapshot_sha256=hashlib.sha256(b"synthetic-index-demo").hexdigest(),
        source_turns_fingerprint=fingerprint([m.content for m in memories]),
        extraction_config_fingerprint=fingerprint("synthetic-handwritten"),
        extractor_version="synthetic-handwritten-v1", prompt_fingerprint=fingerprint("no-extraction-prompt"),
        backend="mock", resolved_model_id=None, build_status="complete", partial_build=False,
        total_windows=1, successful_windows=1, failed_windows=(), memory_count=len(memories),
        memories_jsonl_sha256=hashlib.sha256(jsonl).hexdigest(), memory_set_fingerprint=memory_set_fingerprint(memories),
    )
    return MemoryArtifact(manifest=manifest, memories=tuple(memories))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=".venv/models/all-MiniLM-L6-v2")
    parser.add_argument("--revision", default="1110a243fdf4706b3f48f1d95db1a4f5529b4d41")
    parser.add_argument("--output", default="results/index-smoke/synthetic-index.json")
    args = parser.parse_args()
    manager = IndexManager(LocalIndexEncoder(EmbeddingConfig(args.model, args.revision)))
    settings = load_settings().retrieval.model_copy(update={"embedding_model": args.model})
    artifact = synthetic_artifact()
    index = manager.build_index(artifact, settings)
    save_index(index, args.output)
    loaded = manager.load_index(args.output, artifact, settings)
    assert loaded == index
    try:
        manager.load_index(args.output, artifact, settings.model_copy(update={"base_top_k": 7}))
    except RetrievalIndexError as error:
        assert error.code == "INDEX_STALE"
    else:
        raise AssertionError("changed settings must reject the old index")
    print(json.dumps({"run_kind": "synthetic_memories_real_embedding_index", "memory_count": len(index.memory_ids),
                      "dimension": index.dimension, "reload_equal": True, "changed_settings_rejected": True,
                      "retriever_fingerprint": index.retriever_fingerprint, "output": args.output}, indent=2))


if __name__ == "__main__":
    main()
