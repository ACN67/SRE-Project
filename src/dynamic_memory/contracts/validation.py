"""Checks that require explicit candidate and source context."""

from .models import CandidateSet, MemoryRecord, QueryResponse, ResolutionResult, SourceTurn


def validate_resolution_against_candidates(result: ResolutionResult, candidates: CandidateSet) -> None:
    if result.query_id != candidates.query_id:
        raise ValueError("ResolutionResult query_id differs from CandidateSet")
    ids = {item.memory_id for item in candidates.items}
    if {decision.memory_id for decision in result.decisions} != ids:
        raise ValueError("exactly one decision is required for each candidate")
    if not set(result.unresolved_conflict_ids) <= ids:
        raise ValueError("unresolved_conflict_ids must be candidates")
    for edge in result.relations:
        if edge.from_memory_id not in ids or edge.to_memory_id not in ids:
            raise ValueError("RelationEdge endpoints must be candidates")


def validate_response_context(response: QueryResponse, memories: list[MemoryRecord], turns: list[SourceTurn]) -> None:
    """Validate IDs and references only; this cannot establish semantic support."""
    turn_keys = {(t.sample_id, t.session_id, t.dia_id) for t in turns}
    memory_by_id = {m.memory_id: m for m in memories if m.sample_id == response.sample_id}
    if len(memory_by_id) != len([m for m in memories if m.sample_id == response.sample_id]):
        raise ValueError("duplicate MemoryRecord ID")
    answer = response.answer_result
    for ref in answer.context_source_refs:
        if (response.sample_id, ref.session_id, ref.dia_id) not in turn_keys:
            raise ValueError("context SourceRef not found in original turns")
    if response.retrieved:
        ids = {item.memory_id for item in response.retrieved.items}
        if not set(answer.used_memory_ids) <= ids:
            raise ValueError("used_memory_ids must be retrieved candidates")
        for item in response.retrieved.items:
            memory = memory_by_id.get(item.memory_id)
            if memory is None or item.source_refs != memory.source_refs:
                raise ValueError("candidate source does not match MemoryRecord")
    if response.resolution and response.retrieved:
        validate_resolution_against_candidates(response.resolution, response.retrieved)
        if response.method == "dynamic" and answer.status == "answered" and not set(answer.used_memory_ids) <= set(response.resolution.effective_memory_ids):
            raise ValueError("dynamic answer uses memory outside effective set")
    for memory_id in answer.used_memory_ids:
        memory = memory_by_id.get(memory_id)
        if memory is None:
            raise ValueError("used MemoryRecord not found")
        source_keys = {(r.session_id, r.dia_id) for r in memory.source_refs}
        context_keys = {(r.session_id, r.dia_id) for r in answer.context_source_refs}
        if not source_keys <= context_keys:
            raise ValueError("used MemoryRecord sources missing from answer context")
    if response.method in {"vector_rag", "dynamic"} and answer.status == "answered":
        if not answer.used_memory_ids or not answer.evidence_refs:
            raise ValueError("answered RAG/dynamic response needs used memory and evidence")
        used_source_keys = {(r.session_id, r.dia_id) for mid in answer.used_memory_ids for r in memory_by_id[mid].source_refs}
        if not {(r.session_id, r.dia_id) for r in answer.evidence_refs} <= used_source_keys:
            raise ValueError("evidence is not from used MemoryRecords")
