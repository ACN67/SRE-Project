"""Build the reviewed main_dev state subset from frozen predictions."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from dynamic_memory.contracts import SourceRef
from dynamic_memory.data.loader import load_conversations
from dynamic_memory.integration.artifacts import ArtifactPorts
from dynamic_memory.pipeline.settings import load_settings


ARTIFACT_ROOT = Path(
    r"C:\Users\Keshu\Documents\SRE-Project-RuntimeAssets\semantic_v3_artifacts"
)
PROFILE = Path("configs/models_benchmark.yaml")
DATA = Path("data/raw/locomo10.json")
SPLIT = Path("configs/data_split.yaml")

EFFECTIVE = {
    ("conv-30", "qa_conv-30_65"): (
        "mem_1c79f12bffd23b7ab74c",
    ),
    ("conv-30", "qa_conv-30_70"): (
        "mem_7f739d06f808d228ad09",
        "mem_4a0f5947cd3e787bf8e1",
    ),
    ("conv-30", "qa_conv-30_99"): (),
    ("conv-30", "qa_conv-30_103"): (),
    ("conv-41", "qa_conv-41_101"): (
        "mem_f3e6561f6dcb0b2524e8",
        "mem_67d6219800159a265a1a",
    ),
    ("conv-42", "qa_conv-42_85"): (
        "mem_f6d95f9fc061e3a45a51",
        "mem_5815d06383b342ffdb33",
        "mem_857471311540f75a2a6b",
        "mem_a1bab3fbd45a09747f94",
        "mem_ad2c884542e3d661c77f",
        "mem_dc8987be00c264cb18ea",
        "mem_910865a14697bca9d4f5",
        "mem_20d3efd7b4146c145814",
    ),
    ("conv-42", "qa_conv-42_136"): (
        "mem_d2ea055972464d00a6e8",
        "mem_18dafbf0362c705992a5",
        "mem_5792833cf9c6f54835df",
    ),
    ("conv-42", "qa_conv-42_183"): (
        "mem_2b036d4559b946c5832b",
        "mem_e22a697a6cb7b3de4a79",
    ),
    ("conv-42", "qa_conv-42_187"): (
        "mem_a8a1b4af19978f3af7b2",
    ),
    ("conv-42", "qa_conv-42_188"): (
        "mem_cf95f023a77227dbd524",
    ),
    ("conv-42", "qa_conv-42_249"): (),
}

# Relations are manually reviewed. A plan followed by its evidenced
# implementation is UPDATE; no conflicting edge is inferred from chronology
# alone.
RELATIONS = {
    ("conv-30", "qa_conv-30_70"): (
        (
            "mem_61742247d21d3dfe1588",
            "mem_a41873fe4ad414240f04",
            "business opening",
            "UPDATE",
        ),
        (
            "mem_a41873fe4ad414240f04",
            "mem_39f2c09a8571f19bc7a8",
            "current work",
            "UPDATE",
        ),
        (
            "mem_39f2c09a8571f19bc7a8",
            "mem_4a0f5947cd3e787bf8e1",
            "current work",
            "UPDATE",
        ),
    ),
    ("conv-41", "qa_conv-41_101"): (
        (
            "mem_58c127c57e277e1c6071",
            "mem_f3e6561f6dcb0b2524e8",
            "Maria's shelter fundraiser preparation",
            "UPDATE",
        ),
    ),
    ("conv-42", "qa_conv-42_136"): (
        (
            "mem_d2ea055972464d00a6e8",
            "mem_18dafbf0362c705992a5",
            "video game tournament wins",
            "UPDATE",
        ),
        (
            "mem_18dafbf0362c705992a5",
            "mem_5792833cf9c6f54835df",
            "video game tournament wins",
            "UPDATE",
        ),
    ),
}


def _gold_item_id(memory_id: str) -> str:
    return "gold_" + memory_id.removeprefix("mem_")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("output already exists")
    bundle = load_conversations(str(DATA), str(SPLIT))
    raw = json.loads(DATA.read_text(encoding="utf-8"))
    question_text = {
        (str(sample["sample_id"]), f"qa_{sample['sample_id']}_{index}"): qa["question"]
        for sample in raw
        for index, qa in enumerate(sample["qa"])
    }
    settings = load_settings(models_profile=PROFILE)
    ports = ArtifactPorts(
        ARTIFACT_ROOT,
        bundle,
        settings,
        production=True,
        allowed_splits=("main_dev",),
    )
    artifacts = {
        sample: ports.load(sample)
        for sample in ("conv-30", "conv-41", "conv-42")
    }
    memories = {
        sample: {memory.memory_id: memory for memory in artifact.memories}
        for sample, artifact in artifacts.items()
    }

    mappings_by_sample = {}
    item_id_by_memory = {}
    item_rows = []
    for sample_id in ("conv-30", "conv-41", "conv-42"):
        predictions = json.loads(
            (args.run_root / sample_id / "dynamic.predictions.json").read_text(
                encoding="utf-8"
            )
        )["predictions"]
        by_query = {
            row["response"]["query_id"]: row["response"] for row in predictions
        }
        sample_items = []
        for (gold_sample, query_id), effective_ids in EFFECTIVE.items():
            if gold_sample != sample_id:
                continue
            response = by_query[query_id]
            for candidate in response["retrieved"]["items"]:
                memory_id = candidate["memory_id"]
                memory = memories[sample_id][memory_id]
                if not memory.entity or not memory.attribute or not memory.value:
                    continue
                item_id = _gold_item_id(memory_id)
                previous = item_id_by_memory.get((sample_id, memory_id))
                if previous is not None:
                    if previous != item_id:
                        raise ValueError("gold item collision")
                    continue
                row = {
                    "gold_item_id": item_id,
                    "sample_id": sample_id,
                    "source_refs": [
                        ref.model_dump(mode="json") for ref in memory.source_refs
                    ],
                    "entity": memory.entity,
                    "attribute": memory.attribute,
                    "value": memory.value,
                    "matched_memory_ids": [memory_id],
                    "match_basis": (
                        "ACN67 manual review of the frozen Memory content and "
                        "source turn against the query-scoped state annotation"
                    ),
                    "match_status": "matched",
                    "review_note": (
                        "Matched by stable Memory ID and exact source references."
                    ),
                }
                item_rows.append(row)
                sample_items.append(row)
                item_id_by_memory[(sample_id, memory_id)] = item_id
        mappings_by_sample[sample_id] = sample_items

    queries = []
    for (sample_id, query_id), effective_ids in EFFECTIVE.items():
        response = next(
            row["response"]
            for row in json.loads(
                (
                    args.run_root
                    / sample_id
                    / "dynamic.predictions.json"
                ).read_text(encoding="utf-8")
            )["predictions"]
            if row["response"]["query_id"] == query_id
        )
        mapped_ids = {
            memory_id: _gold_item_id(memory_id)
            for memory_id in (
                candidate["memory_id"]
                for candidate in response["retrieved"]["items"]
            )
            if (sample_id, memory_id) in item_id_by_memory
        }
        effective_item_ids = [
            mapped_ids[memory_id] for memory_id in effective_ids
        ]
        decisions = [
            {
                "item_id": mapped_ids[memory_id],
                "label": (
                    "RETAIN" if memory_id in effective_ids else "EXCLUDE"
                ),
            }
            for memory_id in mapped_ids
        ]
        relations = [
            {
                "old_item_id": mapped_ids[old_memory_id],
                "new_item_id": mapped_ids[new_memory_id],
                "scope_attribute": scope_attribute,
                "label": label,
            }
            for old_memory_id, new_memory_id, scope_attribute, label
            in RELATIONS.get((sample_id, query_id), ())
        ]
        queries.append(
            {
                "sample_id": sample_id,
                "query_id": query_id,
                "relations": relations,
                "decisions": decisions,
                "effective_item_ids": effective_item_ids,
                "relation_universe_closed": False,
                "review_note": (
                    "ACN67 reviewed the question, retrieved candidates, source "
                    "turns and time scope. " + question_text[(sample_id, query_id)]
                ),
            }
        )

    document = {
        "format_version": 1,
        "annotation_source": "team_annotated",
        "annotator": "ACN67",
        "reviewer": "ACN67",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "protocol": "main-dev-state-review-v1",
        "data_fingerprint": "sha256:" + bundle.snapshot_sha256,
        "memory_fingerprints": {
            sample: artifact.manifest.memory_set_fingerprint
            for sample, artifact in artifacts.items()
        },
        "items": item_rows,
        "queries": queries,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(document, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
    print(json.dumps({
        "status": "STATE_ANNOTATION_BUILT",
        "path": str(args.output),
        "queries": len(queries),
        "items": len(item_rows),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
