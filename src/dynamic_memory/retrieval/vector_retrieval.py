"""Sample-local semantic retrieval with bounded, evidence-field history expansion."""

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Protocol

from dynamic_memory.contracts.models import CandidateItem, CandidateSet, MemoryRecord, QueryAnalysis, QueryRequest
from dynamic_memory.pipeline.settings import RetrievalSettings
from .index import (IndexEncoder, RetrievalIndex, RetrievalIndexError,
                    _encoder_snapshot, _reject_extra_fields, fingerprint)

_ADJACENT_TURN_DISTANCE = 2
_LEXICAL_HYBRID_WEIGHT = 0.15
_LEXICAL_STOP = frozenset(
    "a an the to at in on of and or is was are were be been being have has had "
    "do did does with for from by into out it this that these those his her "
    "their my your our me him them as so very really some all up down about "
    "now then he she they i we you what when where who whom which why how".split()
)
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


def _stem(word: str) -> str:
    if len(word) > 5 and word.endswith("ing"):
        word = word[:-3]
    elif len(word) > 4 and word.endswith("ed"):
        word = word[:-2]
    elif len(word) > 4 and word.endswith("es"):
        word = word[:-2]
    elif len(word) > 3 and word.endswith("s"):
        word = word[:-1]
    return word


def _lexical_tokens(value: str) -> tuple[str, ...]:
    return tuple(
        _stem(token)
        for token in re.findall(r"[a-z0-9']+", value.casefold())
        if token not in _LEXICAL_STOP and len(token) > 1
    )


def _memory_lexical_text(memory: MemoryRecord) -> str:
    event_time = memory.event_time
    return " ".join(
        value
        for value in (
            memory.content,
            memory.entity,
            memory.attribute,
            memory.value,
            event_time.raw if event_time is not None else None,
            event_time.start.isoformat() if event_time is not None else None,
            memory.recorded_at.isoformat() if memory.recorded_at is not None else None,
        )
        if value
    )


def _source_key(memory: MemoryRecord) -> tuple[str, str]:
    ref = min(
        memory.source_refs,
        key=lambda item: (item.session_id, item.dia_id),
    )
    return ref.session_id, ref.dia_id


def _turn_position(ref):
    try:
        prefix, ordinal = ref.dia_id.rsplit(":", 1)
        if not prefix.startswith("D") or not ordinal.isdigit():
            return None
        return ref.session_id, int(ordinal)
    except (AttributeError, TypeError, ValueError):
        return None


def _temporal_distance(memory: MemoryRecord, anchors) -> int | None:
    positions = tuple(
        position
        for ref in memory.source_refs
        if (position := _turn_position(ref)) is not None
    )
    distances = [
        abs(position[1] - anchor[1])
        for position in positions
        for anchor in anchors
        if position[0] == anchor[0]
    ]
    return min(distances) if distances else None


