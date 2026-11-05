"""Publish one completed Stage 6 extraction and semantic review.

The command performs no network model calls. It replays every extraction
checkpoint, verifies the immutable review queue and semantic receipts, rebuilds
the reviewed Memory set, publishes a strict ReviewedMemoryArtifact and
optionally builds and reloads the local BGE index.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from dynamic_memory.contracts import (
    MemoryBuildResult,
    QueryRequest,
    WindowDiagnostic,
    memory_set_fingerprint,
)
from dynamic_memory.data.loader import load_conversations
from dynamic_memory.data.repository import SourceRepository
from dynamic_memory.integration.artifacts import ArtifactPorts
from dynamic_memory.memory.artifact_store import (
    ArtifactStoreError,
    ReviewAttestation,
    WindowReview,
)
from dynamic_memory.memory.checkpoints import WindowCheckpointStore
from dynamic_memory.memory.normalization import (
    merge_reviewed_memories,
    normalize_memories,
)
from dynamic_memory.memory.review_workflow import (
    ReviewQueue,
    _fp,
    verify_review_files,
)
from dynamic_memory.memory.windows import plan_extraction_windows
from dynamic_memory.pipeline.settings import load_settings
from dynamic_memory.retrieval import (
    IndexManager,
    LocalIndexEncoder,
    VectorRetriever,
    analyze_query,
    save_index,
)
from dynamic_memory.retrieval.embedding import EmbeddingConfig
from scripts.stage_b_repair_runner import EXTRACTOR_VERSION


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/raw/locomo10.json"
SPLIT = ROOT / "configs/data_split.yaml"
PROFILE = ROOT / "configs/models_high_pilot.yaml"


class PublicationError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _sha(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        raise PublicationError("PUBLICATION_INPUT_UNREADABLE") from None


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise PublicationError("PUBLICATION_INPUT_UNREADABLE") from None


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )


def _safe_new_summary_dir(path: Path) -> Path:
    target = path.resolve()
    if not target.is_relative_to((ROOT / "results").resolve()):
        raise PublicationError("SUMMARY_DIRECTORY_UNSAFE")
    if target.exists() and any(target.iterdir()):
        raise PublicationError("SUMMARY_DIRECTORY_IN_USE")
    return target


def _publish(
    *,
    sample_id: str,
    state_dir: Path,
    review_dir: Path,
    artifact_root: Path,
    index_root: Path | None,
    model_root: Path | None,
    model_revision: str | None,
    allowed_splits: tuple[str, ...],
) -> dict:
    state = state_dir.resolve()
    review = review_dir.resolve()
    artifact_root = artifact_root.resolve()
    if artifact_root.is_relative_to(state) or artifact_root.is_relative_to(ROOT):
        raise PublicationError("ARTIFACT_DIRECTORY_UNSAFE")
    if index_root is not None:
        index_root = index_root.resolve()
        if index_root.is_relative_to(state) or index_root.is_relative_to(ROOT):
            raise PublicationError("INDEX_DIRECTORY_UNSAFE")
    if model_root is not None:
        model_root = model_root.resolve()
        if not model_root.is_dir():
            raise PublicationError("MODEL_DIRECTORY_UNAVAILABLE")

    queue_path = state / "private_review/queue.json"
    manifest_path = state / "manifest.json"
    repair_plan_path = state / "repair_plan.json"
    try:
        queue = ReviewQueue.model_validate_json(queue_path.read_bytes())
    except Exception:
        raise PublicationError("REVIEW_QUEUE_INVALID") from None
    if queue.sample_id != sample_id:
        raise PublicationError("SAMPLE_MISMATCH")
    manifest = _read_json(manifest_path)
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"body", "digest"}
        or not isinstance(manifest["body"], dict)
        or manifest["digest"] != _fp(manifest["body"])
        or _fp(manifest) != queue.run_fingerprint
    ):
        raise PublicationError("EXTRACTION_LINEAGE_INVALID")
    if repair_plan_path.exists():
        repair_plan = _read_json(repair_plan_path)
        if (
            not isinstance(repair_plan, dict)
            or repair_plan.get("status") != "STAGE6_REPAIR_PLAN_READY"
            or repair_plan.get("sample_id") != sample_id
        ):
            raise PublicationError("REPAIR_PLAN_INVALID")
        original_plan_path = Path(repair_plan["original_plan_path"])
    else:
        original_plan_path = state / "plan.json"
    original_plan = _read_json(original_plan_path)
    if (
        not isinstance(original_plan, dict)
        or original_plan.get("status") != "STAGE6_EXTRACTION_PLAN_READY"
        or original_plan.get("sample_id") != sample_id
        or original_plan.get("run_fingerprint")
        != manifest["body"].get("run_fingerprint")
    ):
        raise PublicationError("EXTRACTION_PLAN_INVALID")

    semantic_approval = review / "semantic_review_approval.json"
    semantic_authority = review / "semantic_review_authority.json"
    auto_approval = review / "auto_review_approval.json"
    auto_authority = review / "auto_review_authority.json"
    if semantic_approval.exists() and semantic_authority.exists():
        approval_path, authority_path = semantic_approval, semantic_authority
    elif auto_approval.exists() and auto_authority.exists():
        approval_path, authority_path = auto_approval, auto_authority
    else:
        raise PublicationError("AUTO_OR_SEMANTIC_REVIEW_REQUIRED")
    try:
        verified = verify_review_files(
            queue,
            approval_path,
            authority_path,
            run_kind="real",
        )
    except Exception:
        raise PublicationError("SEMANTIC_REVIEW_INVALID") from None

    settings = load_settings(models_profile=PROFILE)
    bundle = load_conversations(str(DATA), str(SPLIT))
    repository = SourceRepository(bundle)
    sample = repository.get_sample(sample_id)
    planned = tuple(
        plan_extraction_windows(
            sample,
            settings.extraction,
            reserved_prompt_tokens=1800,
        )
    )
    if (
        len(planned) != len(queue.windows)
        or len(planned) != manifest["body"].get("window_count")
        or manifest["body"].get("sample_id") != sample_id
        or manifest["body"].get("dataset_sha256") != bundle.snapshot_sha256
    ):
        raise PublicationError("WINDOW_PLAN_DRIFT")
    extractor_version = manifest["body"].get(
        "extractor_version",
        EXTRACTOR_VERSION,
    )
    if not isinstance(extractor_version, str) or not extractor_version.strip():
        raise PublicationError("EXTRACTION_LINEAGE_INVALID")

    checkpoints = WindowCheckpointStore(
        state / "checkpoints",
        run_fingerprint=manifest["body"]["run_fingerprint"],
    )
    reviewed_memories = []
    diagnostics = []
    audits = []
    for window, queued in zip(planned, queue.windows, strict=True):
        if (
            queued.window_id != window.extraction_window_id
            or queued.source_refs_fingerprint
            != _fp([ref.model_dump(mode="json") for ref in window.source_refs])
        ):
            raise PublicationError("REVIEW_QUEUE_STALE")
        result = checkpoints.load(window, queued.call_fingerprint)
        if result is None:
            raise PublicationError("CHECKPOINT_MISSING")
        selected = verified.filter_for_window(window.extraction_window_id)(result)
        normalized = normalize_memories(
            sample_id,
            [selected],
            repository,
            extractor_version=extractor_version,
            evidence_checker=verified.checker_for_window(
                window.extraction_window_id
            ),
        )
        reviewed_memories.extend(normalized)
        diagnostics.append(
            WindowDiagnostic(
                window.extraction_window_id,
                window.source_refs,
                "success",
                None,
                None,
                len(result.facts),
                len(selected.facts),
                len(result.facts) - len(selected.facts),
                len(normalized),
                "review",
            )
        )
        audits.append({
            "window_id": window.extraction_window_id,
            "call_fingerprint": queued.call_fingerprint,
            "checkpoint_sha256": "sha256:" + _sha(
                state / "checkpoints" / f"{window.extraction_window_id}.json"
            ),
            "input_fact_count": len(result.facts),
            "approved_fact_count": len(selected.facts),
        })

    merged = tuple(merge_reviewed_memories(reviewed_memories))
    report = MemoryBuildResult(
        sample_id,
        merged,
        "complete",
        False,
        len(planned),
        len(planned),
        tuple(diagnostics),
        extractor_version,
    )
    receipt = ReviewAttestation(
        run_kind="real",
        reviewer_id=verified.approval.reviewer_id,
        execution_policy_fingerprint=original_plan[
            "execution_policy_fingerprint"
        ],
        reviewed_memory_set_fingerprint=memory_set_fingerprint(merged),
        review_log_fingerprint=verified.approval_fingerprint,
        review_authority_fingerprint=_fp(
            verified.authority.model_dump(mode="json")
        ),
        extraction_lineage_fingerprint=None,
        windows=tuple(
            WindowReview(
                extraction_window_id=diagnostic.extraction_window_id,
                source_refs_fingerprint=_fp([
                    ref.model_dump(mode="json")
                    for ref in diagnostic.source_refs
                ]),
                approved_fact_count=diagnostic.accepted_fact_count,
                excluded_fact_count=diagnostic.rejected_fact_count,
                decision="approved",
            )
            for diagnostic in diagnostics
        ),
    )

    ports = ArtifactPorts(
        artifact_root,
        bundle,
        settings,
        production=True,
        allowed_splits=allowed_splits,
    )
    store = ports.store(sample_id)
    try:
        existing = store.load()
    except ArtifactStoreError as exc:
        if exc.code != "BUILD_NOT_FOUND":
            raise
        existing = None
    if existing is None:
        artifact = store.publish_reviewed(
            report,
            receipt,
            verified_review=verified,
        )
    else:
        provenance = existing.manifest.production_provenance
        if (
            existing.manifest.memory_set_fingerprint
            != receipt.reviewed_memory_set_fingerprint
            or provenance.review_fingerprint
            != _fp(receipt.model_dump(mode="json"))
            or provenance.reviewer_id != receipt.reviewer_id
        ):
            raise PublicationError("PUBLISHED_BUILD_CONFLICT")
        artifact = existing
    checked = ports.load(sample_id)
    if checked.source_manifest_fingerprint != artifact.source_manifest_fingerprint:
        raise PublicationError("ARTIFACT_READBACK_FAILED")
    ports.record_verified(checked)

    output = {
        "status": "PUBLISHED",
        "network_calls": 0,
        "sample_id": sample_id,
        "build_id": checked.manifest.build_id,
        "memory_count": len(checked.memories),
        "memory_set_fingerprint": memory_set_fingerprint(checked.memories),
        "approved_facts": sum(
            item.accepted_fact_count for item in diagnostics
        ),
        "excluded_facts": sum(
            item.rejected_fact_count for item in diagnostics
        ),
        "windows": len(diagnostics),
        "extractor_version": extractor_version,
        "queue_fingerprint": queue.fingerprint,
        "manifest_sha256": "sha256:" + _sha(manifest_path),
        "queue_sha256": "sha256:" + _sha(queue_path),
        "review_fingerprint": verified.approval_fingerprint,
    }

    if index_root is not None:
        if model_root is None or not model_revision:
            raise PublicationError("MODEL_IDENTITY_REQUIRED")
        encoder = LocalIndexEncoder(
            EmbeddingConfig(
                str(model_root),
                model_revision,
                query_prompt=(
                    "Represent this sentence for searching relevant passages: "
                ),
                document_prompt="",
            )
        )
        retrieval = settings.retrieval.model_copy(
            update={"embedding_model": encoder.identity.model_name}
        )
        index = IndexManager(encoder).build_index(checked, retrieval)
        index_path = index_root / f"{sample_id}.json"
        index_root.mkdir(parents=True, exist_ok=True)
        if index_path.exists():
            loaded = IndexManager(encoder).load_index(
                index_path,
                checked,
                retrieval,
            )
            if loaded.index_fingerprint != index.index_fingerprint:
                raise PublicationError("INDEX_CONFLICT")
        else:
            save_index(index, index_path)
            loaded = IndexManager(encoder).load_index(
                index_path,
                checked,
                retrieval,
            )
        request = QueryRequest(
            query_id="conv26-publication-smoke",
            sample_id=sample_id,
            question="What important events and changes happened in this conversation?",
        )
        snapshot = VectorRetriever(encoder, retrieval).retrieve_with_trace(
            request,
            analyze_query(request),
            loaded,
        )
        valid_ids = {memory.memory_id for memory in checked.memories}
        if any(
            candidate.memory_id not in valid_ids
            for candidate in snapshot.candidates.items
        ):
            raise PublicationError("INDEX_SOURCE_MISMATCH")
        output.update(
            index_path=str(index_path),
            index_fingerprint=loaded.index_fingerprint,
            candidate_count=len(snapshot.candidates.items),
            embedding_model=encoder.identity.model_name,
            embedding_revision=model_revision,
        )
    output["checkpoint_audit"] = audits
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", required=True)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--review-dir", required=True, type=Path)
    parser.add_argument("--artifact-root", required=True, type=Path)
    parser.add_argument("--index-root", type=Path)
    parser.add_argument("--model-root", type=Path)
    parser.add_argument("--model-revision")
    parser.add_argument(
        "--allowed-split",
        action="append",
        choices=("main_dev", "test"),
        default=None,
    )
    parser.add_argument("--summary-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        summary_dir = _safe_new_summary_dir(args.summary_dir)
        output = _publish(
            sample_id=args.sample,
            state_dir=args.state_dir,
            review_dir=args.review_dir,
            artifact_root=args.artifact_root,
            index_root=args.index_root,
            model_root=args.model_root,
            model_revision=args.model_revision,
            allowed_splits=tuple(args.allowed_split or ("main_dev",)),
        )
        summary_dir.mkdir(parents=True, exist_ok=True)
        _write_json(summary_dir / "publication_summary.json", output)
        _write_json(
            summary_dir / "lineage_audit.json",
            {
                "format_version": 1,
                "sample_id": output["sample_id"],
                "build_id": output["build_id"],
                "queue_fingerprint": output["queue_fingerprint"],
                "manifest_sha256": output["manifest_sha256"],
                "queue_sha256": output["queue_sha256"],
                "checkpoint_audit": output["checkpoint_audit"],
            },
        )
        printable = {
            key: value
            for key, value in output.items()
            if key != "checkpoint_audit"
        }
        printable["summary_dir"] = str(summary_dir)
        print(json.dumps(printable, ensure_ascii=False))
        return 0
    except Exception as exc:
        code = getattr(exc, "code", None)
        print(json.dumps({
            "status": code if isinstance(code, str) else "STAGE6_PUBLISH_FAILED",
            "network_calls": 0,
        }))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
