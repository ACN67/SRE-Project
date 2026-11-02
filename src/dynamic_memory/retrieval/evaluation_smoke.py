"""Controlled synthetic recall demonstration, not a real embedding benchmark."""

import argparse
import hashlib
import json
import math
from pathlib import Path

from dynamic_memory.contracts import MemoryArtifact, SourceRef, memory_set_fingerprint
from dynamic_memory.contracts.memory_id import compute_memory_id
from dynamic_memory.contracts.models import QueryRequest
from dynamic_memory.pipeline.settings import load_settings
from .evaluation import RetrievalEvaluationCase, evaluate_retrieval
from .index import EncoderIdentity, IndexManager, fingerprint
from .index_smoke import synthetic_artifact
from .vector_retrieval import VectorRetriever


class _SyntheticEncoder:
    identity = EncoderIdentity(model_name="synthetic-evaluation", model_revision="v1",
                               model_fingerprint=fingerprint("synthetic-evaluation-weights"),
                               encoding_fingerprint=fingerprint("synthetic-evaluation-encoding"))

    def embed_documents(self, texts):
        rows = []
        for text in texts:
            score = 0.99 if text == "Sam works at B." else 0.90 if text.startswith("Distractor") else 0.1
            rows.append([score, math.sqrt(1 - score ** 2)])
        return rows

    def embed_query(self, question):
        return [1.0, 0.0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="results/retrieval-evaluation/synthetic-report.json")
    args = parser.parse_args()
    original = synthetic_artifact()
    texts = ["Sam works at B.", "Sam left A.", "Sam worked at A."] + [f"Distractor {i}" for i in range(8)]
    memories = []
    for i, text in enumerate(texts):
        memory = original.memories[0].model_copy(update={
            "content": text, "entity": "Sam" if i < 3 else None,
            "attribute": "employment" if i < 3 else None,
            "source_refs": [SourceRef(session_id="synthetic_session", dia_id=f"synthetic_turn_{i}")]})
        memories.append(memory.model_copy(update={"memory_id": compute_memory_id(memory)}))
    jsonl = "".join(m.model_dump_json() + "\n" for m in memories).encode("utf-8")
    source = MemoryArtifact(manifest=original.manifest.model_copy(update={
        "memory_count": len(memories), "memory_set_fingerprint": memory_set_fingerprint(memories),
        "memories_jsonl_sha256": hashlib.sha256(jsonl).hexdigest(),
        "source_turns_fingerprint": fingerprint([m.content for m in memories])}), memories=tuple(memories))
    settings = load_settings().retrieval.model_copy(update={"embedding_model": "synthetic-evaluation"})
    encoder = _SyntheticEncoder()
    index = IndexManager(encoder).build_index(source, settings)
    case = RetrievalEvaluationCase(case_id="synthetic-job-history", label_kind="synthetic_fixture", split="main_dev",
        request=QueryRequest(query_id="synthetic-job-history", sample_id=index.sample_id, question="Where has Sam worked?"),
        source_manifest_fingerprint=index.source_manifest_fingerprint,
        relevant_memory_ids=[m.memory_id for m in memories[:3]])
    report = evaluate_retrieval([case], indexes={index.sample_id: index},
                                retriever=VectorRetriever(encoder, settings), recall_at_k=settings.recall_at_k)
    assert report["summary"]["pure_vector"]["hits"] == 1
    assert report["summary"]["final_candidates"]["hits"] == 3
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"label_kind": "synthetic_fixture", "encoder": "controlled_synthetic_vectors",
                      "summary": report["summary"], "output": str(output)}, indent=2))


if __name__ == "__main__":
    main()
