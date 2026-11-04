"""Gold-free fixed-window plan and offline dry-run for conv-30 non-D QA.

The command never opens a provider connection. It prepares exact question
identities, validates the real Memory artifact and BGE index, and can run all
three paths through an injected in-memory provider response.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from dynamic_memory.baselines.full_text import FullTextAnswerer, FullTextProvenance
from dynamic_memory.data.loader import load_conversations
from dynamic_memory.data.repository import SourceRepository
from dynamic_memory.integration.artifacts import ArtifactPorts
from dynamic_memory.llm.deepseek import _new_sdk_client
from dynamic_memory.pipeline.query_runner import QueryOrchestrator
from dynamic_memory.pipeline.runtime import LLMRuntime, create_runtime
from dynamic_memory.pipeline.settings import AppSettings, load_settings
from dynamic_memory.retrieval import (
    EmbeddingConfig, IndexManager, LocalIndexEncoder, QUERY_ANALYSIS_VERSION,
    VectorAnswerProvenance, analyze_query,
)
from dynamic_memory.retrieval.service_ports import RetrievalServicePorts
from dynamic_memory.services.dynamic_memory_service import IndexReadiness
from evaluation.predictions import PredictionReceipt, save_predictions, verify_predictions
from evaluation.question_manifest import QuestionManifest, make_question_record

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "raw" / "locomo10.json"
SPLIT = ROOT / "configs" / "data_split.yaml"
PROFILE = ROOT / "configs" / "models_high_pilot.yaml"
DEFAULT_ARTIFACT_ROOT = Path(
    r"C:\Users\Keshu\Documents\SRE-Project-RuntimeAssets\semantic_v3_artifacts"
)
DEFAULT_INDEX = Path(
    r"C:\Users\Keshu\Documents\SRE-Project-RuntimeAssets\semantic_v3_indexes\conv-30.json"
)
MODEL_ROOT = Path(
    r"C:\Users\Keshu\Documents\SRE-Project-RuntimeAssets\models"
    r"\bge-small-en-v1.5\5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
)
MODEL_REVISION = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
SAMPLE_ID = "conv-30"
METHODS = ("full_text", "vector_rag", "pure_vector_topk")
PROVIDER_DEFAULT_OUTPUT_TOKENS = 65536
PEAK_INPUT_USD_PER_MILLION = Decimal("0.30")
PEAK_OUTPUT_USD_PER_MILLION = Decimal("1.20")

# These are fixed development questions. Expected evidence is a local
# review annotation derived from the conversation, never from QA Gold.
QUESTIONS = (
    {
        "index": 2,
        "question": "How do Jon and Gina both like to destress?",
        "expected_evidence": ("session_1/D1:6", "session_11/D11:8"),
    },
    {
        "index": 25,
        "question": "What does Jon's dance studio offer?",
        "expected_evidence": ("session_8/D8:13", "session_13/D13:7"),
    },
    {
        "index": 54,
        "question": "How is Gina's store doing?",
        "expected_evidence": ("session_7/D7:4",),
    },
    {
        "index": 80,
        "question": (
            "What plans does Jon have after receiving advice at the networking event?"
        ),
        "expected_evidence": ("session_18/D18:10",),
    },
)
CODE_SUFFIXES = frozenset({".py", ".yaml"})
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}\Z")


class Stage4Error(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class Stage4Approval(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    format_version: Literal[1] = 1
    authorization_id: str = Field(pattern=r"^user-chat-[A-Za-z0-9_.:-]{1,80}$")
    approved_by: Literal["ACN67"]
    approved_at: datetime
    expires_at: datetime
    run_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    sample_id: Literal["conv-30"]
    model_id: Literal["deepseek-flash"]
    thinking: Literal["enabled"]
    reasoning_effort: Literal["high"]
    request_max_tokens_omitted: Literal[True]
    max_input_tokens_per_call: Literal[32768]
    provider_default_output_tokens_per_call: Literal[65536]
    max_new_http_attempts: Literal[12]
    retry_attempts: Literal[0]
    max_concurrency: Literal[1]
    max_total_usd: Decimal
    price_checked_on: date
    authorized_methods: tuple[
        Literal["full_text", "vector_rag", "pure_vector_topk"], ...
    ]


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _fp(value: object) -> str:
    return "sha256:" + _sha_bytes((_canonical(value) + "\n").encode("utf-8"))


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise Stage4Error("JSON_READ_FAILED") from None


def _atomic_json(path: Path, value: object) -> None:
    raw = (_canonical(value) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.",
            suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        raise Stage4Error("LEDGER_WRITE_FAILED") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _estimate(messages) -> int:
    return sum(len(message.content.encode("utf-8")) + 16 for message in messages)


def _read_api_key() -> str | None:
    value = os.environ.get("DEEPSEEK_API_KEY")
    if value and value.strip():
        return value
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
            value, _ = winreg.QueryValueEx(handle, "DEEPSEEK_API_KEY")
    except OSError:
        return None
    return value if isinstance(value, str) and value.strip() else None


def _git_sha() -> str:
    try:
        value = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        raise Stage4Error("GIT_IDENTITY_UNAVAILABLE") from None
    if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
        raise Stage4Error("GIT_IDENTITY_UNAVAILABLE")
    return value


def _code_fingerprint() -> str:
    files = []
    for base in (ROOT / "src", ROOT / "evaluation", ROOT / "scripts", ROOT / "configs"):
        for path in sorted(item for item in base.rglob("*") if item.is_file()):
            if path.suffix not in CODE_SUFFIXES:
                continue
            files.append({
                "path": path.relative_to(ROOT).as_posix(),
                "sha256": _sha_bytes(path.read_bytes()),
            })
    return _fp(files)


def _question_manifest(bundle) -> QuestionManifest:
    records = tuple(
        make_question_record(
            snapshot_sha256=bundle.snapshot_sha256,
            sample_id=SAMPLE_ID,
            original_qa_index=item["index"],
            question=item["question"],
            split="main_dev",
        )
        for item in QUESTIONS
    )
    return QuestionManifest(bundle.snapshot_sha256, records, ("main_dev",), None)


class _NoNetworkClient:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=self)

    def create(self, **_kwargs):
        raise Stage4Error("PLAN_MODE_CANNOT_SEND")


class _DryRunClient:
    """Provider-shaped in-memory client; no network client is constructed."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        if kwargs.get("model") != "deepseek-flash":
            raise Stage4Error("DRY_RUN_MODEL_MISMATCH")
        if kwargs.get("max_tokens") is not None:
            raise Stage4Error("DRY_RUN_UNEXPECTED_MAX_TOKENS")
        if kwargs.get("reasoning_effort") != "high":
            raise Stage4Error("DRY_RUN_REASONING_MISMATCH")
        self.calls.append(kwargs)
        user = kwargs["messages"][-1]["content"]
        if "Return one JSON object as text" in user:
            context = user.split("Context data:\n", 1)[1].split(
                "\n\nQuestion data:", 1
            )[0]
            memories = json.loads(context)
            refs = memories[0]["source_refs"] if memories else []
            content = _canonical({
                "status": "answered" if refs else "no_evidence",
                "answer": "Dry-run answer." if refs else None,
                "evidence_refs": refs[:1],
                "uncertainty": None if refs else "No dry-run candidates.",
            })
        else:
            content = "Dry-run answer."
        return SimpleNamespace(
            choices=[SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content=content, reasoning_content="not persisted"),
            )],
            model="deepseek-flash",
            usage=SimpleNamespace(
                prompt_tokens=1,
                completion_tokens=1,
                completion_tokens_details=SimpleNamespace(reasoning_tokens=0),
            ),
        )


