"""Plan, dry-run, or execute the fixed three-method main_dev comparison."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
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
from dynamic_memory.llm.prompts import QA_BENCHMARK_PROMPT, QA_PROMPT
from dynamic_memory.llm.token_estimate import ESTIMATOR_VERSION, estimate_messages_tokens
from dynamic_memory.qa.benchmark_protocol import BenchmarkQuestion, render_question
from dynamic_memory.pipeline.query_runner import QueryOrchestrator
from dynamic_memory.pipeline.runtime import LLMRuntime, create_runtime
from dynamic_memory.pipeline.settings import AppSettings, load_settings
from dynamic_memory.reasoning import DynamicProvenance, DynamicServicePorts
from dynamic_memory.retrieval import (
    EmbeddingConfig,
    IndexManager,
    LocalIndexEncoder,
    QUERY_ANALYSIS_VERSION,
    VectorAnswerProvenance,
    analyze_query,
)
from dynamic_memory.retrieval.service_ports import RetrievalServicePorts
from dynamic_memory.services.dynamic_memory_service import IndexReadiness
from evaluation.predictions import (
    PredictionReceipt,
    save_predictions,
    verify_predictions,
)
from evaluation.question_manifest import (
    QuestionManifest,
    load_question_manifest,
    make_question_record,
)
from scripts.stage4_non_d_fixed import (
    PEAK_INPUT_USD_PER_MILLION,
    PEAK_OUTPUT_USD_PER_MILLION,
    _BudgetedClient,
    _NoNetworkClient,
    _atomic_json,
    _canonical,
    _code_fingerprint,
    _estimate,
    _fp,
    _git_sha,
    _new_output_dir,
    _read_api_key,
    _read_json,
    _write_json,
)


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/raw/locomo10.json"
SPLIT = ROOT / "configs/data_split.yaml"
PROFILE = ROOT / "configs/models_benchmark.yaml"
DEFAULT_ARTIFACT_ROOT = Path(
    r"C:\Users\Keshu\Documents\SRE-Project-RuntimeAssets\semantic_v3_artifacts"
)
DEFAULT_INDEX_ROOT = Path(
    r"C:\Users\Keshu\Documents\SRE-Project-RuntimeAssets\semantic_v3_indexes"
)
MODEL_ROOT = Path(
    r"C:\Users\Keshu\Documents\SRE-Project-RuntimeAssets\models"
    r"\bge-small-en-v1.5\5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
)
MODEL_REVISION = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
PROVIDER_DEFAULT_OUTPUT_TOKENS = 65536
STATE_REASONING_INPUT_TOKENS = 934464
QA_INPUT_TOKENS = 32768
METHODS = ("full_text", "vector_rag", "dynamic")
MAIN_DEV_SAMPLES = ("conv-26", "conv-30", "conv-41", "conv-42")
TEST_SAMPLES = ("conv-49", "conv-50")
ALLOWED_SAMPLES = MAIN_DEV_SAMPLES + TEST_SAMPLES

QUESTIONS = {
    "conv-26": (
        {
            "index": 2,
            "question": (
                "What fields would Caroline be likely to pursue in her educaton?"
            ),
        },
        {
            "index": 25,
            "question": "When did Caroline go to the LGBTQ conference?",
        },
        {
            "index": 54,
            "question": "When did Caroline draw a self-portrait?",
        },
        {
            "index": 80,
            "question": "When did Melanie buy the figurines?",
        },
    ),
    "conv-30": (
        {
            "index": 2,
            "question": "How do Jon and Gina both like to destress?",
        },
        {
            "index": 25,
            "question": "What does Jon's dance studio offer?",
        },
        {
            "index": 54,
            "question": "How is Gina's store doing?",
        },
        {
            "index": 80,
            "question": (
                "What plans does Jon have after receiving advice at the "
                "networking event?"
            ),
        },
    ),
    "conv-41": (
        {
            "index": 2,
            "question": "What martial arts has John done?",
        },
        {
            "index": 25,
            "question": "What European countries has Maria been to?",
        },
        {
            "index": 54,
            "question": (
                "When did Maria take up community work with her church friends?"
            ),
        },
        {
            "index": 80,
            "question": (
                "What did Maria make for her home to remind her of a trip to "
                "England?"
            ),
        },
    ),
    "conv-42": (
        {
            "index": 2,
            "question": (
                'When did Joanna first watch "Eternal Sunshine of the '
                'Spotless Mind?'
            ),
        },
        {
            "index": 25,
            "question": "When did Joanna hike with her buddies?",
        },
        {
            "index": 54,
            "question": (
                "When was Joanna's second movie script shown on the big screens?"
            ),
        },
        {
            "index": 80,
            "question": "How many tournaments has Nate won?",
        },
    ),
}


class Stage6DevError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ThreeMethodApproval(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    format_version: Literal[1] = 1
    authorization_id: str = Field(pattern=r"^user-chat-[A-Za-z0-9_.:-]{1,80}$")
    approved_by: Literal["ACN67"]
    approved_at: datetime
    expires_at: datetime
    run_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    sample_id: Literal[
        "conv-26", "conv-30", "conv-41", "conv-42", "conv-49", "conv-50"
    ]
    authorized_methods: tuple[
        Literal["full_text", "vector_rag", "dynamic"], ...
    ]
    model_id: Literal["deepseek-flash"]
    thinking: Literal["enabled"]
    reasoning_effort: Literal["high"]
    request_max_tokens_omitted: Literal[True]
    max_new_http_attempts: int = Field(ge=1)
    state_reasoning_calls: int = Field(ge=1)
    qa_calls: int = Field(ge=1)
    retry_attempts: Literal[0]
    max_concurrency: Literal[1]
    max_total_usd: Decimal
    price_checked_on: date


class _DynamicDryRunClient:
    """Provider-shaped client for a full no-network three-method replay."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        if kwargs.get("model") != "deepseek-flash":
            raise Stage6DevError("DRY_RUN_MODEL_MISMATCH")
        if kwargs.get("max_tokens") is not None:
            raise Stage6DevError("DRY_RUN_UNEXPECTED_MAX_TOKENS")
        if kwargs.get("reasoning_effort") != "high":
            raise Stage6DevError("DRY_RUN_REASONING_MISMATCH")
        self.calls.append(kwargs)
        user = kwargs["messages"][-1]["content"]
        if "Candidate context data:\n" in user:
            query = json.loads(
                user.split("Query data:\n", 1)[1].split(
                    "\n\nCandidate context data:", 1
                )[0]
            )["request"]
            memories = json.loads(
                user.split("Candidate context data:\n", 1)[1].split(
                    "\n\nOutput JSON schema:", 1
                )[0]
            )["memories"]
            content = _canonical({
                "schema_version": "2.1",
                "query_id": query["query_id"],
                "relations": [],
                "decisions": [
                    {
                        "memory_id": memory["memory_id"],
                        "action": "RETAIN",
                        "reason": "No-network three-method dry run.",
                    }
                    for memory in memories
                ],
                "effective_memory_ids": [
                    memory["memory_id"] for memory in memories
                ],
                "unresolved_conflict_ids": [],
                "uncertainty": None,
            })
        elif "Return one JSON object as text" in user:
            memories = json.loads(
                user.split("Context data:\n", 1)[1].split(
                    "\n\nQuestion data:", 1
                )[0]
            )
            refs = memories[0]["source_refs"] if memories else []
            content = _canonical({
                "status": "answered" if refs else "no_evidence",
                "answer": "No-network fixed-question dry-run answer."
                if refs else None,
                "evidence_refs": refs[:1],
                "uncertainty": None if refs else "No candidates.",
            })
        else:
            content = "No-network Full Text dry-run answer."
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(
                        content=content,
                        reasoning_content="not persisted",
                    ),
                )
            ],
            model="deepseek-flash",
            usage=SimpleNamespace(
                prompt_tokens=1,
                completion_tokens=1,
                completion_tokens_details=SimpleNamespace(reasoning_tokens=0),
            ),
        )


