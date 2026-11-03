"""IC-002 has one public report type and coherent window outcomes."""

import pytest

from dynamic_memory.contracts import MemoryBuildResult, WindowDiagnostic
from dynamic_memory.memory.build import MemoryBuildResult as BuilderResult
from dynamic_memory.memory.build import WindowDiagnostic as BuilderDiagnostic


def test_public_build_types_are_the_builder_types():
    assert MemoryBuildResult is BuilderResult
    assert WindowDiagnostic is BuilderDiagnostic
    empty = MemoryBuildResult("sample", (), "complete", False, 0, 0, (), "mock-v1")
    assert empty.memories == () and empty.total_windows == 0


def test_report_states_and_window_counts():
    good = WindowDiagnostic("w1", (), "success", None, None, 1, 1, 0, 1, "review")
    bad = WindowDiagnostic("w2", (), "failed", "MODEL_TIMEOUT", None, 0, 0, 0, 0, "extraction")
    assert MemoryBuildResult("sample", (), "complete", False, 1, 1, (good,), "mock-v1").build_status == "complete"
    assert MemoryBuildResult("sample", (), "partial", True, 2, 1, (good, bad), "mock-v1").build_status == "partial"
    assert MemoryBuildResult("sample", (), "failed", True, 1, 0, (bad,), "mock-v1").build_status == "failed"
    with pytest.raises(ValueError, match="successful window count mismatch"):
        MemoryBuildResult("sample", (), "complete", False, 1, 0, (good,), "mock-v1")
    with pytest.raises(ValueError, match="failed window diagnostic"):
        WindowDiagnostic("w3", (), "failed", None, None, 0, 0, 0, 0, "review")
