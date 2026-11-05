"""Audit deterministic extraction artifacts through retrieval and state ports.

This script deliberately does not generate answers and therefore makes no
DeepSeek API call. Gold is opened only after structural predictions are saved.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from dataclasses import asdict
from pathlib import Path

from dynamic_memory.contracts import AnswerResult, QueryResponse
from dynamic_memory.data.loader import load_conversations
from dynamic_memory.data.repository import SourceRepository
from dynamic_memory.retrieval import EmbeddingConfig, LocalIndexEncoder
from evaluation.gold_loader import load_gold_after_predictions
from evaluation.metrics.evidence_recall import score_evidence_turns
from evaluation.predictions import save_predictions
from scripts import stage6_dev_three_method as stage6


DATA = Path(__file__).resolve().parents[1] / "data/raw/locomo10.json"
SPLIT = Path(__file__).resolve().parents[1] / "configs/data_split.yaml"
SAMPLES = ("conv-26", "conv-30", "conv-41", "conv-42")


class _PrecomputedQueryEncoder:
    def __init__(self, base, questions, vectors):
        self._base = base
        self._identity = base.identity
        self._vectors = dict(zip(questions, vectors, strict=True))

    @property
    def identity(self):
        return self._identity

    def embed_documents(self, texts):
        return self._base.embed_documents(texts)

    def embed_query(self, question):
        return self._vectors[question]


def _cached_query_vectors(encoder, questions, cache_path):
    identity = encoder.identity.model_dump(mode="json")
    cache = {
        "format_version": 1,
        "encoder_identity": identity,
        "vectors": {},
    }
    if cache_path.exists():
        try:
            loaded = json.loads(cache_path.read_text(encoding="utf-8"))
            if (
                loaded.get("format_version") == 1
                and loaded.get("encoder_identity") == identity
                and isinstance(loaded.get("vectors"), dict)
            ):
                cache = loaded
        except (OSError, UnicodeError, ValueError):
            pass
    missing = [
        question for question in questions
        if question not in cache["vectors"]
    ]
    if missing:
        vectors = encoder.embed_queries(missing)
        cache["vectors"].update(zip(missing, vectors, strict=True))
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_path.with_name(
            f".{cache_path.name}.{os.getpid()}.tmp"
        )
        temporary.write_text(
            json.dumps(cache, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, cache_path)
    return [cache["vectors"][question] for question in questions]


def _json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _progress(output_dir: Path, **value) -> None:
    path = output_dir / "progress.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    print(json.dumps(value, ensure_ascii=False), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--index-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--samples",
        default=",".join(SAMPLES),
        help="comma-separated subset of the four main-dev samples",
    )
    parser.add_argument(
        "--query-cache",
        type=Path,
        default=Path("results/.cache/auto_structural_query_embeddings.json"),
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    requested_samples = tuple(
        item.strip() for item in args.samples.split(",") if item.strip()
    )
    if (
        not requested_samples
        or len(set(requested_samples)) != len(requested_samples)
        or any(sample not in SAMPLES for sample in requested_samples)
    ):
        raise SystemExit("samples must be a unique subset of the main-dev samples")

    bundle = load_conversations(str(DATA), str(SPLIT))
    repository = SourceRepository(bundle)
    encoder = LocalIndexEncoder(
        EmbeddingConfig(
            str(stage6.MODEL_ROOT),
            stage6.MODEL_REVISION,
            query_prompt=(
                "Represent this sentence for searching relevant passages: "
            ),
            document_prompt="",
        )
    )
    summary_path = args.output_dir / "auto_structural_summary.json"
    result = (
        json.loads(summary_path.read_text(encoding="utf-8"))
        if summary_path.exists()
        else {"format_version": 1, "samples": {}}
    )
    for sample_index, sample_id in enumerate(requested_samples, 1):
        structural_summary_path = (
            args.output_dir / sample_id / "structural_summary.json"
        )
        if structural_summary_path.exists():
            sample_summary = json.loads(
                structural_summary_path.read_text(encoding="utf-8")
            )
            result["samples"][sample_id] = sample_summary
            _progress(
                args.output_dir,
                status="reused_completed_sample",
                sample_id=sample_id,
                completed=sum(
                    (args.output_dir / item / "structural_summary.json").exists()
                    for item in requested_samples
                ),
                total=len(requested_samples),
            )
            continue
        started = time.perf_counter()
        _progress(
            args.output_dir,
            status="sample_started",
            sample_id=sample_id,
            sample_index=sample_index,
            total=len(requested_samples),
        )
        probe = stage6._make_system(
            sample_id=sample_id,
            question_mode="full",
            artifact_root=args.artifact_root,
            index_root=args.index_root,
            integration_mode="v2",
            encoder=encoder,
        )
        questions = [
            record.to_query_request().question
            for record in probe.manifest.records
        ]
        vectors = _cached_query_vectors(
            encoder,
            questions,
            args.query_cache,
        )
        system = stage6._make_system(
            sample_id=sample_id,
            question_mode="full",
            artifact_root=args.artifact_root,
            index_root=args.index_root,
            integration_mode="v2",
            encoder=_PrecomputedQueryEncoder(encoder, questions, vectors),
        )
        memories_by_id = {
            memory.memory_id: memory for memory in system.index.memories
        }
        vector_responses = []
        dynamic_responses = []
        for record in system.manifest.records:
            prepared = system.orchestrator.prepare_query(record.to_query_request())
            if prepared.must_early_stop:
                vector_responses.append(
                    prepared.make_early_response("vector_rag")
                )
                dynamic_responses.append(
                    prepared.make_early_response("dynamic")
                )
                continue
            candidates = system.orchestrator._retrieve(
                prepared.request,
                prepared.analysis,
                system.index,
            )
            selected = [
                memories_by_id[item.memory_id]
                for item in sorted(candidates.items, key=lambda item: item.rank)
            ]
            resolution = system.orchestrator._resolve(
                prepared.request,
                prepared.analysis,
                selected,
            )
            vector_responses.append(
                QueryResponse(
                    query_id=prepared.request.query_id,
                    sample_id=prepared.request.sample_id,
                    question=prepared.request.question,
                    method="vector_rag",
                    query_analysis=prepared.analysis,
                    retrieved=candidates,
                    resolution=None,
                    answer_result=AnswerResult(
                        status="no_evidence",
                        uncertainty="Offline structural audit; answer generation was not run.",
                    ),
                )
            )
            dynamic_responses.append(
                QueryResponse(
                    query_id=prepared.request.query_id,
                    sample_id=prepared.request.sample_id,
                    question=prepared.request.question,
                    method="dynamic",
                    query_analysis=prepared.analysis,
                    retrieved=candidates,
                    resolution=resolution,
                    answer_result=AnswerResult(
                        status="no_evidence",
                        uncertainty="Offline structural audit; answer generation was not run.",
                    ),
                )
            )

        sample_dir = args.output_dir / sample_id
        vector_receipt = save_predictions(
            sample_dir / "vector_rag.predictions.json",
            system.manifest,
            vector_responses,
            "vector_rag",
        )
        dynamic_receipt = save_predictions(
            sample_dir / "dynamic.predictions.json",
            system.manifest,
            dynamic_responses,
            "dynamic",
        )
        gold = load_gold_after_predictions(
            DATA,
            SPLIT,
            vector_receipt.path,
            system.manifest,
            vector_receipt,
            annotation_source="course_gold",
        )
        vector_recall = score_evidence_turns(
            system.manifest,
            vector_responses,
            gold,
            repository,
            "vector_rag",
        )
        dynamic_recall = score_evidence_turns(
            system.manifest,
            dynamic_responses,
            gold,
            repository,
            "dynamic",
        )
        sample_summary = {
            "status": "complete",
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "question_count": len(system.manifest.records),
            "vector_prediction_sha256": vector_receipt.file_sha256,
            "dynamic_prediction_sha256": dynamic_receipt.file_sha256,
            "vector_recall": [
                asdict(item) for item in vector_recall.by_method
            ],
            "dynamic_recall": [
                asdict(item) for item in dynamic_recall.by_method
            ],
            "candidate_sets_identical": all(
                vector.retrieved == dynamic.retrieved
                for vector, dynamic in zip(
                    vector_responses,
                    dynamic_responses,
                    strict=True,
                )
            ),
            "state_status_counts": {
                status: sum(
                    decision.action == status
                    for response in dynamic_responses
                    if response.resolution is not None
                    for decision in response.resolution.decisions
                )
                for status in ("RETAIN", "EXCLUDE", "UNRESOLVED")
            },
        }
        _json(
            args.output_dir / sample_id / "structural_summary.json",
            sample_summary,
        )
        result["samples"][sample_id] = sample_summary
        _progress(
            args.output_dir,
            status="sample_completed",
            sample_id=sample_id,
            completed=sum(
                (args.output_dir / item / "structural_summary.json").exists()
                for item in requested_samples
            ),
            total=len(requested_samples),
        )
    result["requested_samples"] = sorted(
        set(result.get("requested_samples", ())) | set(requested_samples)
    )
    result["elapsed_seconds"] = round(
        sum(
            item["elapsed_seconds"]
            for item in result["samples"].values()
        ),
        3,
    )
    _json(summary_path, result)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
