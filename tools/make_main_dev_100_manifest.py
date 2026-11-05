"""Regenerate the deterministic 100-question main_dev validation manifest."""

from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path

from dynamic_memory.data.loader import load_conversations


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/raw/locomo10.json"
SPLIT = ROOT / "configs/data_split.yaml"
OUTPUT = ROOT / "configs/benchmarks/main_dev_100_v1.json"
QUOTAS = {
    "conv-26": 26,
    "conv-30": 14,
    "conv-41": 26,
    "conv-42": 34,
}
FIXED_EXCLUSIONS = {
    sample: {2, 25, 54, 80}
    for sample in QUOTAS
}
SEED = "main-dev-100-v1"


def main() -> int:
    raw = json.loads(DATA.read_text(encoding="utf-8"))
    bundle = load_conversations(str(DATA), str(SPLIT))
    selected = []
    for sample_id, quota in QUOTAS.items():
        sample = next(
            row for row in raw if str(row["sample_id"]) == sample_id
        )
        available: dict[int, list[int]] = defaultdict(list)
        for index, qa in enumerate(sample["qa"]):
            if index not in FIXED_EXCLUSIONS[sample_id]:
                available[int(qa["category"])].append(index)
        counts = {category: len(available[category]) for category in range(1, 6)}
        targets = {
            category: counts[category] * quota / sum(counts.values())
            for category in range(1, 6)
        }
        allocation = {
            category: min(counts[category], math.floor(targets[category]))
            for category in range(1, 6)
        }
        remaining = quota - sum(allocation.values())
        for category in sorted(
            range(1, 6),
            key=lambda item: (-(targets[item] - math.floor(targets[item])), item),
        ):
            if remaining == 0:
                break
            if allocation[category] < counts[category]:
                allocation[category] += 1
                remaining -= 1
        if remaining:
            raise RuntimeError("quota allocation failed")
        for category, count in allocation.items():
            ranked = sorted(
                available[category],
                key=lambda index: hashlib.sha256(
                    f"{SEED}:{sample_id}:{index}".encode("utf-8")
                ).hexdigest(),
            )
            for index in ranked[:count]:
                qa = sample["qa"][index]
                selected.append(
                    {
                        "sample_id": sample_id,
                        "original_qa_index": index,
                        "category": category,
                        "question": qa["question"],
                    }
                )
    selected.sort(
        key=lambda row: (row["sample_id"], row["original_qa_index"])
    )
    document = {
        "format_version": 1,
        "manifest_id": SEED,
        "dataset_sha256": bundle.snapshot_sha256,
        "split": "main_dev",
        "selection_policy": {
            "algorithm": (
                "sample-proportional category quotas; SHA-256 order inside strata"
            ),
            "seed": SEED,
            "excluded_fixed_indices": {
                sample: sorted(indices)
                for sample, indices in FIXED_EXCLUSIONS.items()
            },
            "sample_quotas": QUOTAS,
        },
        "questions": selected,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        json.dumps(
            {
                "output": str(OUTPUT),
                "questions": len(selected),
                "sha256": hashlib.sha256(OUTPUT.read_bytes()).hexdigest(),
                "by_sample": dict(
                    Counter(row["sample_id"] for row in selected)
                ),
                "by_category": dict(
                    sorted(Counter(row["category"] for row in selected).items())
                ),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
