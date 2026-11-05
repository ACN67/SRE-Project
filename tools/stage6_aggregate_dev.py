"""Aggregate independently scored Stage 6 development samples."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
import json
import math
from pathlib import Path


METHODS = ("full_text", "vector_rag", "dynamic")
INPUT_USD_PER_MILLION = Decimal("0.30")
CACHE_HIT_USD_PER_MILLION = Decimal("0.006")
OUTPUT_USD_PER_MILLION = Decimal("1.20")
USD_TO_CNY = Decimal("7.2")


class AggregateError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise AggregateError("INPUT_READ_FAILED") from None
    if not isinstance(value, dict):
        raise AggregateError("INPUT_INVALID")
    return value


def _weighted(rows: list[dict], mean_key: str, weight_key: str) -> float | None:
    values = [
        (row[mean_key], row[weight_key])
        for row in rows
        if row.get(mean_key) is not None and row.get(weight_key, 0) > 0
    ]
    denominator = sum(weight for _, weight in values)
    if denominator == 0:
        return None
    return math.fsum(mean * weight for mean, weight in values) / denominator


def _sum_counts(rows: list[dict], key: str) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in rows:
        value = row.get(key, {})
        if not isinstance(value, dict):
            raise AggregateError("SUMMARY_INVALID")
        counts.update({str(name): int(count) for name, count in value.items()})
    return dict(sorted(counts.items()))


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise AggregateError("CALL_LEDGER_INVALID")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise AggregateError("CALL_LEDGER_INVALID") from None
    if parsed.tzinfo is None:
        raise AggregateError("CALL_LEDGER_INVALID")
    return parsed


def _calls(run_dirs: list[Path]) -> dict:
    total = Counter()
    by_method: dict[str, Counter] = defaultdict(Counter)
    by_sample: dict[str, Counter] = defaultdict(Counter)
    states = Counter()
    latencies: list[float] = []
    input_usd = Decimal(0)
    output_usd = Decimal(0)
    for run_dir in run_dirs:
        sample = run_dir.name
        for path in sorted((run_dir / "budget" / "calls").glob("*.json")):
            row = _read(path)
            state = row.get("state")
            method = row.get("method")
            if method not in METHODS:
                raise AggregateError("CALL_LEDGER_INVALID")
            states[str(state)] += 1
            if state != "completed":
                continue
            response = row.get("response")
            usage = response.get("usage") if isinstance(response, dict) else None
            usage = usage if isinstance(usage, dict) else {}
            prompt = int(usage.get("prompt_tokens") or 0)
            hit = int(usage.get("prompt_cache_hit_tokens") or 0)
            miss = int(usage.get("prompt_cache_miss_tokens") or 0)
            output = int(usage.get("completion_tokens") or 0)
            reasoning = int(usage.get("reasoning_tokens") or 0)
            counters = {
                "calls": 1,
                "prompt_tokens": prompt,
                "cache_hit_tokens": hit,
                "cache_miss_tokens": miss,
                "completion_tokens": output,
                "reasoning_tokens": reasoning,
            }
            total.update(counters)
            by_method[method].update(counters)
            by_sample[sample].update(counters)
            if hit or miss:
                input_usd += (
                    Decimal(hit) * CACHE_HIT_USD_PER_MILLION
                    + Decimal(miss) * INPUT_USD_PER_MILLION
                ) / Decimal(1_000_000)
            else:
                input_usd += (
                    Decimal(prompt) * INPUT_USD_PER_MILLION
                ) / Decimal(1_000_000)
            output_usd += (
                Decimal(output) * OUTPUT_USD_PER_MILLION
            ) / Decimal(1_000_000)
            started = _parse_time(row.get("started_at"))
            finished = _parse_time(row.get("finished_at"))
            latencies.append((finished - started).total_seconds())
    usd = input_usd + output_usd
    total["mean_latency_seconds"] = (
        math.fsum(latencies) / len(latencies) if latencies else None
    )
    total["max_latency_seconds"] = max(latencies) if latencies else None
    total["estimated_input_usd"] = str(input_usd.quantize(Decimal("0.0000001")))
    total["estimated_output_usd"] = str(output_usd.quantize(Decimal("0.0000001")))
    total["estimated_total_usd"] = str(usd.quantize(Decimal("0.0000001")))
    total["estimated_total_cny"] = str(
        (usd * USD_TO_CNY).quantize(Decimal("0.0001"))
    )
    return {
        "call_states": dict(sorted(states.items())),
        "total": dict(sorted(total.items())),
        "by_method": {
            method: dict(sorted(by_method[method].items()))
            for method in METHODS
        },
        "by_sample": {
            sample: dict(sorted(by_sample[sample].items()))
            for sample in sorted(by_sample)
        },
        "pricing": {
            "input_miss_usd_per_million": str(INPUT_USD_PER_MILLION),
            "input_hit_usd_per_million": str(CACHE_HIT_USD_PER_MILLION),
            "output_usd_per_million": str(OUTPUT_USD_PER_MILLION),
            "usd_to_cny": str(USD_TO_CNY),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-root", required=True, type=Path)
    parser.add_argument("--scored-root", required=True, type=Path)
    parser.add_argument("--samples", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        samples = tuple(item.strip() for item in args.samples.split(",") if item.strip())
        if not samples or len(samples) != len(set(samples)):
            raise AggregateError("SAMPLES_INVALID")
        run_dirs = [args.batch_root / sample for sample in samples]
        if args.output.exists():
            raise AggregateError("OUTPUT_EXISTS")
        plans = [_read(path / "plan.json") for path in run_dirs]
        runs = [_read(path / "run_summary.json") for path in run_dirs]
        scores = [
            _read(args.scored_root / sample / "stage6_dev_summary.json")
            for sample in samples
        ]
        question_hashes = {
            plan.get("identity", {}).get("questions_file_sha256") for plan in plans
        }
        question_modes = {
            plan.get("identity", {}).get("question_mode") for plan in plans
        }
        if len(question_modes) != 1:
            raise AggregateError("QUESTION_MANIFEST_MISMATCH")
        if question_modes == {"external"} and (
            len(question_hashes) != 1 or None in question_hashes
        ):
            raise AggregateError("QUESTION_MANIFEST_MISMATCH")
        if question_modes == {"full"} and question_hashes != {None}:
            raise AggregateError("QUESTION_MANIFEST_MISMATCH")
        if question_modes not in ({"external"}, {"full"}):
            raise AggregateError("QUESTION_MODE_INVALID")
        question_count = sum(
            int(plan["identity"]["question_count"]) for plan in plans
        )
        if any(
            plan.get("status") != "STAGE6_DEV_THREE_METHOD_PLAN_READY"
            or plan.get("identity", {}).get("split") != "main_dev"
            for plan in plans
        ):
            raise AggregateError("PLAN_INVALID")
        if any(
            row.get("status") != "STAGE6_DEV_THREE_METHOD_LIVE_COMPLETE"
            for row in runs
        ):
            raise AggregateError("RUN_INVALID")
        if any(
            row.get("status") != "STAGE6_DEV_SCORE_COMPLETE"
            or row.get("sample_id") != sample
            for row, sample in zip(scores, samples)
        ):
            raise AggregateError("SCORE_INVALID")

        qa_rows: dict[str, list[dict]] = {
            method: [row["qa"][method]["summary"] for row in scores]
            for method in METHODS
        }
        qa = {}
        for method in METHODS:
            rows = qa_rows[method]
            categories = []
            for category in ("1", "2", "3", "4", "5"):
                group_rows = [
                    next(
                        (
                            item
                            for item in row["by_category"]
                            if item.get("key") == category
                        ),
                        {
                            "main_mean": None,
                            "gold_scored": 0,
                            "text_f1_main_mean": None,
                            "text_f1_scored": 0,
                            "total_predictions": 0,
                        },
                    )
                    for row in rows
                ]
                categories.append(
                    {
                        "key": category,
                        "total_predictions": sum(
                            item.get("total_predictions", 0) for item in group_rows
                        ),
                        "gold_scored": sum(
                            item.get("gold_scored", 0) for item in group_rows
                        ),
                        "main_mean": _weighted(
                            group_rows, "main_mean", "gold_scored"
                        ),
                        "text_f1_main_mean": _weighted(
                            group_rows, "text_f1_main_mean", "text_f1_scored"
                        ),
                    }
                )
            qa[method] = {
                "total_predictions": sum(row["total_predictions"] for row in rows),
                "gold_scored": sum(row["gold_scored"] for row in rows),
                "gold_missing": sum(row["gold_missing"] for row in rows),
                "execution_failed": sum(row["execution_failed"] for row in rows),
                "main_mean": _weighted(rows, "main_mean", "gold_scored"),
                "conditional_mean": _weighted(
                    rows, "conditional_mean", "conditional_scored"
                ),
                "text_f1_main_mean": _weighted(
                    rows, "text_f1_main_mean", "text_f1_scored"
                ),
                "by_category": categories,
                "answer_status_counts": {
                    status: sum(
                        int(run["answer_status_counts"][method][status])
                        for run in runs
                    )
                    for status in (
                        "answered",
                        "no_evidence",
                        "time_uncertain",
                        "unresolved_conflict",
                        "error",
                    )
                },
            }

        evidence = {}
        for method in METHODS:
            by_k = []
            for k in (5, 10):
                rows = [
                    next(
                        item
                        for item in score["evidence_recall"][method]["by_method"]
                        if item["k"] == k
                    )
                    for score in scores
                ]
                by_k.append(
                    {
                        "k": k,
                        "scored_questions": sum(
                            row["scored_questions"] for row in rows
                        ),
                        "total_questions": sum(
                            row["total_questions"] for row in rows
                        ),
                        "main_mean": _weighted(
                            rows, "main_mean", "scored_questions"
                        ),
                    }
                )
            evidence[method] = by_k

        output = {
            "format_version": 1,
            "status": "STAGE6_DEV_AGGREGATE_COMPLETE",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "question_count": question_count,
            "questions_file_sha256": next(iter(question_hashes)),
            "question_mode": next(iter(question_modes)),
            "samples": list(samples),
            "run_fingerprints": {
                sample: plan["run_fingerprint"]
                for sample, plan in zip(samples, plans)
            },
            "prediction_sha256": {
                sample: run["prediction_sha256"]
                for sample, run in zip(samples, runs)
            },
            "qa": qa,
            "evidence_recall": evidence,
            "calls": _calls(run_dirs),
            "state_metrics": [
                score["state_metrics"] for score in scores
            ],
            "gold_access": "after_each_sample_prediction_sha_verified",
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(output, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
        print(json.dumps({
            "status": output["status"],
            "output": str(args.output),
            "question_count": question_count,
        }))
        return 0
    except Exception as exc:
        print(json.dumps({
            "status": getattr(exc, "code", "STAGE6_AGGREGATE_STOPPED"),
            "error_type": type(exc).__name__,
        }))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
