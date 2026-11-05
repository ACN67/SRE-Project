"""Actual B/C/A/D/E service wiring with synthetic completions, never formal scores."""

import json
from concurrent.futures import ThreadPoolExecutor

from dynamic_memory.contracts import QueryRequest
from dynamic_memory.integration.offline import (
    create_service, prepare_artifacts, prepare_indexes, prepare_source,
)
from dynamic_memory.llm.protocol import LLMResponse
from dynamic_memory.pipeline.runtime import create_runtime
from dynamic_memory.reasoning import DynamicProvenance, DynamicServicePorts
from dynamic_memory.reasoning.common import fingerprint
from dynamic_memory.web.bindings import WebBindings


class SyntheticDynamic:
    def generate(self, request):
        if request.task == "state_reasoning":
            text = request.messages[-1].content
            query_value = json.loads(
                text.split("Query data:\n")[1].split(
                    "\n\nCandidate context data:"
                )[0]
            )["request"]
            memories = json.loads(
                text.split("Candidate context data:\n")[1].split(
                    "\n\nOutput JSON schema:"
                )[0]
            )["memories"]
            selected = [
                item for item in memories if "notebook" in item["content"]
            ]
            raw = {
                "schema_version": "2.1",
                "query_id": query_value["query_id"],
                "relations": [],
                "decisions": [{
                    "memory_id": item["memory_id"],
                    "action": "RETAIN" if item in selected else "EXCLUDE",
                    "reason": "Synthetic notebook fixture selection.",
                } for item in memories],
                "effective_memory_ids": [
                    item["memory_id"] for item in selected
                ],
                "unresolved_conflict_ids": [],
                "uncertainty": None,
            }
            return LLMResponse(
                output_mode="json",
                raw_output=json.dumps(raw),
                finish_reason="stop",
            )
        text = request.messages[-1].content
        memories = json.loads(
            text.split("Context data:\n")[1].split("\n\nQuestion data:")[0]
        )
        raw = {
            "status": "answered",
            "answer": "Synthetic: blue notebook.",
            "evidence_refs": memories[0]["source_refs"],
            "uncertainty": None,
        }
        return LLMResponse(
            output_mode="text",
            raw_output=json.dumps(raw),
            finish_reason="stop",
        )


def test_b_builder_c_index_a_orchestrator_d_ports_and_e_bindings(tmp_path):
    root = tmp_path / "synthetic-state"
    prepare_source(root)
    prepare_artifacts(root)
    indexes = prepare_indexes(root)
    runtime = create_runtime(
        synthetic_adapter=SyntheticDynamic(), sleep=lambda _: None
    )
    ports = DynamicServicePorts(
        runtime,
        synthetic_test_mode=True,
        provenance_factory=lambda sample: DynamicProvenance(
            fingerprint("synthetic-data"),
            "synthetic-analysis-v1",
            "0" * 40,
            "development",
            indexes[sample],
        ),
    )
    service = create_service(
        root, resolve=ports.resolve, answer_dynamic=ports.answer_dynamic
    )
    bindings = WebBindings(
        service=service,
        label="Synthetic B/C/A/D/E",
        synthetic=True,
        dynamic_available=True,
    )

    def run(number):
        sample = f"synthetic-{number % 2}"
        request = QueryRequest(
            query_id=f"synthetic-d-{number}",
            sample_id=sample,
            question="What does Ada keep?",
        )
        vector = bindings.run_query(request, "vector_rag")
        dynamic = bindings.run_query(request, "dynamic")
        assert dynamic.retrieved == vector.retrieved
        assert dynamic.answer_result.status == "answered"
        assert dynamic.answer_result.answer == "Synthetic: blue notebook."
        assert len(dynamic.resolution.decisions) == len(vector.retrieved.items) == 2
        assert len(dynamic.answer_result.used_memory_ids) == 2
        assert any("notebook" in service.get_memory(sample, used).content
                   for used in dynamic.answer_result.used_memory_ids)
        assert service.get_turn(
            sample, dynamic.answer_result.evidence_refs[0]
        ).text
        return dynamic.sample_id

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(run, range(12)))
    assert set(results) == {"synthetic-0", "synthetic-1"}