class _BudgetedClient:
    """No-retry SDK wrapper with a durable attempt ledger."""

    def __init__(self, client, approval: Stage4Approval, output_dir: Path) -> None:
        self._client = client
        self._approval = approval
        self._root = output_dir / "budget" / "calls"
        self._root.mkdir(parents=True, exist_ok=False)
        self._method: str | None = None
        self._query_id: str | None = None
        self.failed = False
        self.attempts = 0
        self.chat = SimpleNamespace(completions=self)

    def set_context(self, method: str, query_id: str) -> None:
        if method not in self._approval.authorized_methods or not _RUN_ID.fullmatch(query_id):
            raise Stage4Error("CALL_CONTEXT_INVALID")
        self._method = method
        self._query_id = query_id

    def create(self, **kwargs):
        if self._method is None or self._query_id is None:
            raise Stage4Error("CALL_CONTEXT_MISSING")
        rows = sorted(self._root.glob("attempt_*.json"))
        if len(rows) >= self._approval.max_new_http_attempts:
            raise Stage4Error("CALL_BUDGET_EXHAUSTED")
        sequence = len(rows) + 1
        request_sha256 = _sha_bytes(_canonical(kwargs).encode("utf-8"))
        attempt_path = self._root / f"attempt_{sequence:04d}.json"
        attempt = {
            "format_version": 1,
            "sequence": sequence,
            "authorization_id": self._approval.authorization_id,
            "method": self._method,
            "query_id": self._query_id,
            "request_sha256": request_sha256,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "state": "started",
        }
        _atomic_json(attempt_path, attempt)
        self.attempts += 1
        try:
            response = self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            self.failed = True
            attempt.update({
                "state": "failed",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "error_type": type(exc).__name__,
            })
            _atomic_json(attempt_path, attempt)
            raise
        choice = response.choices[0]
        usage = response.usage
        response_row = {
            "model": response.model,
            "finish_reason": choice.finish_reason,
            "content": choice.message.content,
            "usage": {
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "reasoning_tokens": getattr(
                    getattr(usage, "completion_tokens_details", None),
                    "reasoning_tokens",
                    None,
                ),
                "prompt_cache_hit_tokens": getattr(
                    usage, "prompt_cache_hit_tokens", None
                ),
                "prompt_cache_miss_tokens": getattr(
                    usage, "prompt_cache_miss_tokens", None
                ),
            } if usage is not None else None,
        }
        attempt.update({
            "state": "completed",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "response": response_row,
        })
        _atomic_json(attempt_path, attempt)
        return response


