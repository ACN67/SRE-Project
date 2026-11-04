"""Evaluate the fixed Stage 4 non-D predictions against frozen LoCoMo Gold."""

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
from evaluation.question_manifest import (
    QuestionManifest, make_question_record,
)

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/raw/locomo10.json"
SPLIT = ROOT / "configs/data_split.yaml"
METHODS = ("full_text", "vector_rag", "pure_vector_topk")


class Stage5Error(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _read(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise Stage5Error("INPUT_READ_FAILED") from None


def _write_new(path: Path, value: object) -> None:
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
    except OSError:
        raise Stage5Error("OUTPUT_WRITE_FAILED") from None


def _manifest(plan: dict, snapshot_sha256: str) -> QuestionManifest:
    rows = plan.get("identity", {}).get("questions")
    if not isinstance(rows, list) or len(rows) != 4:
        raise Stage5Error("PLAN_QUESTIONS_INVALID")
    records = []
    for row in rows:
        try:
            record = make_question_record(
                snapshot_sha256=snapshot_sha256,
                sample_id=row["sample_id"],
                original_qa_index=row["original_qa_index"],
                question=row["question"],
                split="main_dev",
                query_id=row["query_id"],
            )
        except Exception:
            raise Stage5Error("PLAN_QUESTIONS_INVALID") from None
        if record.gold_record_key != row["gold_record_key"]:
            raise Stage5Error("PLAN_QUESTIONS_INVALID")
        records.append(record)
    return QuestionManifest(
        snapshot_sha256=snapshot_sha256,
        records=tuple(records),
        selected_splits=("main_dev",),
        official_id_field=None,
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
        results_root = (ROOT / "results").resolve()
        if not output_dir.is_relative_to(results_root):
            raise Stage5Error("OUTPUT_DIRECTORY_UNSAFE")
        if output_dir.exists() and any(output_dir.iterdir()):
            raise Stage5Error("OUTPUT_DIRECTORY_IN_USE")

        plan = _read(run_dir / "plan.json")
        summary = _read(run_dir / "run_summary.json")
        if (
            not isinstance(plan, dict)
            or plan.get("status") != "STAGE4_PLAN_READY"
            or not isinstance(summary, dict)
            or summary.get("status") != "STAGE4_LIVE_COMPLETE"
            or summary.get("network_calls") != 12
        ):
            raise Stage5Error("STAGE4_RUN_INVALID")

        bundle = load_conversations(str(args.snapshot), str(args.split))
        manifest = _manifest(plan, bundle.snapshot_sha256)
        query_ids = tuple(record.query_id for record in manifest.records)
        prediction_hashes = summary.get("prediction_sha256")
        if not isinstance(prediction_hashes, dict) or set(prediction_hashes) != set(METHODS):
            raise Stage5Error("PREDICTION_IDENTITY_INVALID")

        receipts = {}
        verified = {}
        for method in METHODS:
            path = run_dir / f"{method}.predictions.json"
            if hashlib.sha256(path.read_bytes()).hexdigest() != prediction_hashes[method]:
                raise Stage5Error("PREDICTION_HASH_MISMATCH")
            receipt = PredictionReceipt(
                path=path,
                file_sha256=prediction_hashes[method],
                snapshot_sha256=bundle.snapshot_sha256,
                method=method,
                query_ids=query_ids,
            )
            verified[method] = verify_predictions(path, manifest, receipt)
            receipts[method] = receipt

        # Gold is opened only after every prediction file has passed the
        # byte-bound verification loop above.
        sources = SourceRepository(bundle)
        output_dir.mkdir(parents=True, exist_ok=True)
        qa_summaries = {}
        evidence_summaries = {}
        for method in METHODS:
            report = evaluate_saved_qa(
                receipts[method].path,
                manifest,
                receipts[method],
                args.snapshot,
                args.split,
                annotation_source="course_gold",
            )
            method_dir = output_dir / method
            method_dir.mkdir()
            scoring = save_qa_scoring_manifest(
                method_dir / "qa_scoring_manifest.json",
                receipts[method].path,
                manifest,
                receipts[method],
                report,
                annotation_source="course_gold",
            )
            evidence = evaluate_saved_evidence(
                receipts[method].path,
                manifest,
                receipts[method],
                args.snapshot,
                args.split,
                sources,
                annotation_source="course_gold",
            )
            evidence_path = method_dir / "evidence_recall.json"
            _write_new(evidence_path, asdict(evidence))
            qa_summaries[method] = {
                "scoring_manifest": str(scoring.path),
                "scoring_sha256": scoring.file_sha256,
                "qa_summary": asdict(report.summary),
            }
            evidence_summaries[method] = {
                "path": str(evidence_path),
                "by_method": [asdict(item) for item in evidence.by_method],
            }

        result = {
            "format_version": 1,
            "status": "STAGE5_NON_D_SCORE_COMPLETE",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "run_fingerprint": plan["run_fingerprint"],
            "prediction_sha256": prediction_hashes,
            "qa": qa_summaries,
            "evidence_recall": evidence_summaries,
            "state_metrics": {
                "status": "NOT_TESTED",
                "blocker": "BLOCKED_BY_D",
            },
            "gold_access": "after_all_predictions_sha_verified",
        }
        _write_new(output_dir / "stage5_summary.json", result)
        print(json.dumps({
            "status": result["status"],
            "output_dir": str(output_dir),
            "run_fingerprint": result["run_fingerprint"],
            "methods": list(METHODS),
            "state_metrics": result["state_metrics"],
        }))
        return 0
    except Exception as exc:
        code = getattr(exc, "code", None)
        print(json.dumps({
            "status": code if isinstance(code, str) else "STAGE5_STOPPED",
            "error_type": type(exc).__name__,
        }))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