@dataclass(frozen=True)
class System:
    sample_id: str
    split: str
    question_mode: str
    integration_mode: str
    settings: AppSettings
    bundle: object
    artifact: object
    index: object
    runtime: LLMRuntime
    orchestrator: QueryOrchestrator
    manifest: QuestionManifest
    questions_file_sha256: str | None
    git_sha: str
    code_fingerprint: str


def _index_path(root: Path, sample_id: str) -> Path:
    return root / f"{sample_id}.json"


def _question_policies(
    sample_id: str,
) -> tuple[dict[str, BenchmarkQuestion], dict[str, str], dict[tuple[str, str], dict]]:
    raw = _read_json(DATA)
    sample = next(row for row in raw if str(row["sample_id"]) == sample_id)
    policies: dict[str, BenchmarkQuestion] = {}
    for index, qa in enumerate(sample.get("qa", [])):
        query_id = f"qa_{sample_id}_{index}"
        policies[query_id] = BenchmarkQuestion(
            query_id=query_id,
            category=int(qa["category"]),
            adversarial_answer=qa.get("adversarial_answer") if int(qa["category"]) == 5 else None,
        )
    conversation = sample["conversation"]
    session_dates = {
        key: str(value)
        for key, value in conversation.items()
        if key.startswith("session_") and key.endswith("_date_time") and value
    }
    session_dates = {key.removesuffix("_date_time"): value for key, value in session_dates.items()}
    source_metadata = {}
    for session_id, turns in conversation.items():
        if not session_id.startswith("session_") or not isinstance(turns, list):
            continue
        for turn in turns:
            if not isinstance(turn, dict) or turn.get("dia_id") is None:
                continue
            metadata = {
                key: turn.get(key)
                for key in ("query", "img_url")
                if turn.get(key) not in (None, "", [], {})
            }
            if metadata:
                source_metadata[(session_id, str(turn["dia_id"]))] = metadata
    return policies, session_dates, source_metadata


