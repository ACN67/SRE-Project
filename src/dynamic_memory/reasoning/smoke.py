"""Explicitly scripted synthetic demo of D's real ports; no semantic score."""

from __future__ import annotations

import json

from dynamic_memory.contracts import (
    MemoryRecord, QueryAnalysis, QueryRequest, SourceRef,
)
from dynamic_memory.contracts.memory_id import compute_memory_id
from dynamic_memory.llm.protocol import LLMResponse
from dynamic_memory.pipeline.runtime import create_runtime
from dynamic_memory.qa import DynamicAnswerer

from .common import DynamicProvenance, fingerprint
from .resolver import StateResolver


def example_input():
    memories = []
    for number, value in enumerate(("A", "B"), 1):
        memory = MemoryRecord(
            memory_id="mem_" + "0" * 20,
            sample_id="synthetic-d",
            content=f"Synthetic: Sam works at {value}.",
            entity="Sam",
            attribute="job",
            value=value,
            fact_kind="state",
            assertion_type="fact",
            polarity="positive",
            source_refs=[SourceRef(session_id=f"s{number}", dia_id="d1")],
            extractor_version="synthetic-d-v1",
        )
        memory.memory_id = compute_memory_id(memory)
        memories.append(memory.model_dump(mode="json"))
    request = QueryRequest(
        query_id="synthetic-d-q",
        sample_id="synthetic-d",
        question="Where does Sam work now?",
    )
    analysis = QueryAnalysis(
        query_id=request.query_id,
        sample_id=request.sample_id,
        query_intent="current_state",
        time_mode="latest_known",
    )
    return {
        "request": request.model_dump(mode="json"),
        "analysis": analysis.model_dump(mode="json"),
        "candidates": memories,
        "conversation_cutoff": None,
        "provenance": {
            "data_fingerprint": fingerprint("synthetic-data"),
            "query_analysis_version": "synthetic-analysis-v1",
            "git_sha": "0" * 40,
            "run_kind": "development",
            "retriever_fingerprint": fingerprint("synthetic-index"),
        },
    }


class SyntheticDemoAdapter:
    """Preset completions for example_input only; no semantic inference."""

    def generate(self, request):
        document = example_input()
        old, new = document["candidates"]
        if request.task == "state_reasoning":
            payload = {
                "schema_version": "2.1",
                "query_id": "synthetic-d-q",
                "relations": [{
                    "from_memory_id": old["memory_id"],
                    "to_memory_id": new["memory_id"],
                    "relation": "SUPERSEDE",
                    "scope_attribute": "job",
                    "evidence_refs": old["source_refs"] + new["source_refs"],
                    "reason": "Synthetic preset replacement; not a semantic inference.",
                }],
                "decisions": [
                    {
                        "memory_id": old["memory_id"],
                        "action": "EXCLUDE",
                        "reason": "Synthetic preset old state.",
                    },
                    {
                        "memory_id": new["memory_id"],
                        "action": "RETAIN",
                        "reason": "Synthetic preset new state.",
                    },
                ],
                "effective_memory_ids": [new["memory_id"]],
                "unresolved_conflict_ids": [],
                "uncertainty": None,
            }
            mode = "json"
        else:
            payload = {
                "status": "answered",
                "answer": "Synthetic preset: Sam works at B.",
                "evidence_refs": new["source_refs"],
                "uncertainty": None,
            }
            mode = "text"
        return LLMResponse(
            output_mode=mode,
            raw_output=json.dumps(payload),
            finish_reason="stop",
        )


def run_smoke():
    data = example_input()
    request = QueryRequest.model_validate(data["request"])
    analysis = QueryAnalysis.model_validate(data["analysis"])
    memories = [MemoryRecord.model_validate(item) for item in data["candidates"]]
    runtime = create_runtime(synthetic_adapter=SyntheticDemoAdapter())
    provenance = DynamicProvenance(**data["provenance"])
    resolution = StateResolver(
        runtime, provenance, synthetic_test_mode=True
    )(request, analysis, memories)
    answer = DynamicAnswerer(
        runtime, provenance, synthetic_test_mode=True
    )(request, analysis, resolution, memories)
    assert answer.used_memory_ids == [memories[1].memory_id]
    assert answer.evidence_refs == memories[1].source_refs
    return {
        "synthetic_fixture": True,
        "not_a_formal_score": True,
        "resolution": resolution.model_dump(mode="json"),
        "answer": answer.model_dump(mode="json"),
    }


if __name__ == "__main__":
    print(json.dumps(run_smoke(), ensure_ascii=False, indent=2))
