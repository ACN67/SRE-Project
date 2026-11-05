"""Sample-bound D ports for A; no hidden model creation or mutable query state."""

from __future__ import annotations

import re

from dynamic_memory.contracts import MemoryDecision, ResolutionResult
from dynamic_memory.data.repository import SourceRepository
from dynamic_memory.llm.prompts import QA_PROMPT

from .common import DynamicProvenance, DynamicReasoningError, checked_inputs
from .resolver import StateResolver


_STATE_QUERY = re.compile(
    r"\b(current|currently|latest|still|now|changed?|update[ds]?|cancel(?:led|ed)?|"
    r"supersed(?:e|ed)|instead|as of|before|after|no longer|renamed|moved)\b",
    re.IGNORECASE,
)


def requires_state_resolution(request, analysis) -> bool:
    """Route only explicit state, change, or cutoff questions through D's resolver."""
    return (
        analysis.query_intent in {"current_state", "historical_state", "state_history"}
        or analysis.time_mode == "as_of"
        or bool(_STATE_QUERY.search(request.question))
    )


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
        source_repository: SourceRepository | None = None,
        source_metadata_lookup=None,
        question_transform=None,
        prompt_template=QA_PROMPT,
        plain_text=False,
        max_context_memories=12,
        conditional_state=False,
    ):
        if not callable(provenance_factory):
            raise DynamicReasoningError("DEPENDENCY_INVALID")
        if conversation_cutoff is not None and not callable(conversation_cutoff):
            raise DynamicReasoningError("DEPENDENCY_INVALID")
        if on_diagnostic is not None and not callable(on_diagnostic):
            raise DynamicReasoningError("DEPENDENCY_INVALID")
        if source_repository is not None and type(source_repository) is not SourceRepository:
            raise DynamicReasoningError("DEPENDENCY_INVALID")
        if source_metadata_lookup is not None and not callable(source_metadata_lookup):
            raise DynamicReasoningError("DEPENDENCY_INVALID")
        if question_transform is not None and not callable(question_transform):
            raise DynamicReasoningError("DEPENDENCY_INVALID")
        if (
            type(plain_text) is not bool
            or type(conditional_state) is not bool
            or type(max_context_memories) is not int
            or max_context_memories < 1
        ):
            raise DynamicReasoningError("DEPENDENCY_INVALID")
        self.runtime = runtime
        self.provenance_factory = provenance_factory
        self.cutoff = conversation_cutoff
        self.synthetic = synthetic_test_mode
        self.on_diagnostic = on_diagnostic
        self.source_repository = source_repository
        self.source_metadata_lookup = source_metadata_lookup
        self.question_transform = question_transform
        self.prompt_template = prompt_template
        self.plain_text = plain_text
        self.max_context_memories = max_context_memories
        self.conditional_state = conditional_state

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
        if self.conditional_state and not requires_state_resolution(request, analysis):
            return ResolutionResult(
                query_id=request.query_id,
                decisions=[
                    MemoryDecision(
                        memory_id=memory.memory_id,
                        action="RETAIN",
                        reason="No state update or temporal cutoff requires D resolution.",
                    )
                    for memory in candidates
                ],
                effective_memory_ids=[memory.memory_id for memory in candidates],
            )
        return StateResolver(
            self.runtime,
            conversation_cutoff=self.cutoff,
            **self._kwargs(request),
        ).resolve(request, analysis, candidates)

    def answer_dynamic(self, request, analysis, resolution, candidates):
        from dynamic_memory.qa import DynamicAnswerer

        request, analysis, candidates = checked_inputs(request, analysis, candidates)
        return DynamicAnswerer(
            self.runtime,
            source_repository=self.source_repository,
            source_metadata_lookup=self.source_metadata_lookup,
            question_transform=self.question_transform,
            prompt_template=self.prompt_template,
            plain_text=self.plain_text,
            max_context_memories=self.max_context_memories,
            **self._kwargs(request),
        ).answer_dynamic(request, analysis, resolution, candidates)
