"""Sample-local semantic retrieval with bounded, evidence-field history expansion."""

import math
from dataclasses import dataclass, field
from typing import Protocol

from dynamic_memory.contracts.models import CandidateItem, CandidateSet, MemoryRecord, QueryAnalysis, QueryRequest
from dynamic_memory.pipeline.settings import RetrievalSettings
from .index import (IndexEncoder, RetrievalIndex, RetrievalIndexError,
                    _encoder_snapshot, _reject_extra_fields, fingerprint)


class QueryEncoder(IndexEncoder, Protocol):
    def embed_query(self, question: str) -> list[float]: ...


@dataclass(frozen=True)
class RetrievalSnapshot:
    """One query execution, reusable by both methods without running retrieval twice.

    Copies returned by properties isolate mutations by downstream consumers.
    This is a C-local trace, not a new public business DTO.
    """

    query_fingerprint: str
    initial_topk_ids: tuple[str, ...]
    expanded_ids: tuple[str, ...]
    final_candidate_ids: tuple[str, ...]
    expansion_pool_ids: tuple[str, ...]
    budget_excluded_ids: tuple[str, ...]
    _candidates_json: str = field(repr=False)
    _pure_vector_json: str = field(repr=False)

    @property
    def candidates(self) -> CandidateSet:
        return CandidateSet.model_validate_json(self._candidates_json)

    @property
    def pure_vector(self) -> CandidateSet:
        return CandidateSet.model_validate_json(self._pure_vector_json)


def _history_key(memory: MemoryRecord):
    # Only B's explicit structured pair can anchor expansion. No alias inference.
    if not memory.entity or not memory.attribute:
        return None
    pair = (memory.entity.strip().casefold(), memory.attribute.strip().casefold())
    return pair if all(pair) else None


def _snapshot(value, expected, code):
    try:
        if type(value) is not expected:
            raise ValueError("unexpected object type")
        _reject_extra_fields(value)
        return expected.model_validate_json(value.model_dump_json())
    except Exception:
        raise RetrievalIndexError(code) from None