@dataclass(frozen=True)
class System:
    settings: AppSettings
    bundle: object
    artifact: object
    index: object
    runtime: LLMRuntime
    orchestrator: QueryOrchestrator
    manifest: QuestionManifest
    git_sha: str
    code_fingerprint: str
    full_diagnostics: list
    vector_diagnostics: list


def _make_system(*, artifact_root: Path, index_path: Path,
                 client=None) -> System:
    settings = load_settings(models_profile=PROFILE)
    models = settings.models
    if (
        models.model_id != "deepseek-flash"
        or models.thinking != "enabled"
        or models.reasoning_effort != "high"
        or not models.qa_omit_max_tokens
        or models.max_retries != 0
        or models.max_concurrent_api_requests != 1
    ):
        raise Stage4Error("QA_PROFILE_UNSAFE")
    if not DATA.is_file() or not SPLIT.is_file():
        raise Stage4Error("DATA_UNAVAILABLE")
    bundle = load_conversations(str(DATA), str(SPLIT))
    if bundle.split_by_sample_id.get(SAMPLE_ID) != "main_dev":
        raise Stage4Error("SPLIT_MISMATCH")
    ports = ArtifactPorts(artifact_root, bundle, settings, production=True)
    artifact = ports.load(SAMPLE_ID)
    if (
        artifact.manifest.build_id != "build_bdb0dfc33cc241ce9299a123df2ce091"
        or artifact.manifest.memory_set_fingerprint
        != "sha256:2674d020693bdc43dd9d59bf7661873d41dfcf0bd095656fc215a5155abadec9"
        or len(artifact.memories) != 600
    ):
        raise Stage4Error("MEMORY_IDENTITY_MISMATCH")

    encoder = LocalIndexEncoder(EmbeddingConfig(
        str(MODEL_ROOT),
        MODEL_REVISION,
        query_prompt="Represent this sentence for searching relevant passages: ",
        document_prompt="",
    ))
    retrieval = settings.retrieval.model_copy(
        update={"embedding_model": encoder.identity.model_name}
    )
    index = IndexManager(encoder).load_index(index_path, artifact, retrieval)
    if (
        index.index_fingerprint
        != "sha256:34d42d245361282cb40d5d5b1823aa36f5a984283336ba6ff945ec1b0707ec21"
        or index.sample_id != SAMPLE_ID
        or len(index.memories) != 600
        or index.dimension != 384
    ):
        raise Stage4Error("INDEX_IDENTITY_MISMATCH")

    runtime = create_runtime(
        settings,
        backend="deepseek",
        api_enabled=True,
        deepseek_client=client or _NoNetworkClient(),
        model_id="deepseek-flash",
        token_estimator=_estimate,
        estimator_version="utf8-bytes-plus16-v1",
        strict_budget=True,
        cache_enabled=False,
    )
    git_sha = _git_sha()
    full_diagnostics: list = []
    vector_diagnostics: list = []
    full = FullTextAnswerer(
        runtime,
        FullTextProvenance(
            "sha256:" + bundle.snapshot_sha256,
            QUERY_ANALYSIS_VERSION,
            git_sha,
            "development",
        ),
        on_diagnostic=full_diagnostics.append,
        synthetic_test_mode=False,
    )
    retrieval_ports = RetrievalServicePorts(
        encoder=encoder,
        settings=retrieval,
        load_artifact=ports.load,
        index_path=lambda _sample_id: index_path,
        readiness_factory=IndexReadiness,
        runtime=runtime,
        provenance_factory=lambda source: VectorAnswerProvenance(
            "sha256:" + source.manifest.dataset_snapshot_sha256,
            QUERY_ANALYSIS_VERSION,
            git_sha,
            "development",
        ),
        on_diagnostic=vector_diagnostics.append,
        synthetic_test_mode=False,
    )
    orchestrator = QueryOrchestrator(
        analyze_query=analyze_query,
        get_sample=SourceRepository(bundle).get_sample,
        load_verified_index=retrieval_ports.load_verified_index,
        retrieve=retrieval_ports.retrieve,
        retrieve_raw_topk=retrieval_ports.retrieve_raw_topk,
        load_candidates=retrieval_ports.load_candidates,
        answer_full_text=full,
        answer_vector_rag=retrieval_ports.answer_vector_rag,
        resolve=None,
        answer_dynamic=None,
    )
    return System(
        settings=settings,
        bundle=bundle,
        artifact=artifact,
        index=index,
        runtime=runtime,
        orchestrator=orchestrator,
        manifest=_question_manifest(bundle),
        git_sha=git_sha,
        code_fingerprint=_code_fingerprint(),
        full_diagnostics=full_diagnostics,
        vector_diagnostics=vector_diagnostics,
    )


