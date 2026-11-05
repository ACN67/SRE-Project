"""Offline smoke for the real A/B/C artifact + index with a fake D provider."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from dynamic_memory.contracts import QueryRequest
from dynamic_memory.integration.production import create_multi_sample_system
from dynamic_memory.pipeline.runtime import create_runtime
from dynamic_memory.pipeline.settings import load_settings
from dynamic_memory.web.bindings import WebBindings


class FakeDClient:
    """Provider-shaped no-network client for D wiring validation."""

    def __init__(self):
        self.calls = 0
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.calls += 1
        user = kwargs["messages"][-1]["content"]
        if "Candidate context data:\n" in user:
            context = json.loads(
                user.split("Candidate context data:\n", 1)[1].split(
                    "\n\nOutput JSON schema:", 1
                )[0]
            )
            memories = context["memories"]
            content = json.dumps({
                "schema_version": "2.1",
                "query_id": json.loads(
                    user.split("Query data:\n", 1)[1].split(
                        "\n\nCandidate context data:", 1
                    )[0]
                )["request"]["query_id"],
                "relations": [],
                "decisions": [{
                    "memory_id": memory["memory_id"],
                    "action": "RETAIN",
                    "reason": "No-network wiring smoke retains all candidates.",
                } for memory in memories],
                "effective_memory_ids": [
                    memory["memory_id"] for memory in memories
                ],
                "unresolved_conflict_ids": [],
                "uncertainty": None,
            })
            mode = "json"
        else:
            content = "No-network D wiring smoke answer."
            mode = "text"
        return SimpleNamespace(
            model="deepseek-flash",
            choices=[SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content=content),
            )],
            usage=SimpleNamespace(
                prompt_tokens=1,
                completion_tokens=1,
                completion_tokens_details=SimpleNamespace(reasoning_tokens=0),
            ),
        )


def _write_new(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", required=True, type=Path)
    parser.add_argument("--index-path", required=True, type=Path)
    parser.add_argument("--model-root", required=True, type=Path)
    parser.add_argument("--model-revision", required=True)
    parser.add_argument("--sample-id", default="conv-30")
    parser.add_argument(
        "--split",
        choices=("main_dev", "extra_dev", "test"),
        default="main_dev",
    )
    parser.add_argument(
        "--question",
        default="How is Gina's store doing?",
    )
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    if not output.is_relative_to(
        (Path(__file__).resolve().parents[1] / "results").resolve()
    ):
        raise SystemExit("OUTPUT_DIRECTORY_UNSAFE")
    if output.exists() and any(output.iterdir()):
        raise SystemExit("OUTPUT_DIRECTORY_IN_USE")
    settings = load_settings(
        models_profile=Path(__file__).resolve().parents[1]
        / "configs/models_high_pilot.yaml"
    )
    client = FakeDClient()
    runtime = create_runtime(
        settings,
        backend="deepseek",
        api_enabled=True,
        deepseek_client=client,
        model_id="deepseek-flash",
        token_estimator=lambda messages: sum(
            len(message.content.encode("utf-8")) + 16 for message in messages
        ),
        estimator_version="utf8-bytes-plus16-v1",
        strict_budget=True,
        cache_enabled=False,
    )
    service = create_multi_sample_system(
        artifact_root=args.artifact_root,
        index_paths={args.sample_id: args.index_path},
        model_root=args.model_root,
        model_revision=args.model_revision,
        sample_ids=(args.sample_id,),
        allowed_splits=(args.split,),
        settings=settings,
        runtime=runtime,
    )
    bindings = WebBindings(
        service=service,
        label="No-network D wiring smoke",
        synthetic=False,
        dynamic_available=True,
    )
    request = QueryRequest(
        query_id=f"d-production-wiring-{args.sample_id}",
        sample_id=args.sample_id,
        question=args.question,
    )
    vector = bindings.run_query(request, "vector_rag")
    dynamic = bindings.run_query(request, "dynamic")
    candidate_count = (
        len(dynamic.retrieved.items)
        if dynamic.retrieved is not None else None
    )
    resolution_count = (
        len(dynamic.resolution.decisions)
        if dynamic.resolution is not None else None
    )
    effective_count = (
        len(dynamic.resolution.effective_memory_ids)
        if dynamic.resolution is not None else None
    )
    result = {
        "status": "D_PRODUCTION_WIRING_PASS"
        if (
            vector.retrieved == dynamic.retrieved
            and dynamic.resolution is not None
            and dynamic.answer_result.status == "answered"
        )
        else "D_PRODUCTION_WIRING_FAILED",
        "network_calls": 0,
        "fake_provider_invocations": client.calls,
        "sample_id": args.sample_id,
        "split": args.split,
        "candidate_count": candidate_count,
        "vector_status": vector.answer_result.status,
        "dynamic_status": dynamic.answer_result.status,
        "dynamic_error": (
            dynamic.answer_result.error.model_dump(mode="json")
            if dynamic.answer_result.error is not None else None
        ),
        "same_ordered_candidates": vector.retrieved == dynamic.retrieved,
        "decisions": resolution_count,
        "effective_ids": effective_count,
    }
    _write_new(output / "summary.json", result)
    print(json.dumps(result))
    return 0 if result["status"] == "D_PRODUCTION_WIRING_PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
