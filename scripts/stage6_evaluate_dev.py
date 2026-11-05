"""Score frozen Stage 6 development predictions after Gold-isolation checks."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from dynamic_memory.data.loader import load_conversations
from dynamic_memory.data.repository import SourceRepository
from evaluation.metrics.evidence_recall import evaluate_saved_evidence
from evaluation.metrics.qa_scoring_manifest import save_qa_scoring_manifest
from evaluation.metrics.qa_summary import evaluate_saved_qa
from evaluation.predictions import PredictionReceipt, verify_predictions
from evaluation.question_manifest import QuestionManifest, make_question_record


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/raw/locomo10.json"
SPLIT = ROOT / "configs/data_split.yaml"


class EvaluationError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _read(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise EvaluationError("INPUT_READ_FAILED") from None


def _write_new(path: Path, value: object) -> None:
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
    except OSError:
        raise EvaluationError("OUTPUT_WRITE_FAILED") from None


def _manifest(plan: dict, snapshot_sha256: str) -> QuestionManifest:
    identity = plan.get("identity")
    rows = identity.get("questions") if isinstance(identity, dict) else None
    if not isinstance(rows, list) or not rows:
        raise EvaluationError("PLAN_QUESTIONS_INVALID")
    split = identity.get("split")
    if split not in ("main_dev", "test"):
        raise EvaluationError("PLAN_SPLIT_INVALID")
    records = []
    for row in rows:
        try:
            record = make_question_record(
                snapshot_sha256=snapshot_sha256,
                sample_id=identity["sample_id"],
                original_qa_index=row["original_qa_index"],
                question=row["question"],
                split=split,
                query_id=row["query_id"],
            )
        except Exception:
            raise EvaluationError("PLAN_QUESTIONS_INVALID") from None
        if record.gold_record_key != row["gold_record_key"]:
            raise EvaluationError("PLAN_QUESTIONS_INVALID")
        records.append(record)
    return QuestionManifest(
        snapshot_sha256,
        tuple(records),
        (split,),
        None,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, default=DATA)
    parser.add_argument("--split", type=Path, default=SPLIT)
    args = parser.parse_args()
    try:
        run_dir = args.run_dir.resolve()
        output_dir = args.output_dir.resolve()
        if not output_dir.is_relative_to((ROOT / "results").resolve()):
            raise EvaluationError("OUTPUT_DIRECTORY_UNSAFE")
        if output_dir.exists() and any(output_dir.iterdir()):
            raise EvaluationError("OUTPUT_DIRECTORY_IN_USE")
        plan = _read(run_dir / "plan.json")
        summary = _read(run_dir / "run_summary.json")
        if (
            not isinstance(plan, dict)
            or plan.get("status") != "STAGE6_DEV_THREE_METHOD_PLAN_READY"
            or not isinstance(summary, dict)
            or summary.get("status")
            != "STAGE6_DEV_THREE_METHOD_LIVE_COMPLETE"
        ):
            raise EvaluationError("RUN_INVALID")
        methods = tuple(plan["identity"]["methods"])
        if methods != ("full_text", "vector_rag", "dynamic"):
            raise EvaluationError("METHODS_INVALID")
        prediction_hashes = summary.get("prediction_sha256")
        if (
            not isinstance(prediction_hashes, dict)
            or set(prediction_hashes) != set(methods)
        ):
            raise EvaluationError("PREDICTION_IDENTITY_INVALID")

        bundle = load_conversations(str(args.snapshot), str(args.split))
        manifest = _manifest(plan, bundle.snapshot_sha256)
        query_ids = tuple(record.query_id for record in manifest.records)
        receipts = {}
        verified = {}
        for method in methods:
            path = run_dir / f"{method}.predictions.json"
            if hashlib.sha256(path.read_bytes()).hexdigest() != prediction_hashes[method]:
                raise EvaluationError("PREDICTION_HASH_MISMATCH")
            receipt = PredictionReceipt(
                path,
                prediction_hashes[method],
                bundle.snapshot_sha256,
                method,
                query_ids,
            )
            verified[method] = verify_predictions(path, manifest, receipt)
            receipts[method] = receipt

        # Gold remains unopened until every prediction file above passed its
        # byte-bound summary hash and structural replay.
        if any(
            vector.retrieved != dynamic.retrieved
            for vector, dynamic in zip(
                verified["vector_rag"],
                verified["dynamic"],
            )
        ):
            raise EvaluationError("CANDIDATE_SET_MISMATCH")
        output_dir.mkdir(parents=True, exist_ok=True)
        sources = SourceRepository(bundle)
        qa = {}
        evidence = {}
        for method in methods:
            method_dir = output_dir / method
            method_dir.mkdir()
            report = evaluate_saved_qa(
                receipts[method].path,
                manifest,
                receipts[method],
                args.snapshot,
                args.split,
                annotation_source="course_gold",
            )
            scoring = save_qa_scoring_manifest(
                method_dir / "qa_scoring_manifest.json",
                receipts[method].path,
                manifest,
                receipts[method],
                report,
                annotation_source="course_gold",
            )
            recall = evaluate_saved_evidence(
                receipts[method].path,
                manifest,
                receipts[method],
                args.snapshot,
                args.split,
                sources,
                annotation_source="course_gold",
            )
            recall_path = method_dir / "evidence_recall.json"
            _write_new(recall_path, asdict(recall))
            qa[method] = {
                "scoring_manifest": str(scoring.path),
                "scoring_sha256": scoring.file_sha256,
                "summary": asdict(report.summary),
            }
            evidence[method] = {
                "path": str(recall_path),
                "by_method": [asdict(item) for item in recall.by_method],
            }
        state_metrics = {
            "status": "NOT_EVALUATED",
            "reason": "NO_STATE_GOLD",
            "decision_count": sum(
                len(row.resolution.decisions)
                for row in verified["dynamic"]
                if row.resolution is not None
            ),
        }
        result = {
            "format_version": 1,
            "status": "STAGE6_DEV_SCORE_COMPLETE",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "sample_id": plan["identity"]["sample_id"],
            "run_fingerprint": plan["run_fingerprint"],
            "prediction_sha256": prediction_hashes,
            "qa": qa,
            "evidence_recall": evidence,
            "state_metrics": state_metrics,
            "gold_access": "after_all_predictions_sha_verified",
        }
        _write_new(output_dir / "stage6_dev_summary.json", result)
        print(json.dumps({
            "status": result["status"],
            "sample_id": result["sample_id"],
            "output_dir": str(output_dir),
            "run_fingerprint": result["run_fingerprint"],
            "state_metrics": state_metrics,
        }))
        return 0
    except Exception as exc:
        code = getattr(exc, "code", None)
        print(json.dumps({
            "status": code if isinstance(code, str) else "STAGE6_EVALUATION_STOPPED",
            "error_type": type(exc).__name__,
        }))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