def _peak_reserve(settings: AppSettings) -> dict:
    input_tokens = settings.models.qa_max_input_tokens
    output_tokens = PROVIDER_DEFAULT_OUTPUT_TOKENS
    per_call = (
        Decimal(input_tokens) * PEAK_INPUT_USD_PER_MILLION
        + Decimal(output_tokens) * PEAK_OUTPUT_USD_PER_MILLION
    ) / Decimal(1_000_000)
    calls = len(QUESTIONS) * len(METHODS)
    return {
        "input_tokens_per_call": input_tokens,
        "provider_default_output_tokens_per_call": output_tokens,
        "maximum_new_http_attempts": calls,
        "retry_attempts": 0,
        "peak_input_usd_per_million": str(PEAK_INPUT_USD_PER_MILLION),
        "peak_output_usd_per_million": str(PEAK_OUTPUT_USD_PER_MILLION),
        "worst_case_usd_per_call": format(per_call, ".7f"),
        "worst_case_usd_total": format(per_call * calls, ".7f"),
    }


def _plan(system: System) -> dict:
    question_rows = []
    for spec, record in zip(QUESTIONS, system.manifest.records):
        question_rows.append({
            "query_id": record.query_id,
            "sample_id": record.sample_id,
            "original_qa_index": record.original_qa_index,
            "question": record.question,
            "gold_record_key": record.gold_record_key,
            "review_expected_evidence": list(spec["expected_evidence"]),
        })
    identity = {
        "sample_id": SAMPLE_ID,
        "dataset_sha256": system.bundle.snapshot_sha256,
        "artifact_build_id": system.artifact.manifest.build_id,
        "memory_set_fingerprint": system.artifact.manifest.memory_set_fingerprint,
        "index_fingerprint": system.index.index_fingerprint,
        "retriever_fingerprint": system.index.retriever_fingerprint,
        "embedding_model_revision": MODEL_REVISION,
        "questions": question_rows,
        "methods": list(METHODS),
        "runtime_config_fingerprint": system.runtime.config_fingerprint,
        "execution_policy_fingerprint": system.runtime.execution_policy_fingerprint,
        "git_sha": system.git_sha,
        "code_fingerprint": system.code_fingerprint,
    }
    return {
        "format_version": 1,
        "status": "STAGE4_PLAN_READY",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "run_fingerprint": _fp(identity),
        "identity": identity,
        "network_calls": 0,
        "provider": {
            "model_id": "deepseek-flash",
            "thinking": "enabled",
            "reasoning_effort": "high",
            "max_tokens_omitted": True,
            "max_concurrency": 1,
            "retries": 0,
        },
        "budget": _peak_reserve(system.settings),
        "requires_new_paid_run_approval": True,
    }


def _load_approval(path: Path) -> Stage4Approval:
    try:
        return Stage4Approval.model_validate_json(path.read_bytes())
    except (OSError, UnicodeError, ValidationError):
        raise Stage4Error("APPROVAL_INVALID") from None


