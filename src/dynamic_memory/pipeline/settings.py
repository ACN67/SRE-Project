"""Offline, typed loading of the six repository YAML configurations.

Loading settings has no model, network, environment-variable or filesystem-write
side effects. Callers explicitly inject the returned AppSettings into later services.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PositiveInt, ValidationError, model_validator


class SettingsError(ValueError):
    """A named configuration file cannot be loaded or validated."""


class SettingsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ModelsSettings(SettingsModel):
    default_backend: Literal["mock", "deepseek"]
    api_enabled_by_default: bool
    provider_under_consideration: str
    model_id: str | None
    thinking: Literal["disabled", "enabled"]
    temperature: float = Field(ge=0, le=2)
    extraction_max_input_tokens: PositiveInt
    extraction_max_output_tokens: PositiveInt
    reasoning_max_input_tokens: PositiveInt
    reasoning_max_output_tokens: PositiveInt
    qa_max_input_tokens: PositiveInt
    qa_max_output_tokens: PositiveInt
    request_timeout_seconds: PositiveInt
    max_retries: int = Field(ge=0)
    max_concurrent_api_requests: PositiveInt

    @model_validator(mode="after")
    def offline_default(self) -> ModelsSettings:
        if self.default_backend != "mock" or self.api_enabled_by_default:
            raise ValueError("A-01 requires default_backend=mock and api_enabled_by_default=false")
        return self


class SplitGroup(SettingsModel):
    source_order_1_based: list[PositiveInt] = Field(min_length=1)
    sample_ids: list[str] | None

    @model_validator(mode="after")
    def matching_ids(self) -> SplitGroup:
        if len(set(self.source_order_1_based)) != len(self.source_order_1_based):
            raise ValueError("source_order_1_based contains duplicates")
        if self.sample_ids is not None and len(self.sample_ids) != len(self.source_order_1_based):
            raise ValueError("sample_ids length must match source_order_1_based")
        return self


class SplitGroups(SettingsModel):
    main_dev: SplitGroup
    extra_dev: SplitGroup
    test: SplitGroup


class DataSplitSettings(SettingsModel):
    snapshot_sha256: str | None
    sample_ids: list[str] | None
    groups: SplitGroups


class EvaluationSettings(SettingsModel):
    main_methods: list[Literal["full_text", "vector_rag", "dynamic"]] = Field(min_length=3)
    optional_ablation: Literal["pure_vector_topk"]
    qa_scoring: str = Field(min_length=1)
    gold_access: Literal["evaluation_only_after_prediction_saved"]
    formal_results_status: Literal["not_run", "completed"]
    recall_at_k: list[PositiveInt] = Field(min_length=1)

    @model_validator(mode="after")
    def main_methods_unique(self) -> EvaluationSettings:
        if set(self.main_methods) != {"full_text", "vector_rag", "dynamic"} or len(self.main_methods) != 3:
            raise ValueError("main_methods must contain each of the three main methods once")
        return self


class ExtractionSettings(SettingsModel):
    source_field: Literal["conversation"]
    exclude_fields: list[str]
    prefer_session_windows: bool
    window_max_input_tokens: PositiveInt
    window_overlap_turns: int = Field(ge=0)
    image_caption_origin: Literal["blip_caption"]
    real_extraction_implemented: bool


class RetrievalSettings(SettingsModel):
    base_top_k: PositiveInt
    max_candidates: PositiveInt
    recall_at_k: list[PositiveInt] = Field(min_length=1)
    embedding_model: str | None
    main_comparison_candidates_shared: bool
    optional_pure_vector_top_k: PositiveInt
    real_retrieval_implemented: bool

    @model_validator(mode="after")
    def candidate_budget(self) -> RetrievalSettings:
        if self.max_candidates < self.base_top_k:
            raise ValueError("max_candidates must be >= base_top_k")
        return self


class ReasoningSettings(SettingsModel):
    query_intents: list[Literal["current_state", "historical_state", "state_history", "event_fact", "general_fact", "unknown"]] = Field(min_length=1)
    time_modes: list[Literal["latest_known", "as_of", "unknown", "conflict"]] = Field(min_length=1)
    decision_actions: list[Literal["RETAIN", "EXCLUDE", "UNRESOLVED"]] = Field(min_length=1)
    relation_types: list[Literal["UPDATE", "SUPERSEDE", "CONFLICT"]] = Field(min_length=1)
    real_reasoning_implemented: bool


class AppSettings(SettingsModel):
    models: ModelsSettings
    data_split: DataSplitSettings
    evaluation: EvaluationSettings
    extraction: ExtractionSettings
    retrieval: RetrievalSettings
    reasoning: ReasoningSettings

    @model_validator(mode="after")
    def shared_recall_budget(self) -> AppSettings:
        if self.evaluation.recall_at_k != self.retrieval.recall_at_k:
            raise ValueError("evaluation.yaml recall_at_k must match retrieval.yaml recall_at_k")
        return self


_FILES: dict[str, type[SettingsModel]] = {
    "models": ModelsSettings,
    "data_split": DataSplitSettings,
    "evaluation": EvaluationSettings,
    "extraction": ExtractionSettings,
    "retrieval": RetrievalSettings,
    "reasoning": ReasoningSettings,
}


def _read_config(path: Path, schema: type[SettingsModel]) -> SettingsModel:
    try:
        contents = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SettingsError(f"{path.name}: configuration file not found") from None
    except UnicodeError:
        raise SettingsError(f"{path.name}: file is not valid UTF-8") from None
    except OSError as exc:
        raise SettingsError(f"{path.name}: cannot read configuration file ({type(exc).__name__})") from None
    try:
        document = yaml.safe_load(contents)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f" line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise SettingsError(f"{path.name}: invalid YAML{location}") from None
    if not isinstance(document, dict):
        raise SettingsError(f"{path.name}: YAML root must be a Mapping")
    try:
        return schema.model_validate(document)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(map(str, error['loc'])) or '<root>'}: {error['msg']}"
            for error in exc.errors(include_input=False)
        )
        raise SettingsError(f"{path.name}: {details}") from None


def load_settings(config_dir: str | Path | None = None) -> AppSettings:
    """Read and validate six YAML files without relying on the process cwd."""
    directory = Path(config_dir) if config_dir is not None else Path(__file__).resolve().parents[3] / "configs"
    sections = {name: _read_config(directory / f"{name}.yaml", schema) for name, schema in _FILES.items()}
    try:
        return AppSettings.model_validate(sections)
    except ValidationError as exc:
        details = "; ".join(error["msg"] for error in exc.errors(include_input=False))
        raise SettingsError(f"cross-config validation: {details}") from None
