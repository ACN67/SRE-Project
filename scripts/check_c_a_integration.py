"""Run C against an explicitly selected local Git revision of A, using synthetic data.

No checkout/merge, network, model download, paid API or B artifact-loader claim.
This executes project Python source from the selected revision; use reviewed refs.
"""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import types

from dynamic_memory.contracts import ConversationSample, ConversationSession, DatasetBundle, SourceTurn
from dynamic_memory.contracts.models import AnswerResult, MemoryDecision, QueryRequest, ResolutionResult
from dynamic_memory.data.repository import SourceRepository
from dynamic_memory.llm.protocol import LLMResponse
from dynamic_memory.pipeline.runtime import create_runtime
from dynamic_memory.pipeline.settings import load_settings
from dynamic_memory.retrieval import (EncoderIdentity, IndexManager, SharedRetrieval, VectorRetriever,
                                      VectorAnswerer, VectorAnswerProvenance, analyze_query, QUERY_ANALYSIS_VERSION)
from dynamic_memory.retrieval.index import fingerprint
from dynamic_memory.retrieval.index import RetrievalIndexError
from dynamic_memory.retrieval.index_smoke import synthetic_artifact


def load_a(ref):
    root = Path(__file__).resolve().parents[1]
    revision = subprocess.check_output(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"], cwd=root, text=True).strip()
    package = types.ModuleType("_synthetic_c_a_probe")
    package.__path__ = []
    sys.modules[package.__name__] = package
    for name in ("query_conditions", "query_runner"):
        path = f"src/dynamic_memory/pipeline/{name}.py"
        source = subprocess.check_output(["git", "show", f"{revision}:{path}"], cwd=root).decode("utf-8")
        module = types.ModuleType(f"{package.__name__}.{name}")
        sys.modules[module.__name__] = module
        exec(compile(source, f"git:{revision}:{path}", "exec"), module.__dict__)
    return sys.modules[f"{package.__name__}.query_runner"].QueryOrchestrator, revision


class SyntheticEncoder:
    identity = EncoderIdentity(model_name="synthetic-probe", model_revision="v1",
                               model_fingerprint=fingerprint("synthetic-model"),
                               encoding_fingerprint=fingerprint("synthetic-encoding"))

    def __init__(self):
        self.query_calls = 0

    def embed_documents(self, texts):
        return [[1.0, 0.0] if "teacher" in text else [0.0, 1.0] for text in texts]

    def embed_query(self, question):
        self.query_calls += 1
        return [1.0, 0.0]


class SyntheticQA:
    def generate(self, request):
        raw = request.messages[1].content.split("Context data:\n", 1)[1].split("\n\nQuestion data:", 1)[0]
        memories = json.loads(raw)
        return LLMResponse(output_mode="text", finish_reason="stop", raw_output=json.dumps({
            "status": "answered", "answer": "Synthetic test: Sam is a teacher.",
            "evidence_refs": memories[0]["source_refs"], "uncertainty": None}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--a-ref", required=True, help="Reviewed local Git commit/ref for A; no fetch is performed")
    args = parser.parse_args()
    orchestrator, revision = load_a(args.a_ref)
    source = synthetic_artifact()
    turns = [SourceTurn(sample_id=m.sample_id, session_id=r.session_id, dia_id=r.dia_id,
                        speaker="Synthetic Sam", text=m.content) for m in source.memories for r in m.source_refs]
    sample = ConversationSample(sample_id=source.manifest.sample_id, source_order_1_based=1,
                                sessions=[ConversationSession(session_id="synthetic_session", turns=turns)])
    repository = SourceRepository(DatasetBundle(snapshot_sha256=source.manifest.dataset_snapshot_sha256,
                                                samples=[sample], split_by_sample_id={sample.sample_id: "main_dev"}))
    encoder = SyntheticEncoder()
    settings = load_settings().retrieval.model_copy(update={"embedding_model": encoder.identity.model_name})
    index = IndexManager(encoder).build_index(source, settings)
    shared = SharedRetrieval(VectorRetriever(encoder, settings))
    runtime = create_runtime(synthetic_adapter=SyntheticQA())
    answerer = VectorAnswerer(runtime, VectorAnswerProvenance(
        "sha256:" + source.manifest.dataset_snapshot_sha256, QUERY_ANALYSIS_VERSION, revision, "development"),
        index, synthetic_test_mode=True)

    # Explicit D stub only: checks shared inputs, not real dynamic-state behavior.
    dynamic_ids = []
    def resolve(request, analysis, memories):
        dynamic_ids.extend(m.memory_id for m in memories)
        return ResolutionResult(query_id=request.query_id,
                                decisions=[MemoryDecision(memory_id=m.memory_id, action="RETAIN",
                                                          reason="synthetic wiring stub") for m in memories],
                                effective_memory_ids=[m.memory_id for m in memories])

    def dynamic_answer(request, analysis, resolution, memories):
        return AnswerResult(status="no_evidence", uncertainty="Synthetic D stub; no real reasoning performed.")

    runner = orchestrator(analyze_query=analyze_query, get_sample=repository.get_sample,
                          load_verified_index=lambda _: index,  # synthetic only; NOT B disk verification
                          retrieve=shared.retrieve, load_candidates=shared.load_candidates,
                          answer_vector_rag=answerer, resolve=resolve, answer_dynamic=dynamic_answer)
    request = QueryRequest(query_id="synthetic-probe-q", sample_id=sample.sample_id, question="What is Sam's job?")
    prepared = runner.prepare_query(request)
    vector = runner.run_prepared(prepared, "vector_rag")
    dynamic = runner.run_prepared(prepared, "dynamic")
    assert vector.answer_result.status == "answered", vector.model_dump_json()
    assert dynamic.answer_result.status == "no_evidence", dynamic.model_dump_json()
    assert vector.retrieved == dynamic.retrieved
    assert dynamic_ids == [item.memory_id for item in vector.retrieved.items]
    assert encoder.query_calls == 1
    # A must stop conflicting times before retrieving or generating.
    from datetime import date
    conflict = request.model_copy(update={"question": "Where did Sam work in 2020?", "as_of": date(2021, 1, 1)})
    assert runner.run_query(conflict, "vector_rag").answer_result.status == "time_uncertain"
    assert encoder.query_calls == 1
    def stale_index(_):
        raise RetrievalIndexError("INDEX_STALE")
    failed_runner = orchestrator(analyze_query=analyze_query, get_sample=repository.get_sample,
                                 load_verified_index=stale_index)
    assert failed_runner.run_query(request, "vector_rag").answer_result.error.code == "INDEX_STALE"
    def corrupt_index(_):
        raise RetrievalIndexError("INDEX_CORRUPT")
    failed_runner = orchestrator(analyze_query=analyze_query, get_sample=repository.get_sample,
                                 load_verified_index=corrupt_index)
    # Current A whitelist does not preserve this C code; track this integration gap.
    assert failed_runner.run_query(request, "vector_rag").answer_result.error.code == "COMPONENT_FAILED"
    print(json.dumps({"run_kind": "synthetic_A_C_wiring_with_D_stub", "a_revision": revision,
                      "vector_status": vector.answer_result.status, "shared_candidates_equal": True,
                      "question_encodings": encoder.query_calls, "time_conflict_early_stop": True,
                      "stale_error_preserved": True, "corrupt_error_mapping": "COMPONENT_FAILED",
                      "real_B_artifact_loader": False, "real_D_reasoning": False}, indent=2))


if __name__ == "__main__":
    main()