def _validate_approval(approval: Stage4Approval, plan: dict,
                       approval_path: Path, output_dir: Path) -> None:
    now = datetime.now(timezone.utc)
    budget = plan["budget"]
    if (
        approval.run_fingerprint != plan["run_fingerprint"]
        or approval.sample_id != SAMPLE_ID
        or approval.authorized_methods != METHODS
        or approval.max_new_http_attempts != budget["maximum_new_http_attempts"]
        or approval.max_input_tokens_per_call != budget["input_tokens_per_call"]
        or approval.provider_default_output_tokens_per_call
        != budget["provider_default_output_tokens_per_call"]
        or approval.max_total_usd != Decimal(budget["worst_case_usd_total"])
        or approval.approved_at.tzinfo is None
        or approval.expires_at.tzinfo is None
        or approval.approved_at > now
        or approval.expires_at <= now
        or approval.price_checked_on != now.date()
        or approval_path.resolve().is_relative_to(output_dir.resolve())
    ):
        raise Stage4Error("APPROVAL_MISMATCH")


def _run_live(system: System, client: _BudgetedClient,
              output_dir: Path) -> dict:
    receipts: dict[str, PredictionReceipt] = {}
    checked_rows = {}
    for method in METHODS:
        responses = []
        for record in system.manifest.records:
            client.set_context(method, record.query_id)
            prepared = system.orchestrator.prepare_query(record.to_query_request())
            if method == "pure_vector_topk":
                response = system.orchestrator.run_pure_vector_prepared(
                    prepared,
                    base_top_k=system.settings.retrieval.optional_pure_vector_top_k,
                ).response
            else:
                response = system.orchestrator.run_prepared(prepared, method)
            if client.failed:
                raise Stage4Error("LIVE_HTTP_CALL_FAILED")
            if response.answer_result.status == "error":
                raise Stage4Error("LIVE_RESPONSE_FAILED")
            responses.append(response)
        path = output_dir / f"{method}.predictions.json"
        receipt = save_predictions(path, system.manifest, responses, method)
        checked_rows[method] = verify_predictions(path, system.manifest, receipt)
        receipts[method] = receipt
    if client.attempts != len(QUESTIONS) * len(METHODS):
        raise Stage4Error("LIVE_CALL_COUNT_MISMATCH")
    statuses = {
        method: {
            status: sum(
                row.answer_result.status == status for row in checked_rows[method]
            )
            for status in (
                "answered",
                "no_evidence",
                "time_uncertain",
                "unresolved_conflict",
                "error",
            )
        }
        for method in METHODS
    }
    return {
        "status": "STAGE4_LIVE_COMPLETE",
        "network_calls": client.attempts,
        "retry_attempts": 0,
        "prediction_sha256": {
            method: receipt.file_sha256 for method, receipt in receipts.items()
        },
        "answer_status_counts": statuses,
        "budget_ledger": str(output_dir / "budget" / "calls"),
    }