def _source_diverse(
    rows: list[tuple[str, float]],
    memories: dict[str, MemoryRecord],
    *,
    limit: int,
    occupied_sources: set[tuple[str, str]] | None = None,
) -> list[tuple[str, float]]:
    """Prefer distinct evidenced turns, then fill a short tail deterministically."""
    if limit <= 0:
        return []
    occupied = set(occupied_sources or ())
    selected: list[tuple[str, float]] = []
    selected_ids = set()
    for row in rows:
        key = _source_key(memories[row[0]])
        if key in occupied:
            continue
        occupied.add(key)
        selected.append(row)
        selected_ids.add(row[0])
        if len(selected) == limit:
            return selected
    for row in rows:
        if row[0] in selected_ids:
            continue
        selected.append(row)
        if len(selected) == limit:
            break
    return selected


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
        self._lexical_profiles = {}

    def _lexical_profile(self, index, memories):
        key = index.retriever_fingerprint
        profile = self._lexical_profiles.get(key)
        if profile is None:
            terms = {
                memory.memory_id: _lexical_tokens(_memory_lexical_text(memory))
                for memory in memories
            }
            frequencies = {
                memory_id: Counter(document)
                for memory_id, document in terms.items()
            }
            lengths = {
                memory_id: len(document)
                for memory_id, document in terms.items()
            }
            document_frequency = Counter()
            for document in terms.values():
                document_frequency.update(set(document))
            average_length = (
                sum(lengths.values()) / len(lengths)
                if lengths
                else 0.0
            )
            profile = {
                "frequencies": frequencies,
                "lengths": lengths,
                "document_frequency": document_frequency,
                "average_length": average_length,
                "count": len(memories),
            }
            self._lexical_profiles[key] = profile
        return profile

    def _lexically_reranked(self, scored, memories, question, index):
        query_terms = _lexical_tokens(question)
        if not scored or not query_terms:
            return scored
        memories_by_id = {memory.memory_id: memory for memory in memories}
        profile = self._lexical_profile(index, memories)
        lexical = {}
        maximum = 0.0
        total = profile["count"]
        average_length = profile["average_length"]
        for memory_id, _ in scored:
            memory = memories_by_id[memory_id]
            frequencies = profile["frequencies"][memory_id]
            length = profile["lengths"][memory_id]
            value = 0.0
            for term in query_terms:
                frequency = frequencies.get(term, 0)
                if not frequency:
                    continue
                inverse_frequency = math.log(
                    (total + 1)
                    / (profile["document_frequency"][term] + 0.5)
                )
                denominator = frequency + 1.2 * (
                    0.25 + 0.75 * length / average_length
                )
                value += inverse_frequency * frequency * 2.2 / denominator
            lexical[memory_id] = value
            maximum = max(maximum, value)
        if maximum <= 0:
            return scored
        return sorted(
            (
                (memory_id, score)
                for memory_id, score in scored
            ),
            key=lambda pair: (
                -(
                    pair[1]
                    + _LEXICAL_HYBRID_WEIGHT
                    * lexical[pair[0]]
                    / maximum
                ),
                pair[0],
            ),
        )

    def _check_index(self, index):
        if type(index) is not RetrievalIndex:
            raise RetrievalIndexError("INDEX_CORRUPT")
        # RetrievalIndex validates once in __post_init__, is frozen, and caches
        # its parsed document. Re-parsing every query here made a 41 MB index
        # many times per question without adding a mutation boundary.
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
        memory_snapshot = index._memory_snapshot()
        memories = {m.memory_id: m for m in memory_snapshot}
        ranked = self._lexically_reranked(
            scored,
            memory_snapshot,
            request.question,
            index,
        )
        initial = _source_diverse(
            ranked,
            memories,
            limit=self._settings.base_top_k,
        )
        initial_ids = {mid for mid, _ in initial}
        anchors = {}
        for rank, (mid, _) in enumerate(initial):
            key = _history_key(memories[mid])
            if key is not None:
                anchors.setdefault(key, rank)
        temporal_anchors = {
            position
            for mid, _ in initial
            for ref in memories[mid].source_refs
            if (position := _turn_position(ref)) is not None
        }
        pool = []
        for pair in ranked:
            if pair[0] in initial_ids:
                continue
            memory = memories[pair[0]]
            distance = _temporal_distance(memory, temporal_anchors)
            if (
                anchors.get(_history_key(memory)) is None
                and not (
                    distance is not None
                    and distance <= _ADJACENT_TURN_DISTANCE
                )
            ):
                continue
            pool.append(pair)

        def expansion_key(pair):
            memory = memories[pair[0]]
            history_rank = anchors.get(_history_key(memory))
            distance = _temporal_distance(memory, temporal_anchors)
            near = (
                distance is not None
                and distance <= _ADJACENT_TURN_DISTANCE
            )
            if history_rank is not None:
                return (0, history_rank, distance if near else 99, -pair[1], pair[0])
            if near:
                return (1, distance, -pair[1], pair[0])
            raise AssertionError("pool contains an unrelated candidate")

        # Earlier semantic state anchors get priority, followed by a bounded
        # local dialogue neighborhood. Adjacent candidates are existing,
        # reviewed Memories; retrieval never injects an unreviewed source turn.
        pool.sort(key=expansion_key)
        room = self._settings.max_candidates - len(initial)
        expanded = _source_diverse(
            pool,
            memories,
            limit=room,
            occupied_sources={
                _source_key(memories[mid])
                for mid, _ in initial
            },
        )
        expanded_ids = {item[0] for item in expanded}

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
        pure = candidate_set(
            [
                (mid, score, "semantic")
                for mid, score in _source_diverse(
                    scored,
                    memories,
                    limit=pure_k,
                )
            ],
            pure_k,
            pure_k,
        )
        return RetrievalSnapshot(
            query_fingerprint=fingerprint({"request": request.model_dump(mode="json"),
                                           "analysis": analysis.model_dump(mode="json"),
                                           "index": document.retriever_fingerprint}),
            initial_topk_ids=tuple(mid for mid, _ in initial),
            expanded_ids=tuple(mid for mid, _ in expanded),
            final_candidate_ids=tuple(item.memory_id for item in final.items),
            expansion_pool_ids=tuple(mid for mid, _ in pool),
            budget_excluded_ids=tuple(
                mid for mid, _ in pool
                if mid not in expanded_ids
            ),
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
