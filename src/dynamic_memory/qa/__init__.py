"""D's evidence-constrained answer port."""

from .dynamic import DynamicAnswerer
from .benchmark_protocol import (
    BenchmarkProtocolError,
    BenchmarkQuestion,
    HybridContext,
    build_hybrid_context,
    context_refs,
    render_question,
)

__all__ = [
    "BenchmarkProtocolError",
    "BenchmarkQuestion",
    "DynamicAnswerer",
    "HybridContext",
    "build_hybrid_context",
    "context_refs",
    "render_question",
]
