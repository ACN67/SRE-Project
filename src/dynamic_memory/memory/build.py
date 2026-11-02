"""B-07: fail-closed orchestration of the B-04/05/06 memory pipeline.

The v2.2 build DTO is not yet present in shared contracts; these immutable B-local
results mirror its specified fields until the shared owner publishes it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError

from dynamic_memory.contracts import ConversationSample, MemoryRecord, SourceRef
from dynamic_memory.data.repository import SourceRepository, SourceRepositoryError
from dynamic_memory.llm import CallIdentity, RecordedLLMExecutor
from dynamic_memory.llm.execution import TokenEstimator
from dynamic_memory.memory.extraction import WindowExtractionError, extract_window
from dynamic_memory.memory.normalization import (
    EvidenceChecker, MemoryNormalizationError, merge_reviewed_memories, normalize_memories,
)
from dynamic_memory.memory.windows import ExtractionWindow, WindowPlanningError, plan_extraction_windows
from dynamic_memory.pipeline.settings import ExtractionSettings, ModelsSettings


@dataclass(frozen=True, slots=True)
class WindowDiagnostic:
    extraction_window_id: str
    source_refs: tuple[SourceRef, ...]
    status: Literal["success", "failed"]
    error_code: str | None
    reason_code: str | None
    input_fact_count: int
    accepted_fact_count: int
    rejected_fact_count: int
    output_memory_count: int
    stage: Literal["planning", "extraction", "review", "finalization"]


@dataclass(frozen=True, slots=True)
class MemoryBuildResult:
    sample_id: str
    memories: tuple[MemoryRecord, ...]
    build_status: Literal["complete", "partial", "failed"]
    partial_build: bool
    total_windows: int
    successful_windows: int
    window_diagnostics: tuple[WindowDiagnostic, ...]
    extractor_version: str

    def __post_init__(self) -> None:
        if self.total_windows != len(self.window_diagnostics):
            raise ValueError("window diagnostic count mismatch")
        successes = sum(d.status == "success" for d in self.window_diagnostics)
        if self.successful_windows != successes:
            raise ValueError("successful window count mismatch")
        failures = self.total_windows - successes
        expected = "complete" if not failures else "partial" if successes else "failed"
        if self.build_status != expected or self.partial_build != bool(failures):
            raise ValueError("build status mismatch")
        if self.build_status == "failed" and self.memories:
            raise ValueError("failed build cannot contain memories")


class MemoryBuildError(ValueError):
    def __init__(self, code: str, report: MemoryBuildResult) -> None:
        self.code = code
        self.report = report
        super().__init__(f"{code}: memory build is not complete")


class MemoryBuilder:
    """Inject repository, execution policy and evidence reviewer once per build runtime."""

    def __init__(
        self, repository: SourceRepository, executor: RecordedLLMExecutor,
        models_settings: ModelsSettings, extraction_settings: ExtractionSettings,
        identity_factory: Callable[[ExtractionWindow], CallIdentity], *,
        extractor_version: str, evidence_checker: EvidenceChecker | None = None,
        token_estimator: TokenEstimator | None = None,
        reserved_prompt_tokens: int = 1800,
    ) -> None:
        self.repository = repository
        self.executor = executor
        self.models_settings = models_settings
        self.extraction_settings = extraction_settings
        self.identity_factory = identity_factory
        self.extractor_version = extractor_version
        self.evidence_checker = evidence_checker
        self.token_estimator = token_estimator
        self.reserved_prompt_tokens = reserved_prompt_tokens

    def build_memories(self, sample: ConversationSample) -> list[MemoryRecord]:
        report = self.build_memories_with_report(sample)
        if report.build_status != "complete":
            code = "MEMORY_BUILD_FAILED" if report.build_status == "failed" else "MEMORY_BUILD_INCOMPLETE"
            raise MemoryBuildError(code, report)
        return list(report.memories)

    def build_memories_with_report(self, sample: ConversationSample) -> MemoryBuildResult:
        sample_id = sample.sample_id if isinstance(sample, ConversationSample) and isinstance(sample.sample_id, str) else "invalid_sample"
        diagnostics: list[WindowDiagnostic] = []
        reviewed: list[MemoryRecord] = []

        def failure(window_id: str, refs: tuple[SourceRef, ...], stage: str, code: str,
                    count: int = 0, reason: str | None = None) -> None:
            diagnostics.append(WindowDiagnostic(window_id, refs, "failed", code, reason,
                                                count, 0, count, 0, stage))

        try:
            if not isinstance(sample, ConversationSample) or not isinstance(self.repository, SourceRepository):
                raise ValueError("INVALID_DEPENDENCY")
            checked = ConversationSample.model_validate(sample.model_dump(mode="python", warnings="error"))
            if self.repository.get_sample(checked.sample_id) != checked:
                raise ValueError("SAMPLE_MISMATCH")
            if (not isinstance(self.executor, RecordedLLMExecutor)
                    or not callable(self.identity_factory)
                    or not isinstance(self.models_settings, ModelsSettings)
                    or not isinstance(self.extraction_settings, ExtractionSettings)
                    or not isinstance(self.extractor_version, str) or not self.extractor_version.strip()):
                raise ValueError("INVALID_DEPENDENCY")
            if self.extraction_settings.window_max_input_tokens != self.models_settings.extraction_max_input_tokens:
                raise ValueError("WINDOW_BUDGET_MISMATCH")
            windows = plan_extraction_windows(checked, self.extraction_settings,
                                              reserved_prompt_tokens=self.reserved_prompt_tokens)
        except (WindowPlanningError, SourceRepositoryError) as exc:
            failure("planning:" + sample_id, (), "planning", exc.code)
            return self._report(sample_id, (), diagnostics)
        except (ValidationError, ValueError, TypeError, AttributeError) as exc:
            code = str(exc) if isinstance(exc, ValueError) and str(exc) in {
                "INVALID_DEPENDENCY", "SAMPLE_MISMATCH", "WINDOW_BUDGET_MISMATCH"} else "INVALID_SAMPLE_OR_SETTINGS"
            failure("planning:" + sample_id, (), "planning", code)
            return self._report(sample_id, (), diagnostics)

        for window in windows:
            refs = window.source_refs
            try:
                identity = self.identity_factory(window)
                result = extract_window(window, self.executor, identity, self.models_settings,
                                        token_estimator=self.token_estimator)
            except WindowExtractionError as exc:
                failure(window.extraction_window_id, refs, "extraction", exc.code)
                continue
            except Exception:
                failure(window.extraction_window_id, refs, "extraction", "EXECUTION_DEPENDENCY_FAILED")
                continue
            try:
                memories = normalize_memories(sample_id, [result], self.repository,
                                              extractor_version=self.extractor_version,
                                              evidence_checker=self.evidence_checker)
            except MemoryNormalizationError as exc:
                failure(window.extraction_window_id, refs, "review", exc.code,
                        len(result.facts), exc.reason_code)
                continue
            except Exception:
                failure(window.extraction_window_id, refs, "review", "NORMALIZATION_FAILED", len(result.facts))
                continue
            reviewed.extend(memories)
            diagnostics.append(WindowDiagnostic(window.extraction_window_id, refs, "success", None, None,
                                                len(result.facts), len(result.facts), 0, len(memories), "review"))

        try:
            merged = merge_reviewed_memories(reviewed)
        except MemoryNormalizationError as exc:
            diagnostics = self._fail_finalization(diagnostics, exc.code, exc.reason_code)
            merged = []
        except Exception:
            diagnostics = self._fail_finalization(diagnostics, "MEMORY_INVALID", None)
            merged = []
        return self._report(sample_id, tuple(merged), diagnostics)

    @staticmethod
    def _fail_finalization(
        diagnostics: list[WindowDiagnostic], code: str, reason: str | None,
    ) -> list[WindowDiagnostic]:
        """Keep the real window cardinality when global finalization invalidates output."""
        return [
            WindowDiagnostic(
                item.extraction_window_id, item.source_refs, "failed", code, reason,
                item.input_fact_count, 0, item.input_fact_count, 0, "finalization",
            ) if item.status == "success" else item
            for item in diagnostics
        ]

    def _report(self, sample_id: str, memories: tuple[MemoryRecord, ...],
                diagnostics: list[WindowDiagnostic]) -> MemoryBuildResult:
        successes = sum(d.status == "success" for d in diagnostics)
        failures = len(diagnostics) - successes
        status = "complete" if not failures else "partial" if successes else "failed"
        return MemoryBuildResult(sample_id, memories if status != "failed" else (), status,
                                 bool(failures), len(diagnostics), successes,
                                 tuple(diagnostics), self.extractor_version if isinstance(self.extractor_version, str) else "unknown")
