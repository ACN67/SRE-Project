"""Score saved Dynamic predictions against reviewed state annotations."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

from dynamic_memory.data.loader import load_conversations
from dynamic_memory.data.repository import SourceRepository
from dynamic_memory.integration.artifacts import ArtifactPorts
from dynamic_memory.pipeline.settings import load_settings
from evaluation.metrics.state_annotations import load_state_annotations
from evaluation.metrics.state_metrics import MemoryCatalog, score_state
from evaluation.predictions import PredictionReceipt, verify_predictions
from scripts.stage6_dev_three_method import (
    DEFAULT_ARTIFACT_ROOT,
    PROFILE,
)
from scripts.stage6_evaluate_dev import _manifest


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/raw/locomo10.json"
SPLIT = ROOT / "configs/data_split.yaml"


class StateEvaluationError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise StateEvaluationError("INPUT_READ_FAILED") from None
    if not isinstance(value, dict):
        raise StateEvaluationError("INPUT_INVALID")
    return value


def _accuracy(correct: int, total: int) -> float | None:
    return correct / total if total else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--annotations", required=True, type=Path)
    parser.add_argument("--samples", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.output.exists():
            raise StateEvaluationError("OUTPUT_EXISTS")
        samples = tuple(
            item.strip() for item in args.samples.split(",") if item.strip()
        )
        if not samples or len(set(samples)) != len(samples):
            raise StateEvaluationError("SAMPLES_INVALID")
        bundle = load_conversations(str(DATA), str(SPLIT))
        sources = SourceRepository(bundle)
        annotations = load_state_annotations(args.annotations)
        if annotations.data_fingerprint != "sha256:" + bundle.snapshot_sha256:
            raise StateEvaluationError("ANNOTATION_DATASET_MISMATCH")
        settings = load_settings(models_profile=PROFILE)
        ports = ArtifactPorts(
            DEFAULT_ARTIFACT_ROOT,
            bundle,
            settings,
            production=True,
            allowed_splits=("main_dev",),
        )
        reports = []
        for sample_id in samples:
            run_dir = args.run_root / sample_id
            plan = _read(run_dir / "plan.json")
            run = _read(run_dir / "run_summary.json")
            manifest = _manifest(plan, bundle.snapshot_sha256)
            prediction_path = (run_dir / "dynamic.predictions.json").resolve()
            receipt = PredictionReceipt(
                prediction_path,
                run["prediction_sha256"]["dynamic"],
                bundle.snapshot_sha256,
                "dynamic",
                tuple(record.query_id for record in manifest.records),
            )
            responses = {
                response.query_id: response
                for response in verify_predictions(
                    prediction_path, manifest, receipt
                )
            }
            artifact = ports.load(sample_id)
            if (
                annotations.memory_fingerprints.get(sample_id)
                != artifact.manifest.memory_set_fingerprint
            ):
                raise StateEvaluationError("ANNOTATION_MEMORY_MISMATCH")
            catalog = MemoryCatalog(
                sample_id,
                artifact.manifest.memory_set_fingerprint,
                "sha256:" + bundle.snapshot_sha256,
                tuple(artifact.memories),
                True,
            )
            mappings = annotations.mappings_by_sample.get(sample_id, ())
            query_keys = sorted(
                (key for key in annotations.gold_by_query if key[0] == sample_id),
                key=lambda key: key[1],
            )
            for key in query_keys:
                query_id = key[1]
                response = responses.get(query_id)
                if response is None:
                    raise StateEvaluationError("ANNOTATED_QUERY_MISSING")
                gold = annotations.gold_by_query[key]
                report = score_state(
                    response, gold, catalog, mappings, sources
                )
                relation = report.relation_label_metrics
                decisions = report.decision_metrics
                effective = report.effective_set_metrics
                relation_ok = (
                    relation.status == "gold_empty"
                    or (
                        relation.end_to_end_accuracy == 1.0
                        and relation.unexpected_predictions == 0
                    )
                )
                decisions_ok = (
                    decisions.status == "gold_empty"
                    or decisions.end_to_end_accuracy == 1.0
                )
                effective_ok = effective.exact_match is True
                reports.append(
                    {
                        "sample_id": sample_id,
                        "query_id": query_id,
                        "state_update_correct": (
                            relation_ok and decisions_ok and effective_ok
                        ),
                        "report": asdict(report),
                    }
                )

        def metric_summary(name: str) -> dict:
            metrics = [
                report["report"][name]
                for report in reports
            ]
            total = sum(item["total_gold_units"] for item in metrics)
            mapped = sum(item["mapped_units"] for item in metrics)
            covered = sum(item["candidate_covered_units"] for item in metrics)
            scored = sum(item["scored_units"] for item in metrics)
            correct = sum(
                unit["correct"] is True
                for item in metrics
                for unit in item["units"]
            )
            unexpected = sum(item["unexpected_predictions"] for item in metrics)
            return {
                "status": "scored" if total else "gold_empty",
                "total_gold_units": total,
                "mapped_units": mapped,
                "candidate_covered_units": covered,
                "scored_units": scored,
                "correct_units": correct,
                "end_to_end_accuracy": _accuracy(correct, total),
                "candidate_conditioned_accuracy": _accuracy(correct, covered),
                "unexpected_predictions": unexpected,
            }

        effective_rows = [
            report["report"]["effective_set_metrics"]
            for report in reports
        ]
        scored_effective = [
            item for item in effective_rows if item["status"] == "scored"
        ]
        exact = sum(item["exact_match"] is True for item in scored_effective)
        f1_values = [
            item["f1"] for item in scored_effective
            if item["f1"] is not None
        ]
        output = {
            "format_version": 1,
            "status": "STAGE6_STATE_SCORE_COMPLETE",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "annotation_file": str(args.annotations),
            "annotation_sha256": hashlib.sha256(
                args.annotations.read_bytes()
            ).hexdigest(),
            "annotation_source": annotations.annotation_source,
            "annotator": annotations.annotator,
            "reviewer": annotations.reviewer,
            "query_count": len(reports),
            "state_update_correct": sum(
                report["state_update_correct"] for report in reports
            ),
            "state_update_accuracy": _accuracy(
                sum(report["state_update_correct"] for report in reports),
                len(reports),
            ),
            "relation_label_metrics": metric_summary(
                "relation_label_metrics"
            ),
            "decision_metrics": metric_summary("decision_metrics"),
            "effective_set_metrics": {
                "scored_queries": len(scored_effective),
                "exact_matches": exact,
                "exact_match_accuracy": _accuracy(
                    exact, len(scored_effective)
                ),
                "macro_f1": (
                    math.fsum(f1_values) / len(f1_values)
                    if f1_values else None
                ),
                "statuses": dict(
                    sorted(
                        (
                            status,
                            sum(item["status"] == status for item in effective_rows),
                        )
                        for status in {
                            item["status"] for item in effective_rows
                        }
                    )
                ),
            },
            "queries": reports,
            "limitations": [
                "team_annotated development subset, not official course Gold",
                "candidate sets come from the frozen main_dev Dynamic run",
                "no LLM or external API call was used for annotation or scoring",
            ],
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(output, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
        print(json.dumps({
            "status": output["status"],
            "output": str(args.output),
            "query_count": output["query_count"],
            "state_update_accuracy": output["state_update_accuracy"],
            "relation_label_accuracy": output[
                "relation_label_metrics"
            ]["end_to_end_accuracy"],
            "decision_accuracy": output[
                "decision_metrics"
            ]["end_to_end_accuracy"],
            "effective_set_exact_accuracy": output[
                "effective_set_metrics"
            ]["exact_match_accuracy"],
        }))
        return 0
    except Exception as exc:
        print(json.dumps({
            "status": getattr(exc, "code", "STAGE6_STATE_EVALUATION_STOPPED"),
            "error_type": type(exc).__name__,
        }))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
