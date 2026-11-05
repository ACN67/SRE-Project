"""Build a reproducible main_dev freeze manifest from completed run artifacts."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from dynamic_memory.llm.prompts import QA_BENCHMARK_PROMPT
from dynamic_memory.memory.extraction import MEMORY_DRAFT_PROMPT
from dynamic_memory.reasoning.resolver import DYNAMIC_RESOLUTION_PROMPT
from scripts.stage4_non_d_fixed import _code_fingerprint


ARTIFACT_ROOT = Path(
    r"C:\Users\Keshu\Documents\SRE-Project-RuntimeAssets\semantic_v3_artifacts"
)
INDEX_ROOT = Path(
    r"C:\Users\Keshu\Documents\SRE-Project-RuntimeAssets\semantic_v3_indexes"
)


class FreezeError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise FreezeError("INPUT_READ_FAILED") from None
    if not isinstance(value, dict):
        raise FreezeError("INPUT_INVALID")
    return value


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        raise FreezeError("INPUT_READ_FAILED") from None
    return digest.hexdigest()


def _artifact(sample: str) -> dict:
    pointer = _read(ARTIFACT_ROOT / sample / "current.json")
    build_id = pointer.get("build_id")
    expected_manifest_sha = pointer.get("manifest_sha256")
    if not isinstance(build_id, str) or not isinstance(expected_manifest_sha, str):
        raise FreezeError("ARTIFACT_POINTER_INVALID")
    manifest_path = ARTIFACT_ROOT / sample / "builds" / build_id / "manifest.json"
    if _sha(manifest_path) != expected_manifest_sha:
        raise FreezeError("ARTIFACT_MANIFEST_HASH_MISMATCH")
    manifest = _read(manifest_path)
    provenance = manifest.get("production_provenance")
    if not isinstance(provenance, dict):
        raise FreezeError("ARTIFACT_PROVENANCE_MISSING")
    return {
        "build_id": build_id,
        "manifest_sha256": expected_manifest_sha,
        "memory_count": manifest.get("memory_count"),
        "memory_set_fingerprint": manifest.get("memory_set_fingerprint"),
        "memories_json_sha256": manifest.get("memories_json_sha256"),
        "memories_jsonl_sha256": manifest.get("memories_jsonl_sha256"),
        "extraction_config_fingerprint": manifest.get(
            "extraction_config_fingerprint"
        ),
        "prompt_fingerprint": manifest.get("prompt_fingerprint"),
        "source_turns_fingerprint": manifest.get("source_turns_fingerprint"),
        "total_windows": manifest.get("total_windows"),
        "successful_windows": manifest.get("successful_windows"),
        "review_fingerprint": provenance.get("review_fingerprint"),
        "review_authority_fingerprint": provenance.get(
            "review_authority_fingerprint"
        ),
        "reviewer_id": provenance.get("reviewer_id"),
        "review_status": provenance.get("review_status"),
        "run_kind": provenance.get("run_kind"),
        "execution_policy_fingerprint": provenance.get(
            "execution_policy_fingerprint"
        ),
        "window_plan_fingerprint": provenance.get("window_plan_fingerprint"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--aggregate", required=True, type=Path)
    parser.add_argument("--validation-aggregate", type=Path)
    parser.add_argument("--state-score", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.output.exists():
            raise FreezeError("OUTPUT_EXISTS")
        aggregate = _read(args.aggregate)
        samples = aggregate.get("samples")
        if (
            aggregate.get("status") != "STAGE6_DEV_AGGREGATE_COMPLETE"
            or not isinstance(samples, list)
            or not samples
            or aggregate.get("question_count") != 757
        ):
            raise FreezeError("AGGREGATE_INVALID")
        plans = {}
        artifacts = {}
        index_files = {}
        for sample in samples:
            run_dir = args.run_root / sample
            plan = _read(run_dir / "plan.json")
            summary = _read(run_dir / "run_summary.json")
            if (
                plan.get("status") != "STAGE6_DEV_THREE_METHOD_PLAN_READY"
                or summary.get("status")
                != "STAGE6_DEV_THREE_METHOD_LIVE_COMPLETE"
                or plan.get("identity", {}).get("split") != "main_dev"
            ):
                raise FreezeError("RUN_INVALID")
            plans[sample] = plan
            artifacts[sample] = _artifact(sample)
            index_path = INDEX_ROOT / f"{sample}.json"
            index_files[sample] = {
                "sha256": _sha(index_path),
                "bytes": index_path.stat().st_size,
            }
        identities = [plan["identity"] for plan in plans.values()]
        stable_fields = (
            "dataset_sha256",
            "code_fingerprint",
            "runtime_config_fingerprint",
            "execution_policy_fingerprint",
            "embedding_model_revision",
        )
        if any(
            len({identity.get(field) for identity in identities}) != 1
            for field in stable_fields
        ):
            raise FreezeError("IDENTITY_MISMATCH")

        output = {
            "format_version": 1,
            "status": "MAIN_DEV_CONFIGURATION_FROZEN",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "scope": {
                "split": "main_dev",
                "samples": samples,
                "question_count": aggregate["question_count"],
                "test_samples_executed": False,
            },
            "identity": {
                "dataset_sha256": identities[0]["dataset_sha256"],
                "prediction_code_fingerprint": identities[0][
                    "code_fingerprint"
                ],
                "final_tree_code_fingerprint": _code_fingerprint(),
                "runtime_config_fingerprint": identities[0][
                    "runtime_config_fingerprint"
                ],
                "execution_policy_fingerprint": identities[0][
                    "execution_policy_fingerprint"
                ],
                "embedding_model_revision": identities[0][
                    "embedding_model_revision"
                ],
                "questions_file_sha256": aggregate.get(
                    "questions_file_sha256"
                ),
                "run_fingerprints": aggregate.get("run_fingerprints"),
                "prediction_sha256": aggregate.get("prediction_sha256"),
            },
            "prompts": {
                "extraction": MEMORY_DRAFT_PROMPT.template_fingerprint,
                "state_resolution": (
                    DYNAMIC_RESOLUTION_PROMPT.template_fingerprint
                ),
                "benchmark_qa": QA_BENCHMARK_PROMPT.template_fingerprint,
            },
            "evaluation": {
                "gold_policy": "course_gold_after_prediction_sha_verification",
                "numeric_gold_normalization": "decimal_text_v1",
                "refusal_normalization": "canonical_no_information_available_v1",
                "official_scorer_commit": (
                    "cbfbc1dba6bc53d00625212a0f22d55ffee7c1fc"
                ),
            },
            "artifacts": artifacts,
            "indexes": index_files,
            "aggregate": {
                "path": str(args.aggregate),
                "sha256": _sha(args.aggregate),
            },
            "validation_100": (
                {
                    "path": str(args.validation_aggregate),
                    "sha256": _sha(args.validation_aggregate),
                }
                if args.validation_aggregate is not None
                else None
            ),
            "state_evaluation": (
                {
                    "path": str(args.state_score),
                    "sha256": _sha(args.state_score),
                    "annotation_source": _read(args.state_score).get(
                        "annotation_source"
                    ),
                    "state_update_accuracy": _read(args.state_score).get(
                        "state_update_accuracy"
                    ),
                    "relation_label_accuracy": _read(args.state_score)[
                        "relation_label_metrics"
                    ].get("end_to_end_accuracy"),
                    "decision_accuracy": _read(args.state_score)[
                        "decision_metrics"
                    ].get("end_to_end_accuracy"),
                    "effective_set_exact_accuracy": _read(args.state_score)[
                        "effective_set_metrics"
                    ].get("exact_match_accuracy"),
                }
                if args.state_score is not None
                else None
            ),
            "test_policy": {
                "main_dev_only": True,
                "test_questions_run": False,
                "test_gold_used_for_tuning": False,
                "gold_opened_only_after_prediction_sha_verification": True,
                "model_id": "deepseek-flash",
                "thinking": "enabled",
                "reasoning_effort": "high",
                "max_tokens_omitted": True,
                "retry_attempts": 0,
                "max_concurrency_per_sample": 1,
                "question_selection": "frozen split main_dev, all questions",
            },
            "integration_status": {
                "memory_and_retrieval_pipeline_runnable": True,
                "formal_compatibility_repair_pending": [
                    "memory retrieval quality and full-text efficiency",
                    "semantic coverage for causal and implicit facts",
                    "cross-module runtime integration on test split",
                ],
            },
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(output, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
        print(json.dumps({
            "status": output["status"],
            "output": str(args.output),
            "question_count": output["scope"]["question_count"],
        }))
        return 0
    except Exception as exc:
        print(json.dumps({
            "status": getattr(exc, "code", "FREEZE_STOPPED"),
            "error_type": type(exc).__name__,
        }))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
