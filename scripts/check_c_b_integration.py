"""B disk artifact -> C index probe with synthetic data and deterministic vectors.

Executes explicitly selected, trusted local B source in a temporary overlay.
No checkout merge, real dataset, model downloads, paid APIs or official Gold.
"""

import argparse
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile


def check(root):
    from dynamic_memory.contracts import QueryRequest
    from dynamic_memory.memory.artifact_store import ArtifactStoreError, MemoryArtifactStore
    from dynamic_memory.retrieval import EncoderIdentity, IndexManager, VectorRetriever, analyze_query, save_index
    from dynamic_memory.retrieval.index import RetrievalIndexError, fingerprint

    # Reuse B's clearly labelled synthetic source and fixed Mock builder, not
    # its test assertions: below we exercise C's actual public consumer ports.
    fixtures = runpy.run_path(str(root / "b_synthetic_fixture.py"))
    data = root / "synthetic-data"
    data.mkdir()
    bundle, sample = fixtures["_snapshot"](data)
    builder, _, settings = fixtures["_runtime"](bundle, sample)
    report = builder.build_memories_with_report(sample)
    store = MemoryArtifactStore(root / "artifacts", bundle, settings.extraction,
                                settings.models, allow_mock_publication=True)
    store.publish(report)
    source = store.load()
    assert len(source.memories) == 2

    class SyntheticEncoder:
        identity = EncoderIdentity(model_name="synthetic-b-c", model_revision="v1",
                                   model_fingerprint=fingerprint("synthetic-vectors"),
                                   encoding_fingerprint=fingerprint("synthetic-encoding"))

        def embed_documents(self, texts):
            return [[1.0, 0.0] if "Paris" in text else [0.0, 1.0] for text in texts]

        def embed_query(self, question):
            return [1.0, 0.0]

    def rejected(code, action, error_type):
        try:
            action()
        except error_type as error:
            assert error.code == code, error.code
        else:
            raise AssertionError(f"expected {code}")

    encoder = SyntheticEncoder()
    config = settings.retrieval.model_copy(update={"embedding_model": encoder.identity.model_name})
    manager = IndexManager(encoder)
    index_path = root / "synthetic-index.json"
    index = manager.build_index(source, config)
    save_index(index, index_path)
    loaded = manager.load_index(index_path, store.load(), config)
    assert loaded == index
    assert loaded.source_manifest_fingerprint == source.source_manifest_fingerprint
    assert loaded.memory_ids == tuple(sorted(m.memory_id for m in source.memories))
    request = QueryRequest(query_id="synthetic-b-c", sample_id=sample.sample_id,
                           question="Where does Ada live?")
    retriever = VectorRetriever(encoder, config)
    candidates = retriever.retrieve(request, analyze_query(request), loaded)
    memories = retriever.load_candidates(candidates, loaded)
    assert "Paris" in memories[0].content
    assert {m.memory_id for m in memories} == {m.memory_id for m in source.memories}
    assert all(builder.repository.get_turn(sample.sample_id, ref).text == m.content
               for m in memories for ref in m.source_refs)

    partial_builder, _, _ = fixtures["_runtime"](bundle, sample, rejected_session="session_2")
    rejected("ARTIFACT_INCOMPLETE", lambda: store.publish(
        partial_builder.build_memories_with_report(sample)), ArtifactStoreError)
    assert store.load().source_manifest_fingerprint == source.source_manifest_fingerprint
    # A new publication must invalidate C even with identical Memory contents.
    store.publish(report)
    current = store.load()
    assert current.source_manifest_fingerprint != source.source_manifest_fingerprint
    rejected("INDEX_STALE", lambda: manager.load_index(index_path, current, config), RetrievalIndexError)
    save_index(manager.build_index(current, config), index_path)
    manager.load_index(index_path, store.load(), config)
    pointer = json.loads((root / "artifacts/current.json").read_text(encoding="utf-8"))
    (root / "artifacts/builds" / pointer["build_id"] / "memories.jsonl").write_bytes(b"corrupt")
    rejected("ARTIFACT_CORRUPT", store.load, ArtifactStoreError)
    print(json.dumps({"b_c_integration": "PASS", "fixture": "synthetic",
                      "strict_B_disk_loader": True, "index_roundtrip": True,
                      "source_refs_verified": True, "partial_publication_rejected": True,
                      "republish_invalidates_index": True, "disk_corruption_rejected": True,
                      "real_embedding": False, "production_extraction": False}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--b-ref", required=True, help="Trusted B commit available locally")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    revision = subprocess.check_output(["git", "rev-parse", "--verify", f"{args.b_ref}^{{commit}}"],
                                       cwd=root, text=True).strip()
    with tempfile.TemporaryDirectory(prefix="acn-c-b-") as folder:
        temporary = Path(folder)
        overlay = temporary / "src"
        shutil.copytree(root / "src/dynamic_memory", overlay / "dynamic_memory",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copytree(root / "configs", temporary / "configs")
        for name in ("windows", "extraction", "normalization", "build", "artifact_store"):
            relative = f"src/dynamic_memory/memory/{name}.py"
            (temporary / relative).write_bytes(subprocess.check_output(
                ["git", "show", f"{revision}:{relative}"], cwd=root))
        (temporary / "b_synthetic_fixture.py").write_bytes(subprocess.check_output(
            ["git", "show", f"{revision}:tests/integration/test_memory_pipeline_offline.py"], cwd=root))
        bootstrap = ("import sys,runpy; sys.path.insert(0,sys.argv[1]); "
                     "m=runpy.run_path(sys.argv[2]); m['check'](m['Path'](sys.argv[3]))")
        subprocess.run([sys.executable, "-c", bootstrap, str(overlay), str(Path(__file__).resolve()),
                        str(temporary)], cwd=root, check=True)
    print(json.dumps({"b_revision": revision, "checkout_merged": False}))


if __name__ == "__main__":
    main()
