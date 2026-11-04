"""Sample-bound D ports for A; no hidden model creation or mutable query state."""

from __future__ import annotations

from .common import DynamicProvenance, DynamicReasoningError, checked_inputs
from .resolver import StateResolver


class DynamicServicePorts:
    """Provenance and cutoff are supplied by verified upstream composition."""

    def __init__(
        self,
        runtime,
        *,
        provenance_factory,
        conversation_cutoff=None,
        synthetic_test_mode=False,
        on_diagnostic=None,
    ):
        if not callable(provenance_factory):
            raise DynamicReasoningError("DEPENDENCY_INVALID")
        if conversation_cutoff is not None and not callable(conversation_cutoff):
            raise DynamicReasoningError("DEPENDENCY_INVALID")
        if on_diagnostic is not None and not callable(on_diagnostic):
            raise DynamicReasoningError("DEPENDENCY_INVALID")
        self.runtime = runtime
        self.provenance_factory = provenance_factory
        self.cutoff = conversation_cutoff
        self.synthetic = synthetic_test_mode
        self.on_diagnostic = on_diagnostic

    def _kwargs(self, request):
        try:
            provenance = self.provenance_factory(request.sample_id)
            if type(provenance) is not DynamicProvenance:
                raise ValueError("invalid provenance")
        except Exception:
            raise DynamicReasoningError("PROVENANCE_INVALID") from None
        return {
            "provenance": provenance,
            "synthetic_test_mode": self.synthetic,
            "on_diagnostic": self.on_diagnostic,
        }

    def resolve(self, request, analysis, candidates):
        request, analysis, candidates = checked_inputs(request, analysis, candidates)
        return StateResolver(
            self.runtime,
            conversation_cutoff=self.cutoff,
            **self._kwargs(request),
        ).resolve(request, analysis, candidates)

    def answer_dynamic(self, request, analysis, resolution, candidates):
        from dynamic_memory.qa import DynamicAnswerer

        request, analysis, candidates = checked_inputs(request, analysis, candidates)
        return DynamicAnswerer(
            self.runtime, **self._kwargs(request)
        ).answer_dynamic(request, analysis, resolution, candidates)
