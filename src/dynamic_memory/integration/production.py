"""Explicit production composition for A/B/C/D and E.

The factory is read-only during startup. A paid QA request can occur only
after the user explicitly submits a query in the Web application.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import os
import subprocess
from pathlib import Path

from dynamic_memory.baselines.full_text import FullTextAnswerer, FullTextProvenance
from dynamic_memory.data.loader import load_conversations
from dynamic_memory.data.repository import SourceRepository
from dynamic_memory.llm.token_estimate import ESTIMATOR_VERSION, estimate_messages_tokens
from dynamic_memory.pipeline.query_runner import QueryOrchestrator
from dynamic_memory.pipeline.runtime import LLMRuntime, create_runtime
from dynamic_memory.pipeline.settings import AppSettings, load_settings
from dynamic_memory.reasoning import DynamicProvenance, DynamicServicePorts
from dynamic_memory.retrieval import (
    EmbeddingConfig, IndexManager, LocalIndexEncoder, QUERY_ANALYSIS_VERSION,
    VectorAnswerProvenance, analyze_query,
)
from dynamic_memory.retrieval.service_ports import RetrievalServicePorts
from dynamic_memory.services.dynamic_memory_service import (
    DynamicMemoryService, IndexReadiness,
)
from dynamic_memory.web.bindings import WebBindings

from .artifacts import ArtifactPorts


class ProductionConfigurationError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ProductionWebService:
    """Expose only the ordered samples configured for this production session."""

    def __init__(
        self,
        service: DynamicMemoryService,
        sample_ids: str | Sequence[str] = "conv-30",
    ) -> None:
        self._service = service
        values = (sample_ids,) if isinstance(sample_ids, str) else tuple(sample_ids)
        if (
            not values
            or any(type(value) is not str or not value.strip() for value in values)
            or len(set(values)) != len(values)
        ):
            raise ProductionConfigurationError("SAMPLE_CONFIGURATION_INVALID")
        self._sample_ids = values

    def _require_sample(self, sample_id: str) -> None:
        if sample_id not in self._sample_ids:
            raise ProductionConfigurationError("SAMPLE_NOT_EXPOSED")

    def list_samples(self) -> list[str]:
        return list(self._sample_ids)

    def list_memories(self, sample_id: str):
        self._require_sample(sample_id)
        return self._service.list_memories(sample_id)

    def get_memory(self, sample_id: str, memory_id: str):
        self._require_sample(sample_id)
        return self._service.get_memory(sample_id, memory_id)

    def get_turn(self, sample_id: str, ref):
        self._require_sample(sample_id)
        return self._service.get_turn(sample_id, ref)

    def get_build_status(self, sample_id: str):
        self._require_sample(sample_id)
        return self._service.get_build_status(sample_id)

    def get_query_state(self, sample_id: str) -> str:
        self._require_sample(sample_id)
        return self._service.get_query_state(sample_id)

    def run_query(self, request, method: str = "dynamic"):
        self._require_sample(request.sample_id)
        return self._service.run_query(request, method)


def _environment_path(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ProductionConfigurationError("PRODUCTION_CONFIGURATION_REQUIRED")
    return path.resolve()


def _path(name: str) -> Path:
    value = _environment_path(name)
    if value is None:
        raise ProductionConfigurationError("PRODUCTION_CONFIGURATION_REQUIRED")
    return value


def _configured_samples() -> tuple[str, ...]:
    raw = os.environ.get("SRE_PRODUCTION_SAMPLE_IDS", "").strip()
    if raw:
        values = tuple(value.strip() for value in raw.split(",") if value.strip())
    else:
        value = os.environ.get("SRE_PRODUCTION_SAMPLE_ID", "").strip()
        values = (value or "conv-30",)
    if not values or len(set(values)) != len(values):
        raise ProductionConfigurationError("SAMPLE_CONFIGURATION_INVALID")
    return values


def _configured_index_paths(sample_ids: Sequence[str]) -> dict[str, Path]:
    root = _environment_path("SRE_PRODUCTION_INDEX_ROOT")
    single = _environment_path("SRE_PRODUCTION_INDEX_PATH")
    if root is not None and single is not None:
        raise ProductionConfigurationError("PRODUCTION_CONFIGURATION_CONFLICT")
    if root is not None:
        return {sample_id: root / f"{sample_id}.json" for sample_id in sample_ids}
    if single is not None:
        if len(sample_ids) != 1:
            raise ProductionConfigurationError("PRODUCTION_CONFIGURATION_REQUIRED")
        return {sample_ids[0]: single}
    raise ProductionConfigurationError("PRODUCTION_CONFIGURATION_REQUIRED")


def _configured_splits() -> tuple[str, ...]:
    raw = os.environ.get("SRE_PRODUCTION_ALLOWED_SPLITS", "").strip()
    values = (
        tuple(value.strip() for value in raw.split(",") if value.strip())
        if raw
        else ("main_dev",)
    )
    if (
        not values
        or len(set(values)) != len(values)
        or any(value not in ("main_dev", "extra_dev", "test") for value in values)
    ):
        raise ProductionConfigurationError("SPLIT_CONFIGURATION_INVALID")
    return values


def _api_key() -> str | None:
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
    configured = os.environ.get("SRE_PRODUCTION_GIT_SHA", "").strip()
    if len(configured) == 40 and all(char in "0123456789abcdef" for char in configured):
        return configured
    try:
        value = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[3],
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        raise ProductionConfigurationError("GIT_IDENTITY_UNAVAILABLE") from None
    if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
        raise ProductionConfigurationError("GIT_IDENTITY_UNAVAILABLE")
    return value


def create_system(
    *,
    artifact_root: Path,
    index_path: Path,
    model_root: Path,
    model_revision: str,
    sample_id: str = "conv-30",
    settings: AppSettings | None = None,
    runtime: LLMRuntime | None = None,
) -> DynamicMemoryService:
    """Compose one verified sample while preserving the legacy entry point."""
    return create_multi_sample_system(
        artifact_root=artifact_root,
        index_paths={sample_id: index_path},
        model_root=model_root,
        model_revision=model_revision,
        sample_ids=(sample_id,),
        allowed_splits=("main_dev",),
        settings=settings,
        runtime=runtime,
    )


def create_multi_sample_system(
    *,
    artifact_root: Path,
    index_paths: Mapping[str, Path],
    model_root: Path,
    model_revision: str,
    sample_ids: Sequence[str] | None = None,
    allowed_splits: Sequence[str] = ("main_dev",),
    settings: AppSettings | None = None,
    runtime: LLMRuntime | None = None,
) -> DynamicMemoryService:
    """Compose verified artifacts, per-sample indexes and the shared A/D ports."""
    configured = tuple(sample_ids or tuple(index_paths))
    if (
        not configured
        or any(type(sample_id) is not str or not sample_id for sample_id in configured)
        or len(set(configured)) != len(configured)
        or set(configured) != set(index_paths)
    ):
        raise ProductionConfigurationError("SAMPLE_CONFIGURATION_INVALID")
    split_values = tuple(allowed_splits)
    if (
        not split_values
        or len(set(split_values)) != len(split_values)
        or any(value not in ("main_dev", "extra_dev", "test") for value in split_values)
    ):
        raise ProductionConfigurationError("SPLIT_CONFIGURATION_INVALID")
    paths_by_sample = {
        sample_id: Path(index_paths[sample_id]).expanduser().resolve()
        for sample_id in configured
    }
    settings = settings or load_settings(
        models_profile=Path(__file__).resolve().parents[3] / "configs/models_high_pilot.yaml"
    )
    raw = Path(__file__).resolve().parents[3] / "data/raw/locomo10.json"
    split = Path(__file__).resolve().parents[3] / "configs/data_split.yaml"
    bundle = load_conversations(str(raw), str(split))
    if any(
        bundle.split_by_sample_id.get(sample_id) not in split_values
        for sample_id in configured
    ):
        raise ProductionConfigurationError("SPLIT_MISMATCH")
    repository = SourceRepository(bundle)
    artifacts = ArtifactPorts(
        artifact_root,
        bundle,
        settings,
        production=True,
        allowed_splits=split_values,
    )
    for sample_id in configured:
        artifacts.load(sample_id)

    encoder = LocalIndexEncoder(EmbeddingConfig(
        str(model_root),
        model_revision,
        query_prompt="Represent this sentence for searching relevant passages: ",
        document_prompt="",
    ))
    retrieval = settings.retrieval.model_copy(
        update={"embedding_model": encoder.identity.model_name}
    )
    if runtime is None:
        api_key = _api_key()
        runtime = create_runtime(
            settings,
            backend="deepseek",
            api_enabled=api_key is not None,
            api_key=api_key,
            model_id="deepseek-flash",
            token_estimator=estimate_messages_tokens,
            estimator_version=ESTIMATOR_VERSION,
            strict_budget=True,
            cache_enabled=False,
        )
    if (
        runtime.settings.models.model_id != "deepseek-flash"
        or runtime.settings.models.thinking != "enabled"
        or runtime.settings.models.reasoning_effort != "high"
        or not runtime.settings.models.qa_omit_max_tokens
    ):
        raise ProductionConfigurationError("QA_PROFILE_UNSAFE")

    git_sha = _git_sha()
    full = FullTextAnswerer(
        runtime,
        FullTextProvenance(
            "sha256:" + bundle.snapshot_sha256,
            QUERY_ANALYSIS_VERSION,
            git_sha,
            "development",
        ),
    )
    ports = RetrievalServicePorts(
        encoder=encoder,
        settings=retrieval,
        load_artifact=artifacts.load,
        index_path=paths_by_sample.__getitem__,
        readiness_factory=IndexReadiness,
        runtime=runtime,
        provenance_factory=lambda artifact: VectorAnswerProvenance(
            "sha256:" + artifact.manifest.dataset_snapshot_sha256,
            QUERY_ANALYSIS_VERSION,
            git_sha,
            "development",
        ),
        source_repository=repository,
        plain_text=True,
        synthetic_test_mode=False,
    )

    def provenance_for_sample(requested_sample: str):
        artifact = artifacts.load(requested_sample)
        index = IndexManager(encoder).load_index(
            paths_by_sample[requested_sample], artifact, retrieval
        )
        return DynamicProvenance(
            "sha256:" + artifact.manifest.dataset_snapshot_sha256,
            QUERY_ANALYSIS_VERSION,
            git_sha,
            "development",
            index.index_fingerprint,
        )

    def conversation_cutoff(requested_sample: str):
        source = repository.get_sample(requested_sample)
        timestamps = [
            turn.recorded_at
            for session in source.sessions for turn in session.turns
            if turn.recorded_at is not None
        ]
        return max(timestamps) if timestamps else None

    dynamic = DynamicServicePorts(
        runtime,
        provenance_factory=provenance_for_sample,
        conversation_cutoff=conversation_cutoff,
        source_repository=repository,
        plain_text=True,
        conditional_state=True,
        synthetic_test_mode=False,
    )
    orchestrator = QueryOrchestrator(
        analyze_query=analyze_query,
        get_sample=repository.get_sample,
        load_verified_index=ports.load_verified_index,
        retrieve=ports.retrieve,
        retrieve_raw_topk=ports.retrieve_raw_topk,
        load_candidates=ports.load_candidates,
        answer_full_text=full,
        answer_vector_rag=ports.answer_vector_rag,
        resolve=dynamic.resolve,
        answer_dynamic=dynamic.answer_dynamic,
    ).with_source_lookup(repository.get_sample)
    return DynamicMemoryService(
        sources=repository,
        build_status=artifacts.build_status,
        load_verified_artifact=artifacts.verified,
        index_readiness=ports.index_readiness,
        query_orchestrator=orchestrator,
    )


def create_web_bindings() -> WebBindings:
    revision = os.environ.get("SRE_PRODUCTION_MODEL_REVISION", "").strip()
    if not revision:
        raise ProductionConfigurationError("PRODUCTION_CONFIGURATION_REQUIRED")
    sample_ids = _configured_samples()
    service = create_multi_sample_system(
        artifact_root=_path("SRE_PRODUCTION_ARTIFACT_ROOT"),
        index_paths=_configured_index_paths(sample_ids),
        model_root=_path("SRE_PRODUCTION_MODEL_ROOT"),
        model_revision=revision,
        sample_ids=sample_ids,
        allowed_splits=_configured_splits(),
    )
    sample_label = (
        sample_ids[0]
        if len(sample_ids) == 1
        else f"{len(sample_ids)} 个样本"
    )
    return WebBindings(
        service=ProductionWebService(service, sample_ids),
        label=f"SRE-Project · {sample_label} · DeepSeek V4.1 Flash",
        synthetic=False,
        dynamic_available=True,
    )
