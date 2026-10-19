"""Run explicitly requested local-model inference on synthetic English text."""

from __future__ import annotations

import argparse
import json
from importlib.metadata import version

from .embedding import EmbeddingConfig, SentenceTransformerEmbedder


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="sentence-transformers/all-MiniLM-L6-v2")
    parser.add_argument("--revision", required=True, help="Model commit SHA or local snapshot version")
    parser.add_argument("--allow-download", action="store_true")
    args = parser.parse_args()
    embedder = SentenceTransformerEmbedder(EmbeddingConfig(
        model_name=args.model, revision=args.revision,
        local_files_only=not args.allow_download,
    ))
    texts = ["Sam works as a teacher in Shanghai.", "Sam likes playing football."]
    documents = embedder.embed_documents(texts)
    query = embedder.embed_query("What is Sam's job?")
    print(json.dumps({
        "run_kind": "synthetic_real_embedding_smoke",
        "model": args.model, "revision": args.revision,
        "sentence_transformers_version": version("sentence-transformers"),
        "configuration_fingerprint": embedder.configuration_fingerprint,
        "dimension": len(query),
        "examples": [{"text": text, "similarity": sum(x * y for x, y in zip(query, vector))}
                     for text, vector in zip(texts, documents)],
    }, indent=2))


if __name__ == "__main__":
    main()
