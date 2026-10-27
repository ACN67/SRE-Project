"""Bounded process-local snapshot reuse for A's separate method dispatches."""

from collections import OrderedDict
from threading import RLock

from dynamic_memory.contracts.models import QueryAnalysis, QueryRequest
from .index import fingerprint
from .query_analysis import QUERY_ANALYSIS_VERSION
from .vector_retrieval import VectorRetriever, _snapshot


class SharedRetrieval:
    """Inject retrieve/load_candidates into A; do not cache upstream verification.

    A must still load/verify the current B artifact and C index on each dispatch.
    Entries contain private memory identifiers and stay in this instance only.
    One lock ensures concurrent methods cannot build two snapshots for one key.
    """

    def __init__(self, retriever: VectorRetriever, *, max_entries: int = 128):
        if not isinstance(retriever, VectorRetriever) or type(max_entries) is not int or max_entries < 1:
            raise ValueError("valid retriever and positive max_entries required")
        self._retriever = retriever
        self._max_entries = max_entries
        self._entries = OrderedDict()
        self._lock = RLock()

    def retrieve_with_trace(self, request, analysis, index):
        request = _snapshot(request, QueryRequest, "QUERY_INVALID")
        analysis = _snapshot(analysis, QueryAnalysis, "QUERY_ANALYSIS_INVALID")
        with self._lock:
            # A cache hit never bypasses current encoder/index identity validation.
            document, _ = self._retriever._check_index(index)
            key = fingerprint({"request": request.model_dump(mode="json"),
                               "analysis": analysis.model_dump(mode="json"),
                               "analysis_version": QUERY_ANALYSIS_VERSION,
                               "index": document.retriever_fingerprint})
            if key not in self._entries:
                self._entries[key] = self._retriever.retrieve_with_trace(request, analysis, index)
                if len(self._entries) > self._max_entries:
                    self._entries.popitem(last=False)
            self._entries.move_to_end(key)
            return self._entries[key]

    def retrieve(self, request, analysis, index):
        return self.retrieve_with_trace(request, analysis, index).candidates

    def load_candidates(self, candidates, index):
        return self._retriever.load_candidates(candidates, index)

    def clear(self):
        with self._lock:
            self._entries.clear()
