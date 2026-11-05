"""Remap reviewed state annotations to a new artifact and score saved predictions.

The remap uses exact source overlap, local index vectors and lexical evidence.
No provider or network call is made. Gold is read only after prediction files
have passed their byte-bound verification.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re

from dynamic_memory.contracts import (
    AnswerResult,
    CandidateItem,
    CandidateSet,
    QueryRequest,
    QueryResponse,
)
from dynamic_memory.data.loader import load_conversations
from dynamic_memory.data.repository import SourceRepository
from dynamic_memory.integration.artifacts import ArtifactPorts
from dynamic_memory.pipeline.settings import load_settings
from dynamic_memory.reasoning.auto_resolver import DeterministicStateResolver
from evaluation.metrics.state_annotations import load_state_annotations
from evaluation.metrics.state_metrics import MemoryCatalog, score_state
from evaluation.predictions import PredictionReceipt, verify_predictions
from evaluation.question_manifest import QuestionManifest, load_question_manifest
from scripts.stage6_dev_three_method import PROFILE


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/raw/locomo10.json"
SPLIT = ROOT / "configs/data_split.yaml"
SAMPLES = ("conv-30", "conv-41", "conv-42")
_STOP = frozenset(
    "the a an to at in on of and or is was are were be been being have has "
    "had i my me we our you your he she his her they their it this that "
    "these those for from with by into out as so very really some all up "
    "down about now then".split()
)
_ALIASES = {
    "biz": "business",
    "startin": "start",
    "starting": "start",
    "started": "start",
    "lose": "loss",
    "lost": "loss",
    "losing": "loss",
    "writes": "write",
    "writing": "write",
    "written": "write",
    "plans": "plan",
    "planned": "plan",
    "planning": "plan",
    "volunteered": "volunteer",
    "volunteering": "volunteer",
    "recommended": "recommend",
    "recommendation": "recommend",
}


def _read(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected object: {path}")
    return value


def _stem(word: str) -> str:
    word = _ALIASES.get(word, word)
    if len(word) > 5 and word.endswith("ing"):
        word = word[:-3]
    elif len(word) > 4 and word.endswith("ed"):
        word = word[:-2]
    elif len(word) > 3 and word.endswith("s"):
        word = word[:-1]
    return word


def _terms(text: str) -> set[str]:
    return {
        _stem(word)
        for word in re.findall(r"[a-z0-9]+", text.casefold())
        if word not in _STOP
    }


def _content(text: str) -> str:
    return re.sub(r"^\s*[^:]{1,80}:\s*", "", text).strip().casefold()


def _speaker(text: str) -> str | None:
    match = re.match(r"^\s*([A-Z][a-z]+)\s*:", text)
    return match.group(1).casefold() if match else None


def _load_memories(artifact_root: Path, sample_id: str) -> dict[str, dict]:
    current = _read(artifact_root / sample_id / "current.json")
    build_id = (
        current.get("build_id")
        or current.get("current_build_id")
        or current["build"]["build_id"]
    )
    document = json.loads(
        (
            artifact_root
            / sample_id
            / "builds"
            / build_id
            / "memories.json"
        ).read_text(encoding="utf-8")
    )
    rows = document["memories"] if "memories" in document else document
    return {row["memory_id"]: row for row in rows}


def _load_vectors(index_root: Path, sample_id: str) -> dict[str, tuple[float, ...]]:
    payload = _read(index_root / f"{sample_id}.json")["payload"]
    return dict(zip(payload["memory_ids"], payload["vectors"], strict=True))


def _cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    return math.fsum(a * b for a, b in zip(left, right, strict=True))


def _alignment_score(
    item: dict,
    old_memory: dict,
    new_memory: dict,
    old_vector: tuple[float, ...],
    new_vector: tuple[float, ...],
) -> tuple[float, dict]:
    old_text = _content(old_memory["content"])
    new_text = _content(new_memory["content"])
    old_terms, new_terms = _terms(old_text), _terms(new_text)
    value_terms = _terms(item["value"])
    semantic = _cosine(old_vector, new_vector)
    lexical = len(old_terms & new_terms) / max(1, len(old_terms | new_terms))
    value_coverage = (
        len(value_terms & new_terms) / len(value_terms)
        if value_terms
        else 0.0
    )
    old_speaker = _speaker(old_memory["content"])
    new_speaker = _speaker(new_memory["content"])
    speaker_penalty = (
        0.35
        if old_speaker and new_speaker and old_speaker != new_speaker
        else 0.0
    )
    visual_penalty = (
        0.45
        if re.match(r"^(?:a|an) (?:photo|photography|picture|image)\b", new_text)
        and not re.search(r"\b(?:photo|picture|image)\b", old_text)
        else 0.0
    )
    score = (
        0.60 * semantic
        + 0.25 * lexical
        + 0.15 * value_coverage
        - speaker_penalty
        - visual_penalty
    )
    return score, {
        "semantic": semantic,
        "lexical": lexical,
        "value_coverage": value_coverage,
        "speaker_penalty": speaker_penalty,
        "visual_penalty": visual_penalty,
    }


def _align_sample(
    sample_id: str,
    annotation: dict,
    old_root: Path,
    old_index_root: Path,
    new_root: Path,
    new_index_root: Path,
    manual_map: dict[str, str] | None = None,
    manual_drop: set[str] | None = None,
) -> tuple[dict[str, str], list[dict], tuple[str, ...]]:
    old = _load_memories(old_root, sample_id)
    new = _load_memories(new_root, sample_id)
    old_vectors = _load_vectors(old_index_root, sample_id)
    new_vectors = _load_vectors(new_index_root, sample_id)
    by_source: dict[tuple[str, str], set[str]] = {}
    for memory in new.values():
        for ref in memory["source_refs"]:
            by_source.setdefault(
                (ref["session_id"], ref["dia_id"]),
                set(),
            ).add(memory["memory_id"])
    manual_map = manual_map or {}
    manual_drop = manual_drop or set()
    items = [
        item for item in annotation["items"]
        if item["sample_id"] == sample_id
        and item["gold_item_id"] not in manual_drop
    ]
    candidates = {}
    unmapped_initial = set()
    for item in items:
        ids = set()
        for ref in item["source_refs"]:
            ids.update(
                by_source.get((ref["session_id"], ref["dia_id"]), ())
            )
        if not ids:
            unmapped_initial.add(item["gold_item_id"])
            continue
        candidates[item["gold_item_id"]] = ids

    unused = set().union(*candidates.values())
    remaining = set(candidates)
    selected: dict[str, str] = {}
    diagnostics = []
    for item in items:
        item_id = item["gold_item_id"]
        memory_id = manual_map.get(item_id)
        if memory_id is None:
            continue
        if memory_id not in candidates[item_id]:
            raise ValueError(
                f"manual mapping has no source overlap: {item_id}"
            )
        if memory_id in set(selected.values()):
            raise ValueError(f"manual mapping reuses Memory: {memory_id}")
        selected[item_id] = memory_id
        diagnostics.append(
            {
                "sample_id": sample_id,
                "gold_item_id": item_id,
                "memory_id": memory_id,
                "score": None,
                "manual": True,
            }
        )
        remaining.remove(item_id)
        unused.remove(memory_id)
    while remaining:
        options = []
        for item in items:
            item_id = item["gold_item_id"]
            if item_id not in remaining:
                continue
            old_id = item["matched_memory_ids"][0]
            for new_id in candidates[item_id] & unused:
                score, detail = _alignment_score(
                    item,
                    old[old_id],
                    new[new_id],
                    old_vectors[old_id],
                    new_vectors[new_id],
                )
                options.append((score, item_id, new_id, detail))
        if not options:
            break
        score, item_id, new_id, detail = max(options)
        selected[item_id] = new_id
        diagnostics.append(
            {
                "sample_id": sample_id,
                "gold_item_id": item_id,
                "memory_id": new_id,
                "score": round(score, 6),
                **{key: round(value, 6) for key, value in detail.items()},
            }
        )
        remaining.remove(item_id)
        unused.remove(new_id)
    return (
        selected,
        sorted(
            diagnostics,
            key=lambda row: (
                row["score"] is None,
                row["score"] if row["score"] is not None else 0,
            ),
        ),
        tuple(
            sorted(
                set(remaining)
                | unmapped_initial
                | (manual_drop & {item["gold_item_id"] for item in annotation["items"] if item["sample_id"] == sample_id})
            )
        ),
    )


def _metric_summary(
    reports: list[dict],
    name: str,
) -> dict:
    metrics = [report["report"][name] for report in reports]
    total = sum(item["total_gold_units"] for item in metrics)
    scored = sum(item["scored_units"] for item in metrics)
    covered = sum(item["candidate_covered_units"] for item in metrics)
    correct = sum(
        unit["correct"] is True
        for item in metrics
        for unit in item["units"]
    )
    return {
        "total_gold_units": total,
        "candidate_covered_units": covered,
        "scored_units": scored,
        "correct_units": correct,
        "end_to_end_accuracy": correct / total if total else None,
        "candidate_conditioned_accuracy": correct / covered if covered else None,
        "unexpected_predictions": sum(
            item["unexpected_predictions"] for item in metrics
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--annotation", type=Path, required=True)
    parser.add_argument("--old-artifact-root", type=Path, required=True)
    parser.add_argument("--old-index-root", type=Path, required=True)
    parser.add_argument("--new-artifact-root", type=Path, required=True)
    parser.add_argument("--new-index-root", type=Path, required=True)
    parser.add_argument("--prediction-root", type=Path, required=True)
    parser.add_argument("--old-prediction-root", type=Path, required=True)
    parser.add_argument("--mapped-annotation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manual-map", type=Path)
    parser.add_argument(
        "--recompute-resolutions",
        action="store_true",
        help="replay the current deterministic resolver against saved candidate IDs",
    )
    args = parser.parse_args()
    if args.mapped_annotation.exists() or args.output.exists():
        raise SystemExit("outputs must not already exist")

    base_document = _read(args.annotation)
    base = load_state_annotations(args.annotation)
    manual = (
        _read(args.manual_map)
        if args.manual_map is not None
        else {}
    )
    manual_ids = manual.get("map", {})
    manual_drop = set(manual.get("drop", ()))
    if (
        not isinstance(manual_ids, dict)
        or not all(
            isinstance(item_id, str) and isinstance(memory_id, str)
            for item_id, memory_id in manual_ids.items()
        )
        or not all(isinstance(item_id, str) for item_id in manual_drop)
    ):
        raise SystemExit("manual mapping file is invalid")
    bundle = load_conversations(str(DATA), str(SPLIT))
    sources = SourceRepository(bundle)
    settings = load_settings(models_profile=PROFILE)
    ports = ArtifactPorts(
        args.new_artifact_root,
        bundle,
        settings,
        production=True,
        allowed_splits=("main_dev",),
    )
    all_manifest = load_question_manifest(
        DATA,
        SPLIT,
        selected_splits=("main_dev",),
    )

    mapped_ids = {}
    diagnostics = []
    unmapped = {}
    fingerprints = {}
    artifacts = {}
    for sample_id in SAMPLES:
        ids, sample_diagnostics, sample_unmapped = _align_sample(
            sample_id,
            base_document,
            args.old_artifact_root,
            args.old_index_root,
            args.new_artifact_root,
            args.new_index_root,
            manual_ids,
            manual_drop,
        )
        mapped_ids.update(ids)
        diagnostics.extend(sample_diagnostics)
        unmapped[sample_id] = sample_unmapped
        artifact = ports.load(sample_id)
        artifacts[sample_id] = artifact
        fingerprints[sample_id] = artifact.manifest.memory_set_fingerprint

    dropped_ids = set().union(*map(set, unmapped.values()))
    mapped_items = []
    for item in base_document["items"]:
        item_id = item["gold_item_id"]
        if item["sample_id"] not in SAMPLES or item_id in dropped_ids:
            continue
        mapped = dict(item)
        mapped["matched_memory_ids"] = [mapped_ids[item_id]]
        mapped["match_basis"] = (
            "Exact source overlap plus local BGE vector and lexical alignment; "
            "ACN67 reviewed the generated mapping."
        )
        mapped["review_note"] = (
            "Remapped from the frozen reviewed Memory to the deterministic "
            "source-grounded Memory with the same cited turn."
        )
        mapped_items.append(mapped)
    mapped_queries = []
    for source_query in base_document["queries"]:
        if source_query["sample_id"] not in SAMPLES:
            continue
        query = dict(source_query)
        query["decisions"] = [
            row for row in source_query["decisions"]
            if row["item_id"] not in dropped_ids
        ]
        if source_query["effective_item_ids"] is not None:
            query["effective_item_ids"] = [
                item_id
                for item_id in source_query["effective_item_ids"]
                if item_id not in dropped_ids
            ]
        relations = []
        for row in source_query["relations"]:
            if (
                row["old_item_id"] in dropped_ids
                or row["new_item_id"] in dropped_ids
            ):
                continue
            normalized = dict(row)
            normalized["scope_attribute"] = "state_transition"
            relations.append(normalized)
        query["relations"] = relations
        mapped_queries.append(query)
    mapped_document = {
        "format_version": 1,
        "annotation_source": "team_annotated",
        "annotator": "ACN67",
        "reviewer": "ACN67",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "protocol": "main-dev-state-auto-remap-v2",
        "data_fingerprint": base.data_fingerprint,
        "memory_fingerprints": fingerprints,
        "items": mapped_items,
        "queries": mapped_queries,
    }
    args.mapped_annotation.parent.mkdir(parents=True, exist_ok=True)
    args.mapped_annotation.write_text(
        json.dumps(
            mapped_document,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    mapped_annotations = load_state_annotations(args.mapped_annotation)

    aligned_reports = []
    end_to_end_reports = []
    prediction_audit = {}
    old_to_item = {
        item["matched_memory_ids"][0]: item["gold_item_id"]
        for item in base_document["items"]
        if item["sample_id"] in SAMPLES
    }

    def score_responses(
        *,
        sample_id: str,
        response_by_id: dict[str, QueryResponse],
        catalog: MemoryCatalog,
        mappings,
    ) -> list[dict]:
        rows = []
        for key in sorted(
            (
                key for key in mapped_annotations.gold_by_query
                if key[0] == sample_id
            ),
            key=lambda item: item[1],
        ):
            response = response_by_id.get(key[1])
            if response is None:
                raise ValueError(f"missing prediction: {key[1]}")
            report = score_state(
                response,
                mapped_annotations.gold_by_query[key],
                catalog,
                mappings,
                sources,
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
            rows.append(
                {
                    "sample_id": sample_id,
                    "query_id": key[1],
                    "state_update_correct": (
                        relation_ok
                        and decisions_ok
                        and effective.exact_match is True
                    ),
                    "report": asdict(report),
                }
            )
        return rows

    for sample_id in SAMPLES:
        manifest = QuestionManifest(
            all_manifest.snapshot_sha256,
            tuple(
                record for record in all_manifest.records
                if record.sample_id == sample_id
            ),
            ("main_dev",),
            all_manifest.official_id_field,
        )
        new_path = (
            args.prediction_root
            / sample_id
            / "dynamic.predictions.json"
        ).resolve()
        new_digest = hashlib.sha256(new_path.read_bytes()).hexdigest()
        new_receipt = PredictionReceipt(
            new_path,
            new_digest,
            bundle.snapshot_sha256,
            "dynamic",
            tuple(record.query_id for record in manifest.records),
        )
        new_responses = verify_predictions(
            new_path,
            manifest,
            new_receipt,
        )
        new_response_by_id = {
            row.query_id: row for row in new_responses
        }
        artifact = artifacts[sample_id]
        catalog = MemoryCatalog(
            sample_id,
            artifact.manifest.memory_set_fingerprint,
            "sha256:" + bundle.snapshot_sha256,
            tuple(artifact.memories),
            True,
        )
        mappings = mapped_annotations.mappings_by_sample[sample_id]
        catalog_ids = {memory.memory_id for memory in artifact.memories}
        new_memories = {
            memory.memory_id: memory
            for memory in artifact.memories
        }
        sample = sources.get_sample(sample_id)
        timestamps = [
            turn.recorded_at
            for session in sample.sessions
            for turn in session.turns
            if turn.recorded_at is not None
        ]
        cutoff = max(timestamps) if timestamps else None
        if args.recompute_resolutions:
            resolver = DeterministicStateResolver(
                conversation_cutoff=lambda _sample_id, value=cutoff: value
            )
            for query_id, response in tuple(new_response_by_id.items()):
                if response.retrieved is None or response.query_analysis is None:
                    continue
                try:
                    memories = [
                        new_memories[item.memory_id]
                        for item in sorted(
                            response.retrieved.items,
                            key=lambda item: item.rank,
                        )
                    ]
                except KeyError:
                    raise ValueError(
                        f"prediction references unknown Memory: {query_id}"
                    ) from None
                request = QueryRequest(
                    query_id=response.query_id,
                    sample_id=response.sample_id,
                    question=response.question,
                )
                resolution = resolver.resolve(
                    request,
                    response.query_analysis,
                    memories,
                )
                new_response_by_id[query_id] = response.model_copy(
                    update={"resolution": resolution}
                )
        new_predictions_use_catalog = all(
            response.retrieved is None
            or all(
                item.memory_id in catalog_ids
                for item in response.retrieved.items
            )
            for response in new_responses
        )
        if new_predictions_use_catalog:
            end_to_end_reports.extend(
                score_responses(
                    sample_id=sample_id,
                    response_by_id=new_response_by_id,
                    catalog=catalog,
                    mappings=mappings,
                )
            )

        old_path = (
            args.old_prediction_root
            / sample_id
            / "dynamic.predictions.json"
        ).resolve()
        old_digest = hashlib.sha256(old_path.read_bytes()).hexdigest()
        old_receipt = PredictionReceipt(
            old_path,
            old_digest,
            bundle.snapshot_sha256,
            "dynamic",
            tuple(record.query_id for record in manifest.records),
        )
        old_responses = verify_predictions(
            old_path,
            manifest,
            old_receipt,
        )
        resolver = DeterministicStateResolver(
            conversation_cutoff=lambda _sample_id, value=cutoff: value
        )
        aligned_by_id = {}
        for old_response in old_responses:
            if (
                old_response.resolution is None
                or old_response.retrieved is None
                or old_response.query_analysis is None
            ):
                continue
            candidates = []
            memories = []
            seen = set()
            for old_item in old_response.retrieved.items:
                gold_item_id = old_to_item.get(old_item.memory_id)
                new_memory_id = mapped_ids.get(gold_item_id)
                if new_memory_id is None or new_memory_id in seen:
                    continue
                seen.add(new_memory_id)
                memory = new_memories[new_memory_id]
                candidates.append(
                    CandidateItem(
                        memory_id=new_memory_id,
                        rank=len(candidates) + 1,
                        score=old_item.score,
                        source_refs=memory.source_refs,
                        origin=old_item.origin,
                    )
                )
                memories.append(memory)
            relocated = CandidateSet(
                query_id=old_response.query_id,
                sample_id=old_response.sample_id,
                base_top_k=old_response.retrieved.base_top_k,
                max_candidates=old_response.retrieved.max_candidates,
                retriever_fingerprint=(
                    old_response.retrieved.retriever_fingerprint
                ),
                items=candidates,
            )
            request = QueryRequest(
                query_id=old_response.query_id,
                sample_id=old_response.sample_id,
                question=old_response.question,
                as_of=None,
            )
            resolution = resolver.resolve(
                request,
                old_response.query_analysis,
                memories,
            )
            aligned_by_id[old_response.query_id] = QueryResponse(
                query_id=old_response.query_id,
                sample_id=old_response.sample_id,
                question=old_response.question,
                method="dynamic",
                query_analysis=old_response.query_analysis,
                retrieved=relocated,
                resolution=resolution,
                answer_result=AnswerResult(
                    status="no_evidence",
                    uncertainty=(
                        "Offline state audit; answer generation was not run."
                    ),
                ),
            )
        aligned_reports.extend(
            score_responses(
                sample_id=sample_id,
                response_by_id=aligned_by_id,
                catalog=catalog,
                mappings=mappings,
            )
        )
        prediction_audit[sample_id] = {
            "new_prediction_sha256": new_digest,
            "old_prediction_sha256": old_digest,
            "query_count": len(manifest.records),
            "new_predictions_use_catalog": (
                new_predictions_use_catalog
            ),
        }

    def summarized(reports: list[dict]) -> dict:
        effective_rows = [
            report["report"]["effective_set_metrics"]
            for report in reports
            if report["report"]["effective_set_metrics"]["status"] == "scored"
        ]
        f1_values = [
            row["f1"] for row in effective_rows if row["f1"] is not None
        ]
        return {
            "state_update_accuracy": (
                sum(row["state_update_correct"] for row in reports)
                / len(reports)
                if reports
                else None
            ),
            "relation_label_metrics": _metric_summary(
                reports,
                "relation_label_metrics",
            ),
            "decision_metrics": _metric_summary(
                reports,
                "decision_metrics",
            ),
            "effective_set_metrics": {
                "scored_queries": len(effective_rows),
                "exact_match_accuracy": (
                    sum(row["exact_match"] is True for row in effective_rows)
                    / len(effective_rows)
                    if effective_rows
                    else None
                ),
                "macro_f1": (
                    math.fsum(f1_values) / len(f1_values)
                    if f1_values
                    else None
                ),
            },
        }

    reports = end_to_end_reports or aligned_reports
    primary = summarized(reports)
    output = {
        "format_version": 1,
        "status": "AUTO_STATE_AUDIT_COMPLETE",
        "annotation_source": "team_annotated",
        "state_update_accuracy_scope": "legacy_annotation_candidate_universe",
        "network_calls": 0,
        "query_count": len(reports),
        **primary,
        "prediction_audit": prediction_audit,
        "mapping_coverage": {
            "source_items": len(
                [item for item in base_document["items"] if item["sample_id"] in SAMPLES]
            ),
            "mapped_items": len(mapped_items),
            "unmapped_item_ids": sorted(dropped_ids),
        },
        "alignment_diagnostics": diagnostics,
        "queries": reports,
        "end_to_end_new_retrieval": {
            "status": (
                "scored"
                if end_to_end_reports
                else "unavailable_prediction_catalog_mismatch"
            ),
            **summarized(end_to_end_reports),
            "queries": end_to_end_reports,
        },
        "legacy_candidate_alignment": {
            **summarized(aligned_reports),
            "queries": aligned_reports,
        },
        "limitations": [
            "team-reviewed development subset, not official course state Gold",
            "mapping uses exact source overlap, local vectors, and lexical evidence",
            "primary state metrics use the new end-to-end retrieval when its candidate IDs match the audited artifact",
            "legacy_candidate_alignment replaces candidate IDs in the frozen reviewed candidate sets and is retained only for comparability",
            "no provider or network call is used for mapping or scoring",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2)
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": output["status"],
                "query_count": output["query_count"],
                "state_update_accuracy": output["state_update_accuracy"],
                "relation_accuracy": output[
                    "relation_label_metrics"
                ]["end_to_end_accuracy"],
                "decision_accuracy": output[
                    "decision_metrics"
                ]["end_to_end_accuracy"],
                "effective_exact": output[
                    "effective_set_metrics"
                ]["exact_match_accuracy"],
                "lowest_alignments": diagnostics[:10],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