def _run_dry(system: System, client: _DryRunClient, output_dir: Path) -> dict:
    receipts: dict[str, PredictionReceipt] = {}
    checked_rows = {}
    for method in METHODS:
        responses = []
        for record in system.manifest.records:
            prepared = system.orchestrator.prepare_query(record.to_query_request())
            if method == "pure_vector_topk":
                response = system.orchestrator.run_pure_vector_prepared(
                    prepared,
                    base_top_k=system.settings.retrieval.optional_pure_vector_top_k,
                ).response
            else:
                response = system.orchestrator.run_prepared(prepared, method)
            responses.append(response)
        path = output_dir / f"{method}.predictions.json"
        receipt = save_predictions(path, system.manifest, responses, method)
        checked_rows[method] = verify_predictions(path, system.manifest, receipt)
        receipts[method] = receipt
    if len(client.calls) != len(QUESTIONS) * len(METHODS):
        raise Stage4Error("DRY_RUN_CALL_COUNT_MISMATCH")
    statuses = {
        method: {
            status: sum(
                row.answer_result.status == status for row in checked_rows[method]
            )
            for status in (
                "answered",
                "no_evidence",
                "time_uncertain",
                "unresolved_conflict",
                "error",
            )
        }
        for method in METHODS
    }
    comparisons = []
    for index, record in enumerate(system.manifest.records):
        vector = checked_rows["vector_rag"][index]
        pure = checked_rows["pure_vector_topk"][index]
        vector_ids = (
            [item.memory_id for item in vector.retrieved.items]
            if vector.retrieved is not None
            else []
        )
        pure_ids = (
            [item.memory_id for item in pure.retrieved.items]
            if pure.retrieved is not None
            else []
        )
        comparisons.append({
            "query_id": record.query_id,
            "vector_candidate_ids": vector_ids,
            "pure_vector_candidate_ids": pure_ids,
            "vector_used_memory_ids": vector.answer_result.used_memory_ids,
            "pure_vector_used_memory_ids": pure.answer_result.used_memory_ids,
        })
    summary = {
        "status": "STAGE4_DRY_RUN_COMPLETE",
        "network_calls": 0,
        "fake_provider_invocations": len(client.calls),
        "methods": list(METHODS),
        "question_count": len(system.manifest.records),
        "prediction_sha256": {
            method: receipt.file_sha256 for method, receipt in receipts.items()
        },
        "answer_status_counts": statuses,
        "candidate_comparison": comparisons,
        "full_text_diagnostics": [
            {
                "included_turns": len(item.included_refs),
                "dropped_turns": len(item.dropped_refs),
                "truncated": item.truncated_full_text,
                "input_tokens_estimated": item.input_tokens_estimated,
            }
            for item in system.full_diagnostics
        ],
        "vector_diagnostics": [
            {
                "query_id": item.query_id,
                "included_memory_ids": list(item.included_memory_ids),
                "dropped_memory_ids": list(item.dropped_memory_ids),
                "input_tokens_estimated": item.input_tokens_estimated,
            }
            for item in system.vector_diagnostics
        ],
    }
    return summary


def _new_output_dir(path: Path) -> Path:
    target = path.resolve()
    allowed = (Path(tempfile.gettempdir()).resolve(), (ROOT / "results").resolve())
    if not any(target.is_relative_to(parent) for parent in allowed):
        raise Stage4Error("OUTPUT_DIRECTORY_UNSAFE")
    if target.exists() and any(target.iterdir()):
        raise Stage4Error("OUTPUT_DIRECTORY_IN_USE")
    target.mkdir(parents=True, exist_ok=True)
    return target


def _write_json(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("plan", "dry-run", "live"), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--index-path", type=Path, default=DEFAULT_INDEX)
    args = parser.parse_args()
    try:
        client = _DryRunClient() if args.mode == "dry-run" else _NoNetworkClient()
        planning_system = _make_system(
            artifact_root=args.artifact_root,
            index_path=args.index_path,
            client=client,
        )
        plan = _plan(planning_system)
        if args.mode == "live":
            if args.approval is None:
                raise Stage4Error("APPROVAL_REQUIRED")
            approval = _load_approval(args.approval)
            _validate_approval(approval, plan, args.approval, args.output_dir)
            api_key = _read_api_key()
            if api_key is None:
                raise Stage4Error("CREDENTIAL_REQUIRED")
            output_dir = _new_output_dir(args.output_dir)
            sdk = _new_sdk_client(
                api_key, planning_system.settings.models.request_timeout_seconds
            )
            client = _BudgetedClient(sdk, approval, output_dir)
            system = _make_system(
                artifact_root=args.artifact_root,
                index_path=args.index_path,
                client=client,
            )
            if _plan(system)["run_fingerprint"] != plan["run_fingerprint"]:
                raise Stage4Error("RUN_FINGERPRINT_CHANGED")
        else:
            output_dir = _new_output_dir(args.output_dir)
            system = planning_system
        _write_json(output_dir / "plan.json", plan)
        output = {"plan": str(output_dir / "plan.json"), **plan["budget"]}
        if args.mode == "dry-run":
            summary = _run_dry(system, client, output_dir)
            _write_json(output_dir / "dry_run.json", summary)
            output.update(status=summary["status"], network_calls=0,
                          fake_provider_invocations=summary["fake_provider_invocations"])
        elif args.mode == "live":
            summary = _run_live(system, client, output_dir)
            _write_json(output_dir / "run_summary.json", summary)
            output.update(status=summary["status"],
                          network_calls=summary["network_calls"])
        else:
            output.update(status=plan["status"], network_calls=0)
        print(_canonical(output))
        return 0
    except Exception as exc:
        code = getattr(exc, "code", None)
        print(_canonical({
            "status": code if isinstance(code, str) else "STAGE4_STOPPED",
            "error_type": type(exc).__name__,
            "network_calls": 0,
        }))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