def _make_system(
    *,
    sample_id: str,
    question_mode: str,
    artifact_root: Path,
    index_root: Path,
    questions_file: Path | None = None,
    client=None,
    integration_mode: str = "legacy",
    plain_text: bool = False,
    hybrid_context: bool = False,
    conditional_state: bool = False,
) -> System:
    if integration_mode not in {"legacy", "v2"}:
        raise Stage6DevError("INTEGRATION_MODE_INVALID")
    if integration_mode == "v2":
        plain_text = hybrid_context = conditional_state = True
    settings = load_settings(models_profile=PROFILE)
    models = settings.models
    if (
        models.model_id != "deepseek-flash"
        or models.thinking != "enabled"
        or models.reasoning_effort != "high"
        or not models.qa_omit_max_tokens
        or not models.reasoning_omit_max_tokens
        or models.max_retries != 0
        or models.max_concurrent_api_requests != 1
    ):
        raise Stage6DevError("QA_PROFILE_UNSAFE")
    bundle = load_conversations(str(DATA), str(SPLIT))
    split = "test" if sample_id in TEST_SAMPLES else "main_dev"
    if bundle.split_by_sample_id.get(sample_id) != split:
        raise Stage6DevError("SPLIT_MISMATCH")
    repository = SourceRepository(bundle)
    policies, session_dates, source_metadata = _question_policies(sample_id)

    def question_transform(query):
        policy = policies.get(query.query_id)
        if policy is None:
            return query.question
        return render_question(query, policy, session_dates=session_dates)
    answer_prompt = QA_BENCHMARK_PROMPT if plain_text else QA_PROMPT
    ports = ArtifactPorts(
        artifact_root,
        bundle,
        settings,
        production=True,
        allowed_splits=(split,),
    )
    artifact = ports.load(sample_id)
    encoder = LocalIndexEncoder(
        EmbeddingConfig(
            str(MODEL_ROOT),
            MODEL_REVISION,
            query_prompt="Represent this sentence for searching relevant passages: ",
            document_prompt="",
        )
    )
    retrieval = settings.retrieval.model_copy(
        update={"embedding_model": encoder.identity.model_name}
    )
    index = IndexManager(encoder).load_index(
        _index_path(index_root, sample_id),
        artifact,
        retrieval,
    )
    runtime = create_runtime(
        settings,
        backend="deepseek",
        api_enabled=True,
        deepseek_client=client or _NoNetworkClient(),
        model_id="deepseek-flash",
        token_estimator=estimate_messages_tokens,
        estimator_version=ESTIMATOR_VERSION,
        strict_budget=True,
        cache_enabled=False,
    )
    git_sha = _git_sha()
    run_kind = "evaluation" if split == "test" else "development"
    full = FullTextAnswerer(
        runtime,
        FullTextProvenance(
            "sha256:" + bundle.snapshot_sha256,
            QUERY_ANALYSIS_VERSION,
            git_sha,
            run_kind,
        ),
        question_transform=question_transform if plain_text else None,
        prompt_template=answer_prompt,
        synthetic_test_mode=False,
    )
    retrieval_ports = RetrievalServicePorts(
        encoder=encoder,
        settings=retrieval,
        load_artifact=ports.load,
        index_path=lambda _sample_id: _index_path(index_root, sample_id),
        readiness_factory=IndexReadiness,
        runtime=runtime,
        provenance_factory=lambda source: VectorAnswerProvenance(
            "sha256:" + source.manifest.dataset_snapshot_sha256,
            QUERY_ANALYSIS_VERSION,
            git_sha,
            run_kind,
        ),
        source_repository=repository if hybrid_context else None,
        source_metadata_lookup=(
            lambda requested_sample, ref: source_metadata.get(
                (ref.session_id, ref.dia_id), {}
            )
            if requested_sample == sample_id
            else {}
        )
        if hybrid_context
        else None,
        question_transform=question_transform if plain_text else None,
        prompt_template=answer_prompt,
        plain_text=plain_text,
        max_context_memories=retrieval.max_candidates,
        synthetic_test_mode=False,
    )

    def provenance_for_sample(requested_sample: str) -> DynamicProvenance:
        if requested_sample != sample_id:
            raise Stage6DevError("SAMPLE_MISMATCH")
        return DynamicProvenance(
            "sha256:" + artifact.manifest.dataset_snapshot_sha256,
            QUERY_ANALYSIS_VERSION,
            git_sha,
            run_kind,
            index.index_fingerprint,
        )

    def conversation_cutoff(requested_sample: str):
        if requested_sample != sample_id:
            raise Stage6DevError("SAMPLE_MISMATCH")
        source = repository.get_sample(requested_sample)
        timestamps = [
            turn.recorded_at
            for session in source.sessions
            for turn in session.turns
            if turn.recorded_at is not None
        ]
        return max(timestamps) if timestamps else None

    dynamic = DynamicServicePorts(
        runtime,
        provenance_factory=provenance_for_sample,
        conversation_cutoff=conversation_cutoff,
        source_repository=repository if hybrid_context else None,
        source_metadata_lookup=(
            lambda requested_sample, ref: source_metadata.get(
                (ref.session_id, ref.dia_id), {}
            )
            if requested_sample == sample_id
            else {}
        )
        if hybrid_context
        else None,
        question_transform=question_transform if plain_text else None,
        prompt_template=answer_prompt,
        plain_text=plain_text,
        max_context_memories=retrieval.max_candidates,
        conditional_state=conditional_state,
        synthetic_test_mode=False,
    )
    orchestrator = QueryOrchestrator(
        analyze_query=analyze_query,
        get_sample=repository.get_sample,
        load_verified_index=retrieval_ports.load_verified_index,
        retrieve=retrieval_ports.retrieve,
        retrieve_raw_topk=retrieval_ports.retrieve_raw_topk,
        load_candidates=retrieval_ports.load_candidates,
        answer_full_text=full,
        answer_vector_rag=retrieval_ports.answer_vector_rag,
        resolve=dynamic.resolve,
        answer_dynamic=dynamic.answer_dynamic,
    ).with_source_lookup(repository.get_sample)
    if question_mode == "fixed":
        if sample_id not in QUESTIONS:
            raise Stage6DevError("FULL_QUESTION_MODE_REQUIRED")
        records = tuple(
            make_question_record(
                snapshot_sha256=bundle.snapshot_sha256,
                sample_id=sample_id,
                original_qa_index=item["index"],
                question=item["question"],
                split=split,
            )
            for item in QUESTIONS[sample_id]
        )
    elif question_mode == "full":
        split_manifest = load_question_manifest(
            DATA,
            SPLIT,
            selected_splits=(split,),
        )
        records = tuple(
            record for record in split_manifest.records
            if record.sample_id == sample_id
        )
        if not records:
            raise Stage6DevError("QUESTION_MANIFEST_EMPTY")
    elif question_mode == "external":
        if questions_file is None:
            raise Stage6DevError("QUESTIONS_FILE_REQUIRED")
        try:
            document = json.loads(questions_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            raise Stage6DevError("QUESTIONS_FILE_INVALID") from None
        rows = document.get("questions") if isinstance(document, dict) else None
        if not isinstance(rows, list) or not rows:
            raise Stage6DevError("QUESTIONS_FILE_INVALID")
        if document.get("dataset_sha256") not in (None, bundle.snapshot_sha256):
            raise Stage6DevError("QUESTIONS_DATASET_MISMATCH")
        source_questions = {
            str(row["sample_id"]): tuple(row.get("qa", []))
            for row in _read_json(DATA)
        }
        selected = []
        for row in rows:
            if not isinstance(row, dict) or row.get("sample_id") != sample_id:
                continue
            qa_index = row.get("original_qa_index")
            if type(qa_index) is not int or qa_index < 0:
                raise Stage6DevError("QUESTIONS_FILE_INVALID")
            source = source_questions[sample_id][qa_index]
            if str(row.get("question")) != str(source.get("question")):
                raise Stage6DevError("QUESTIONS_FILE_INVALID")
            selected.append(make_question_record(
                snapshot_sha256=bundle.snapshot_sha256,
                sample_id=sample_id,
                original_qa_index=qa_index,
                question=str(row["question"]),
                split=split,
            ))
        if not selected:
            raise Stage6DevError("QUESTION_MANIFEST_EMPTY")
        records = tuple(selected)
    else:
        raise Stage6DevError("QUESTION_MODE_INVALID")
    manifest = QuestionManifest(
        bundle.snapshot_sha256,
        records,
        (split,),
        None,
    )
    return System(
        sample_id=sample_id,
        split=split,
        question_mode=question_mode,
        integration_mode=integration_mode,
        settings=settings,
        bundle=bundle,
        artifact=artifact,
        index=index,
        runtime=runtime,
        orchestrator=orchestrator,
        manifest=manifest,
        questions_file_sha256=(
            hashlib.sha256(questions_file.read_bytes()).hexdigest()
            if question_mode == "external" and questions_file is not None
            else None
        ),
        git_sha=git_sha,
        code_fingerprint=_code_fingerprint(),
    )


def _budget(question_count: int) -> dict:
    if type(question_count) is not int or question_count < 1:
        raise Stage6DevError("QUESTION_COUNT_INVALID")
    qa_call = (
        Decimal(QA_INPUT_TOKENS) * PEAK_INPUT_USD_PER_MILLION
        + Decimal(PROVIDER_DEFAULT_OUTPUT_TOKENS)
        * PEAK_OUTPUT_USD_PER_MILLION
    ) / Decimal(1_000_000)
    reasoning_call = (
        Decimal(STATE_REASONING_INPUT_TOKENS) * PEAK_INPUT_USD_PER_MILLION
        + Decimal(PROVIDER_DEFAULT_OUTPUT_TOKENS)
        * PEAK_OUTPUT_USD_PER_MILLION
    ) / Decimal(1_000_000)
    qa_calls = question_count * 3
    reasoning_calls = question_count
    total = qa_call * qa_calls + reasoning_call * reasoning_calls
    return {
        "max_new_http_attempts": qa_calls + reasoning_calls,
        "qa_calls": qa_calls,
        "state_reasoning_calls": reasoning_calls,
        "qa_input_tokens_per_call": QA_INPUT_TOKENS,
        "state_reasoning_input_tokens_per_call": STATE_REASONING_INPUT_TOKENS,
        "provider_default_output_tokens_per_call": PROVIDER_DEFAULT_OUTPUT_TOKENS,
        "worst_case_usd_per_qa_call": format(qa_call, ".7f"),
        "worst_case_usd_per_reasoning_call": format(reasoning_call, ".7f"),
        "worst_case_usd_total": format(total, ".7f"),
        "retry_attempts": 0,
    }


def _plan(system: System) -> dict:
    question_count = len(system.manifest.records)
    identity = {
        "sample_id": system.sample_id,
        "split": system.split,
        "question_mode": system.question_mode,
        "integration_mode": system.integration_mode,
        "question_count": question_count,
        "questions_file_sha256": system.questions_file_sha256,
        "dataset_sha256": system.bundle.snapshot_sha256,
        "artifact_build_id": system.artifact.manifest.build_id,
        "memory_set_fingerprint": (
            system.artifact.manifest.memory_set_fingerprint
        ),
        "index_fingerprint": system.index.index_fingerprint,
        "retriever_fingerprint": system.index.retriever_fingerprint,
        "embedding_model_revision": MODEL_REVISION,
        "questions": [
            {
                "query_id": record.query_id,
                "sample_id": record.sample_id,
                "original_qa_index": record.original_qa_index,
                "question": record.question,
                "gold_record_key": record.gold_record_key,
            }
            for record in system.manifest.records
        ],
        "methods": list(METHODS),
        "runtime_config_fingerprint": system.runtime.config_fingerprint,
        "execution_policy_fingerprint": (
            system.runtime.execution_policy_fingerprint
        ),
        "git_sha": system.git_sha,
        "code_fingerprint": system.code_fingerprint,
    }
    return {
        "format_version": 1,
        "status": "STAGE6_DEV_THREE_METHOD_PLAN_READY",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "run_fingerprint": _fp(identity),
        "identity": identity,
        "provider": {
            "model_id": "deepseek-flash",
            "thinking": "enabled",
            "reasoning_effort": "high",
            "max_tokens_omitted": True,
            "max_concurrency": 1,
            "retries": 0,
        },
        "budget": _budget(question_count),
        "network_calls": 0,
        "requires_user_approval": True,
    }


def _load_approval(path: Path) -> ThreeMethodApproval:
    try:
        return ThreeMethodApproval.model_validate_json(path.read_bytes())
    except (OSError, UnicodeError, ValidationError):
        raise Stage6DevError("APPROVAL_INVALID") from None


def _validate_approval(
    approval: ThreeMethodApproval,
    plan: dict,
    approval_path: Path,
    output_dir: Path,
) -> None:
    now = datetime.now(timezone.utc)
    budget = plan["budget"]
    if (
        approval.run_fingerprint != plan["run_fingerprint"]
        or approval.sample_id != plan["identity"]["sample_id"]
        or approval.authorized_methods != METHODS
        or approval.max_new_http_attempts
        != budget["max_new_http_attempts"]
        or approval.state_reasoning_calls != budget["state_reasoning_calls"]
        or approval.qa_calls != budget["qa_calls"]
        or approval.max_total_usd
        != Decimal(budget["worst_case_usd_total"])
        or approval.approved_at.tzinfo is None
        or approval.expires_at.tzinfo is None
        or approval.approved_at > now
        or approval.expires_at <= now
        or approval.price_checked_on != now.date()
        or approval_path.resolve().is_relative_to(output_dir.resolve())
    ):
        raise Stage6DevError("APPROVAL_MISMATCH")


def _execute_methods(
    system: System,
    records,
    output_dir: Path,
    client=None,
) -> dict:
    receipts: dict[str, PredictionReceipt] = {}
    rows = {}
    for method in METHODS:
        responses = []
        for record in records:
            if client is not None:
                client.set_context(method, record.query_id)
            prepared = system.orchestrator.prepare_query(record.to_query_request())
            response = system.orchestrator.run_prepared(prepared, method)
            if response.answer_result.status == "error":
                detail = (
                    response.answer_result.error.code
                    if response.answer_result.error is not None
                    else "UNKNOWN"
                )
                raise Stage6DevError(f"METHOD_RESPONSE_ERROR:{detail}")
            responses.append(response)
        receipts[method] = save_predictions(
            output_dir / f"{method}.predictions.json",
            system.manifest,
            responses,
            method,
        )
        rows[method] = verify_predictions(
            output_dir / f"{method}.predictions.json",
            system.manifest,
            receipts[method],
        )
    return {
        "receipts": receipts,
        "rows": rows,
    }


def _run_live(
    system: System,
    client,
    output_dir: Path,
) -> dict:
    result = _execute_methods(
        system,
        system.manifest.records,
        output_dir,
        client,
    )
    if client.failed:
        raise Stage6DevError("LIVE_HTTP_CALL_FAILED")
    calculations = []
    for index, record in enumerate(system.manifest.records):
        vector = result["rows"]["vector_rag"][index]
        dynamic = result["rows"]["dynamic"][index]
        calculations.append({
            "query_id": record.query_id,
            "same_query_analysis": vector.query_analysis == dynamic.query_analysis,
            "same_retrieved": vector.retrieved == dynamic.retrieved,
            "vector_candidate_count": (
                len(vector.retrieved.items)
                if vector.retrieved is not None else None
            ),
            "dynamic_candidate_count": (
                len(dynamic.retrieved.items)
                if dynamic.retrieved is not None else None
            ),
            "dynamic_decisions": (
                len(dynamic.resolution.decisions)
                if dynamic.resolution is not None else None
            ),
        })
    if not all(
        item["same_query_analysis"] and item["same_retrieved"]
        for item in calculations
    ):
        raise Stage6DevError("VECTOR_DYNAMIC_CANDIDATE_MISMATCH")
    statuses = {
        method: {
            status: sum(
                row.answer_result.status == status
                for row in result["rows"][method]
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
        "status": "STAGE6_DEV_THREE_METHOD_LIVE_COMPLETE",
        "network_calls": client.attempts,
        "retry_attempts": 0,
        "prediction_sha256": {
            method: receipt.file_sha256
            for method, receipt in result["receipts"].items()
        },
        "answer_status_counts": statuses,
        "candidate_comparison": calculations,
    }


def _run_dry(system: System, client, output_dir: Path) -> dict:
    result = _execute_methods(
        system,
        system.manifest.records,
        output_dir,
    )
    comparisons = []
    for index, record in enumerate(system.manifest.records):
        vector = result["rows"]["vector_rag"][index]
        dynamic = result["rows"]["dynamic"][index]
        comparisons.append({
            "query_id": record.query_id,
            "same_query_analysis": vector.query_analysis == dynamic.query_analysis,
            "same_retrieved": vector.retrieved == dynamic.retrieved,
            "vector_candidate_count": (
                len(vector.retrieved.items)
                if vector.retrieved is not None else None
            ),
            "dynamic_candidate_count": (
                len(dynamic.retrieved.items)
                if dynamic.retrieved is not None else None
            ),
            "dynamic_decisions": (
                len(dynamic.resolution.decisions)
                if dynamic.resolution is not None else None
            ),
        })
    if not all(
        item["same_query_analysis"] and item["same_retrieved"]
        for item in comparisons
    ):
        raise Stage6DevError("VECTOR_DYNAMIC_CANDIDATE_MISMATCH")
    return {
        "status": "STAGE6_DEV_THREE_METHOD_DRY_RUN_COMPLETE",
        "network_calls": 0,
        "fake_provider_invocations": len(client.calls),
        "prediction_sha256": {
            method: receipt.file_sha256
            for method, receipt in result["receipts"].items()
        },
        "candidate_comparison": comparisons,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("plan", "dry-run", "live"),
        required=True,
    )
    parser.add_argument(
        "--sample",
        choices=ALLOWED_SAMPLES,
        required=True,
    )
    parser.add_argument(
        "--question-mode",
        choices=("fixed", "full", "external"),
        default="fixed",
    )
    parser.add_argument(
        "--integration-mode",
        choices=("legacy", "v2"),
        default="legacy",
    )
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--questions-file", type=Path)
    parser.add_argument("--approval", type=Path)
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=DEFAULT_ARTIFACT_ROOT,
    )
    parser.add_argument(
        "--index-root",
        type=Path,
        default=DEFAULT_INDEX_ROOT,
    )
    args = parser.parse_args()
    try:
        dry = _DynamicDryRunClient()
        planning_system = _make_system(
            sample_id=args.sample,
            question_mode=args.question_mode,
            integration_mode=args.integration_mode,
            artifact_root=args.artifact_root,
            index_root=args.index_root,
            questions_file=args.questions_file,
            client=dry,
        )
        plan = _plan(planning_system)
        if args.mode == "live":
            if args.approval is None:
                raise Stage6DevError("APPROVAL_REQUIRED")
            approval = _load_approval(args.approval)
            _validate_approval(
                approval,
                plan,
                args.approval,
                args.output_dir,
            )
            api_key = _read_api_key()
            if api_key is None:
                raise Stage6DevError("CREDENTIAL_REQUIRED")
            output_dir = _new_output_dir(args.output_dir)
            sdk = _new_sdk_client(
                api_key,
                planning_system.settings.models.request_timeout_seconds,
            )
            client = _BudgetedClient(sdk, approval, output_dir)
            system = _make_system(
                sample_id=args.sample,
                question_mode=args.question_mode,
                integration_mode=args.integration_mode,
                artifact_root=args.artifact_root,
                index_root=args.index_root,
                questions_file=args.questions_file,
                client=client,
            )
            if _plan(system)["run_fingerprint"] != plan["run_fingerprint"]:
                raise Stage6DevError("RUN_FINGERPRINT_CHANGED")
            summary = _run_live(system, client, output_dir)
            _write_json(output_dir / "run_summary.json", summary)
            _write_json(output_dir / "plan.json", plan)
            print(_canonical(summary))
            return 0
        output_dir = _new_output_dir(args.output_dir)
        _write_json(output_dir / "plan.json", plan)
        if args.mode == "dry-run":
            summary = _run_dry(planning_system, dry, output_dir)
            _write_json(output_dir / "dry_run.json", summary)
            print(_canonical(summary))
            return 0
        print(_canonical({
            "status": plan["status"],
            "run_fingerprint": plan["run_fingerprint"],
            "budget": plan["budget"],
        }))
        return 0
    except Exception as exc:
        code = getattr(exc, "code", None)
        print(json.dumps({
            "status": code if isinstance(code, str) else "STAGE6_DEV_STOPPED",
            "error_type": type(exc).__name__,
        }))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
