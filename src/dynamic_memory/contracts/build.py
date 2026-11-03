"""IC-002 public, immutable build report shared by B and its consumers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .models import MemoryRecord, SourceRef


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

    def __post_init__(self) -> None:
        if (not self.extraction_window_id or
                not isinstance(self.source_refs, tuple) or
                any(type(ref) is not SourceRef for ref in self.source_refs) or
                self.status not in ("success", "failed") or
                self.stage not in ("planning", "extraction", "review", "finalization") or
                any(type(n) is not int or n < 0 for n in (
                    self.input_fact_count, self.accepted_fact_count,
                    self.rejected_fact_count, self.output_memory_count))):
            raise ValueError("invalid window diagnostic")
        if self.status == "success":
            if (self.error_code is not None or self.reason_code is not None or
                    self.accepted_fact_count + self.rejected_fact_count != self.input_fact_count):
                raise ValueError("successful window diagnostic is inconsistent")
        elif not self.error_code or self.output_memory_count:
            raise ValueError("failed window diagnostic is inconsistent")


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
        if (not isinstance(self.sample_id, str) or not self.sample_id.strip() or
                not isinstance(self.extractor_version, str) or not self.extractor_version.strip() or
                not isinstance(self.memories, tuple) or
                any(type(memory) is not MemoryRecord or memory.sample_id != self.sample_id
                    for memory in self.memories) or
                type(self.total_windows) is not int or type(self.successful_windows) is not int or
                not 0 <= self.successful_windows <= self.total_windows or
                not isinstance(self.window_diagnostics, tuple) or
                any(type(diagnostic) is not WindowDiagnostic for diagnostic in self.window_diagnostics)):
            raise ValueError("invalid build report")
        if self.total_windows != len(self.window_diagnostics):
            raise ValueError("window diagnostic count mismatch")
        successes = sum(d.status == "success" for d in self.window_diagnostics)
        if self.successful_windows != successes:
            raise ValueError("successful window count mismatch")
        failures = self.total_windows - successes
        expected = "complete" if not failures else "partial" if successes else "failed"
        if self.build_status != expected or self.partial_build is not bool(failures):
            raise ValueError("build status mismatch")
        if self.build_status == "failed" and self.memories:
            raise ValueError("failed build cannot contain memories")