class VectorRetriever:
    """Bind the same encoder/settings used by IndexManager; expose formal ports.

    Analysis hints are advisory. Semantic retrieval does not discard old states,
    uncertain dates or negative scores. The caller handles time-conflict responses.
    """

    def __init__(self, encoder: QueryEncoder, settings: RetrievalSettings):
        self._encoder = encoder
        self._settings = _snapshot(settings, RetrievalSettings, "INDEX_CONFIGURATION_INVALID")

    def _check_index(self, index):
        if type(index) is not RetrievalIndex:
            raise RetrievalIndexError("INDEX_CORRUPT")
        index.__post_init__()
        document = index._document()
        identity = _encoder_snapshot(self._encoder)
        if (document.encoder_identity != identity or
                document.settings_fingerprint != fingerprint(self._settings.model_dump(mode="json")) or
                self._settings.embedding_model != identity.model_name):
            raise RetrievalIndexError("INDEX_STALE")
        return document, identity

    def retrieve(self, request: QueryRequest, analysis: QueryAnalysis, index: RetrievalIndex) -> CandidateSet:
        return self.retrieve_with_trace(request, analysis, index).candidates

    def retrieve_with_trace(self, request: QueryRequest, analysis: QueryAnalysis,
                            index: RetrievalIndex) -> RetrievalSnapshot:
        request = _snapshot(request, QueryRequest, "QUERY_INVALID")
        analysis = _snapshot(analysis, QueryAnalysis, "QUERY_ANALYSIS_INVALID")
        document, identity = self._check_index(index)
        if request.sample_id != document.sample_id or analysis.sample_id != request.sample_id:
            raise RetrievalIndexError("SAMPLE_MISMATCH")
        if request.query_id != analysis.query_id:
            raise RetrievalIndexError("QUERY_ANALYSIS_MISMATCH")
        if analysis.time_mode == "conflict":
            raise RetrievalIndexError("QUERY_TIME_CONFLICT")
        if ((analysis.time_mode != "as_of" and analysis.resolved_as_of is not None) or
                (request.as_of is not None and analysis.time_mode == "latest_known") or
                (request.as_of is not None and analysis.time_mode == "as_of" and
                 request.as_of != analysis.resolved_as_of)):
            raise RetrievalIndexError("QUERY_ANALYSIS_MISMATCH")
        scored = []
        if document.memory_ids:
            try:
                raw = self._encoder.embed_query(request.question)
            except RetrievalIndexError:
                raise
            except Exception:
                raise RetrievalIndexError("EMBEDDING_FAILED") from None
            if _encoder_snapshot(self._encoder) != identity:
                raise RetrievalIndexError("INDEX_STALE")
            try:
                vector = tuple(raw)
                if (len(vector) != document.dimension or
                        any(type(x) not in (float, int) or not math.isfinite(x) for x in vector) or
                        not math.isclose(math.hypot(*vector), 1.0, abs_tol=1e-6)):
                    raise ValueError("invalid query vector")
            except Exception:
                raise RetrievalIndexError("EMBEDDING_OUTPUT_INVALID") from None
            # Unit vectors: dot product is cosine similarity. Clamp rounding noise.
            scored = [(mid, max(-1.0, min(1.0, math.fsum(a * b for a, b in zip(vector, row)))))
                      for mid, row in zip(document.memory_ids, document.vectors)]
            scored.sort(key=lambda pair: (-pair[1], pair[0]))
        memories = {m.memory_id: m for m in index.memories}
        initial = scored[:self._settings.base_top_k]
        initial_ids = {mid for mid, _ in initial}
        anchors = {}
        for rank, (mid, _) in enumerate(initial):
            key = _history_key(memories[mid])
            if key is not None:
                anchors.setdefault(key, rank)
        pool = [(mid, score) for mid, score in scored
                if mid not in initial_ids and _history_key(memories[mid]) in anchors]
        # Earlier semantic anchors get priority; never infer validity from time.
        pool.sort(key=lambda pair: (anchors[_history_key(memories[pair[0]])], -pair[1], pair[0]))
        room = self._settings.max_candidates - len(initial)
        expanded = pool[:room]

        def candidate_set(rows, base_top_k, max_candidates):
            return CandidateSet(query_id=request.query_id, sample_id=request.sample_id,
                                base_top_k=base_top_k, max_candidates=max_candidates,
                                retriever_fingerprint=document.retriever_fingerprint,
                                items=[CandidateItem(memory_id=mid, rank=rank, score=score,
                                                     source_refs=memories[mid].source_refs, origin=origin)
                                       for rank, (mid, score, origin) in enumerate(rows, 1)])

        final = candidate_set([(mid, score, "semantic") for mid, score in initial] +
                              [(mid, score, "history_expansion") for mid, score in expanded],
                              self._settings.base_top_k, self._settings.max_candidates)
        pure_k = self._settings.optional_pure_vector_top_k
        pure = candidate_set([(mid, score, "semantic") for mid, score in scored[:pure_k]], pure_k, pure_k)
        return RetrievalSnapshot(
            query_fingerprint=fingerprint({"request": request.model_dump(mode="json"),
                                           "analysis": analysis.model_dump(mode="json"),
                                           "index": document.retriever_fingerprint}),
            initial_topk_ids=tuple(mid for mid, _ in initial),
            expanded_ids=tuple(mid for mid, _ in expanded),
            final_candidate_ids=tuple(item.memory_id for item in final.items),
            expansion_pool_ids=tuple(mid for mid, _ in pool),
            budget_excluded_ids=tuple(mid for mid, _ in pool[room:]),
            _candidates_json=final.model_dump_json(), _pure_vector_json=pure.model_dump_json())

    def load_candidates(self, candidates: CandidateSet, index: RetrievalIndex) -> list[MemoryRecord]:
        """Validate provenance and restore rank order; return independent records."""
        candidates = _snapshot(candidates, CandidateSet, "CANDIDATES_INVALID")
        document, _ = self._check_index(index)
        if candidates.sample_id != document.sample_id:
            raise RetrievalIndexError("SAMPLE_MISMATCH")
        if candidates.retriever_fingerprint != document.retriever_fingerprint:
            raise RetrievalIndexError("INDEX_STALE")
        budgets = (candidates.base_top_k, candidates.max_candidates)
        main_budget = (self._settings.base_top_k, self._settings.max_candidates)
        pure_budget = (self._settings.optional_pure_vector_top_k,) * 2
        if (budgets not in (main_budget, pure_budget) or
                (budgets != main_budget and any(item.origin != "semantic" for item in candidates.items))):
            raise RetrievalIndexError("CANDIDATES_INVALID")
        memories = {m.memory_id: m for m in index.memories}
        loaded = []
        for item in sorted(candidates.items, key=lambda item: item.rank):
            memory = memories.get(item.memory_id)
            if memory is None or not math.isfinite(item.score) or not -1.0 <= item.score <= 1.0:
                raise RetrievalIndexError("CANDIDATES_INVALID")
            refs = [(ref.session_id, ref.dia_id) for ref in item.source_refs]
            if (len(refs) != len(set(refs)) or
                    set(refs) != {(ref.session_id, ref.dia_id) for ref in memory.source_refs}):
                raise RetrievalIndexError("CANDIDATE_SOURCE_MISMATCH")
            loaded.append(memory)
        return loaded
