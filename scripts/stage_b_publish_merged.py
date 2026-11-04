"""Read-only replay, then optional publication of a frozen 59+4 real batch.

The default command only verifies lineage and builds reviewed Memory in memory.
No extraction request is ever made. --publish writes a new immutable artifact;
--index additionally builds a local BGE index from that artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from dynamic_memory.contracts import MemoryBuildResult, QueryRequest, WindowDiagnostic, memory_set_fingerprint
from dynamic_memory.memory.artifact_store import ArtifactStoreError, ReviewAttestation, WindowReview
from dynamic_memory.memory.batch_budget import BatchApproval, BatchBudgetLedger
from dynamic_memory.memory.checkpoints import WindowCheckpointStore
from dynamic_memory.memory.extraction import MEMORY_DRAFT_PROMPT
from dynamic_memory.memory.normalization import merge_reviewed_memories, normalize_memories
from dynamic_memory.memory.repair_lineage import FAILED_WINDOW_IDS, OLD_AUTH, OLD_RUN, OLD_ROOT, verify_original_batch
from dynamic_memory.memory.review_workflow import ReviewQueue, _fp, make_review_queue, verify_review_files
from dynamic_memory.integration.artifacts import ArtifactPorts
from dynamic_memory.retrieval import IndexManager, LocalIndexEncoder, VectorRetriever, analyze_query, save_index
from dynamic_memory.retrieval.embedding import EmbeddingConfig
from scripts.stage_b_repair_runner import (
    EXTRACTOR_VERSION, MODEL_REVISION, MODEL_ROOT, _budget_root, _registry_root, prepare_plan,
)


class MergedPublicationError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(f"{code}: merged publication unavailable")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def replay_merged(*, state_dir: Path, repair_approval_path: Path,
                  review_file: Path, authority_file: Path,
                  expected_manifest_sha256: str, expected_queue_sha256: str):
    """Validate paid lineage and independently rebuild the approved memory set."""
    try:
        root = state_dir.resolve()
        if (review_file.resolve().is_relative_to(root) or
                authority_file.resolve().is_relative_to(root) or
                repair_approval_path.resolve().is_relative_to(root)):
            raise ValueError("approval inside run state")
        plan = prepare_plan(mode="repair4")  # offline: client cannot send
        old_results, old_rows, baseline = verify_original_batch(plan)
        manifest_path = root / "merged" / "manifest.json"
        queue_path = root / "merged" / "queue.json"
        if (_sha(manifest_path) != expected_manifest_sha256.lower() or
                _sha(queue_path) != expected_queue_sha256.lower()):
            raise ValueError("frozen file hash")
        manifest = json.loads(manifest_path.read_bytes())
        body = manifest["body"]
        if (manifest["digest"] != _fp(body) or body["format_version"] != 1 or
                body["sample_id"] != plan.sample_id or
                body["dataset_sha256"] != plan.bundle.snapshot_sha256 or
                body["plan_fingerprint"] != plan.plan_fingerprint or
                body["split"] != "main_dev" or len(body["windows"]) != 63):
            raise ValueError("manifest identity")
        raw_approval = repair_approval_path.read_bytes()
        repair_approval = BatchApproval.model_validate_json(raw_approval)
        if (repair_approval.mode != "repair4" or repair_approval.sample_id != plan.sample_id or
                repair_approval.dataset_sha256 != plan.bundle.snapshot_sha256 or
                repair_approval.plan_fingerprint != plan.plan_fingerprint or
                set(repair_approval.selected_window_ids) != FAILED_WINDOW_IDS or
                body["origin_runs"] != [OLD_RUN, repair_approval.run_fingerprint]):
            raise ValueError("repair approval")
        ledger = BatchBudgetLedger(_budget_root(plan, root, repair_approval),
            repair_approval, "sha256:" + hashlib.sha256(raw_approval).hexdigest(),
            _registry_root(plan, root))
        rows = ledger.window_rows()
        if (len(rows) != 4 or {row["window_id"] for row in rows} != FAILED_WINDOW_IDS or
                any(row["state"] != "extracted" for row in rows)):
            raise ValueError("repair ledger")
        new_rows = {row["window_id"]: row for row in rows}
        new_store = WindowCheckpointStore(root / "checkpoints",
            run_fingerprint=repair_approval.run_fingerprint)
        results = dict(old_results)
        for window in plan.selected:
            row = new_rows[window.extraction_window_id]
            result = new_store.load(window, row["call_fingerprint"])
            if result is None:
                raise ValueError("missing repaired checkpoint")
            results[window.extraction_window_id] = result
        if len(results) != 63:
            raise ValueError("missing results")
        manifest_rows = body["windows"]
        call_fps = {}
        for window, item in zip(plan.windows, manifest_rows):
            wid = window.extraction_window_id
            repaired = wid in FAILED_WINDOW_IDS
            row = new_rows[wid] if repaired else old_rows[wid]
            cp = (root / "checkpoints" if repaired else OLD_ROOT / "checkpoints") / (wid + ".json")
            result = results[wid]
            checks = {
                "window_id": item["window_id"] == wid,
                "origin_run": item["origin_run_fingerprint"] == (repair_approval.run_fingerprint if repaired else OLD_RUN),
                "authorization": item["authorization_id"] == (repair_approval.authorization_id if repaired else OLD_AUTH),
                "approval": item["approval_sha256"] == (_sha(repair_approval_path) if repaired else baseline["approval_sha256"]),
                "call": item["call_fingerprint"] == row["call_fingerprint"],
                "checkpoint": item["checkpoint_sha256"] == _sha(cp),
                "input": item["input_sha256"] == hashlib.sha256(window.input_text.encode()).hexdigest(),
                "source_refs": item["source_refs_fingerprint"] == _fp([ref.model_dump(mode="json") for ref in window.source_refs]),
                "prompt": (item["prompt_fingerprint"] == result.template_fingerprint ==
                           (MEMORY_DRAFT_PROMPT.template_fingerprint if repaired else baseline["old_prompt_fingerprint"])),
            }
            if not all(checks.values()):
                raise ValueError("window lineage: " + wid + ":" + ",".join(k for k, v in checks.items() if not v))
            call_fps[wid] = row["call_fingerprint"]
        queue = ReviewQueue.model_validate_json(queue_path.read_bytes())
        rebuilt_queue = make_review_queue(run_fingerprint=_fp(manifest), sample_id=plan.sample_id,
            windows=plan.windows, call_fingerprints=call_fps, results=results,
            repository=plan.repository)
        if queue != rebuilt_queue:
            raise ValueError("queue drift")
        verified = verify_review_files(queue, review_file, authority_file, run_kind="real")
        diagnostics = []
        memories = []
        for window in plan.windows:
            wid = window.extraction_window_id
            result = results[wid]
            selected = verified.filter_for_window(wid)(result)
            normalized = normalize_memories(plan.sample_id, [selected], plan.repository,
                extractor_version=EXTRACTOR_VERSION,
                evidence_checker=verified.checker_for_window(wid))
            memories.extend(normalized)
            diagnostics.append(WindowDiagnostic(wid, window.source_refs, "success", None, None,
                len(result.facts), len(selected.facts), len(result.facts) - len(selected.facts),
                len(normalized), "review"))
        merged = tuple(merge_reviewed_memories(memories))
        report = MemoryBuildResult(plan.sample_id, merged, "complete", False, 63, 63,
            tuple(diagnostics), EXTRACTOR_VERSION)
        receipt = ReviewAttestation(run_kind="real", reviewer_id=verified.approval.reviewer_id,
            execution_policy_fingerprint=plan.runtime.execution_policy_fingerprint,
            reviewed_memory_set_fingerprint=memory_set_fingerprint(merged),
            review_log_fingerprint=verified.approval_fingerprint,
            review_authority_fingerprint=_fp(verified.authority.model_dump(mode="json")),
            extraction_lineage_fingerprint="sha256:" + _sha(manifest_path),
            windows=tuple(WindowReview(extraction_window_id=d.extraction_window_id,
                source_refs_fingerprint=_fp([r.model_dump(mode="json") for r in d.source_refs]),
                approved_fact_count=d.accepted_fact_count,
                excluded_fact_count=d.rejected_fact_count, decision="approved") for d in diagnostics))
        return plan, report, receipt, verified, manifest
    except MergedPublicationError:
        raise
    except Exception:
        raise MergedPublicationError("MERGED_REPLAY_REJECTED") from None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--repair-approval", type=Path, required=True)
    parser.add_argument("--review-file", type=Path, required=True)
    parser.add_argument("--authority-file", type=Path, required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--expected-queue-sha256", required=True)
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--index-dir", type=Path)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--index", action="store_true")
    args = parser.parse_args()
    try:
        if args.index and not args.publish:
            raise MergedPublicationError("PUBLISH_REQUIRED_FOR_INDEX")
        if args.index and args.index_dir is None:
            raise MergedPublicationError("INDEX_DIR_REQUIRED")
        plan, report, receipt, verified, manifest = replay_merged(
            state_dir=args.state_dir, repair_approval_path=args.repair_approval,
            review_file=args.review_file, authority_file=args.authority_file,
            expected_manifest_sha256=args.expected_manifest_sha256,
            expected_queue_sha256=args.expected_queue_sha256)
        output = {"status": "MERGED_REPLAY_VERIFIED", "network_calls": 0,
            "windows": report.total_windows,
            "approved_facts": sum(d.accepted_fact_count for d in report.window_diagnostics),
            "excluded_facts": sum(d.rejected_fact_count for d in report.window_diagnostics),
            "memory_count": len(report.memories),
            "memory_set_fingerprint": receipt.reviewed_memory_set_fingerprint,
            "manifest_fingerprint": manifest["digest"],
            "review_fingerprint": verified.approval_fingerprint, "published": False}
        if not args.publish:
            print(json.dumps(output))
            return 0
        if args.artifact_dir is None:
            raise MergedPublicationError("ARTIFACT_DIR_REQUIRED")
        repo_root = Path(__file__).resolve().parents[1]
        if (args.artifact_dir.resolve().is_relative_to(args.state_dir.resolve()) or
                args.artifact_dir.resolve().is_relative_to(repo_root) or
                (args.index and (args.index_dir.resolve().is_relative_to(args.state_dir.resolve()) or
                                  args.index_dir.resolve().is_relative_to(repo_root)))):
            raise MergedPublicationError("OUTPUT_DIR_UNSAFE")
        ports = ArtifactPorts(args.artifact_dir, plan.bundle, plan.settings, production=True)
        store = ports.store(plan.sample_id)
        try:
            existing = store.load()
        except ArtifactStoreError as exc:
            if exc.code != "BUILD_NOT_FOUND":
                raise
            existing = None
        if existing is not None:
            if (existing.manifest.memory_set_fingerprint != receipt.reviewed_memory_set_fingerprint or
                    existing.manifest.production_provenance.reviewer_id != receipt.reviewer_id or
                    existing.manifest.production_provenance.review_fingerprint != _fp(receipt.model_dump(mode="json")) or
                    existing.manifest.production_provenance.extraction_lineage_fingerprint != receipt.extraction_lineage_fingerprint):
                raise MergedPublicationError("PUBLISHED_BUILD_CONFLICT")
            artifact = existing
        else:
            artifact = store.publish_reviewed(report, receipt, verified_review=verified,
                lineage_receipt=(args.state_dir / "merged" / "manifest.json").read_bytes())
        checked = ports.load(plan.sample_id)
        if checked.source_manifest_fingerprint != artifact.source_manifest_fingerprint:
            raise MergedPublicationError("ARTIFACT_READBACK_FAILED")
        ports.record_verified(checked)
        output.update(status="PUBLISHED", published=True,
                      build_id=checked.manifest.build_id,
                      memory_count=len(checked.memories),
                      source_manifest_fingerprint=checked.source_manifest_fingerprint)
        if args.index:
            encoder = LocalIndexEncoder(EmbeddingConfig(str(MODEL_ROOT), MODEL_REVISION,
                query_prompt="Represent this sentence for searching relevant passages: ",
                document_prompt=""))
            retrieval = plan.settings.retrieval.model_copy(update={
                "embedding_model": encoder.identity.model_name})
            index = IndexManager(encoder).build_index(checked, retrieval)
            index_path = args.index_dir / f"{plan.sample_id}.json"
            index_path.parent.mkdir(parents=True, exist_ok=True)
            if index_path.exists():
                loaded = IndexManager(encoder).load_index(index_path, checked, retrieval)
                if loaded.index_fingerprint != index.index_fingerprint:
                    raise MergedPublicationError("INDEX_CONFLICT")
            else:
                save_index(index, index_path)
                loaded = IndexManager(encoder).load_index(index_path, checked, retrieval)
            request = QueryRequest(query_id="merged-real-smoke", sample_id=plan.sample_id,
                                   question="What did Jon do after losing his banking job?")
            snapshot = VectorRetriever(encoder, retrieval).retrieve_with_trace(
                request, analyze_query(request), loaded)
            valid_ids = {item.memory_id for item in checked.memories}
            if any(item.memory_id not in valid_ids for item in snapshot.candidates.items):
                raise MergedPublicationError("INDEX_SOURCE_MISMATCH")
            output.update(index_path=str(index_path),
                index_fingerprint=loaded.index_fingerprint,
                candidate_count=len(snapshot.candidates.items))
        print(json.dumps(output, ensure_ascii=False))
        return 0
    except MergedPublicationError as exc:
        print(json.dumps({"status": exc.code, "network_calls": 0}))
        return 2
    except ArtifactStoreError as exc:
        print(json.dumps({"status": exc.code, "network_calls": 0}))
        return 2
    except Exception:
        print(json.dumps({"status": "MERGED_PUBLISH_FAILED", "network_calls": 0}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
