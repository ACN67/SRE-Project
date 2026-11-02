"""Explicitly synthetic memories/questions, real local embedding, no QA model."""

import argparse
import json

from dynamic_memory.contracts.models import QueryRequest
from dynamic_memory.pipeline.settings import load_settings
from .embedding import EmbeddingConfig
from .index import IndexManager, save_index
from .index_embedding import LocalIndexEncoder
from .index_smoke import synthetic_artifact
from .query_analysis import analyze_query
from .vector_retrieval import VectorRetriever


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=".venv/models/all-MiniLM-L6-v2")
    parser.add_argument("--revision", default="1110a243fdf4706b3f48f1d95db1a4f5529b4d41")
    parser.add_argument("--output", default="results/retrieval-smoke/synthetic-index.json")
    args = parser.parse_args()
    encoder = LocalIndexEncoder(EmbeddingConfig(args.model, args.revision))
    settings = load_settings().retrieval.model_copy(update={"embedding_model": args.model})
    source = synthetic_artifact()
    manager, retriever = IndexManager(encoder), VectorRetriever(encoder, settings)
    index = manager.build_index(source, settings)
    save_index(index, args.output)
    loaded = manager.load_index(args.output, source, settings)
    results = []
    for number, (question, expected) in enumerate([
        ("What is Sam's job?", "teacher"), ("What sport does Sam like?", "football"),
    ]):
        request = QueryRequest(query_id=f"synthetic-q{number}", sample_id=index.sample_id, question=question)
        analysis = analyze_query(request)
        snapshot = retriever.retrieve_with_trace(request, analysis, index)
        candidates = snapshot.candidates
        assert snapshot == retriever.retrieve_with_trace(request, analysis, loaded)
        memories = retriever.load_candidates(candidates, loaded)
        assert expected in memories[0].content
        results.append({"question": question, "initial_topk_ids": snapshot.initial_topk_ids,
                        "expanded_ids": snapshot.expanded_ids,
                        "final_candidate_ids": snapshot.final_candidate_ids, "top1": memories[0].content,
                        "scores": [item.score for item in candidates.items],
                        "reload_equal": True})
    print(json.dumps({"run_kind": "synthetic_fixture_real_embedding", "results": results}, indent=2))


if __name__ == "__main__":
    main()
