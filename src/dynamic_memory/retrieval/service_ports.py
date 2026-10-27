"""C-owned read-only ports for A's service, with per-query index pinning."""

from contextvars import ContextVar

from dynamic_memory.contracts import ConversationSample
from dynamic_memory.pipeline.settings import RetrievalSettings
from .index import IndexManager, RetrievalIndexError, _artifact_snapshot, fingerprint
from .shared_retrieval import SharedRetrieval
from .vector_retrieval import VectorRetriever, _snapshot
from .vector_answer import VectorAnswerer


class RetrievalServicePorts:
    """Inject B's strict artifact loader, configured paths and A's readiness type.

    No builds, model downloads or generation occur in index_readiness or loading.
    A's IndexReadiness is supplied as a factory until A is merged into main; it
    is never duplicated here. Each sample's answerer binds its selected index.
    """

    def __init__(self, *, encoder, settings, load_artifact, index_path,
                 readiness_factory, runtime, provenance_factory,
                 on_diagnostic=None, synthetic_test_mode=False, max_entries=128):
        if not all(callable(port) for port in (load_artifact, index_path, readiness_factory, provenance_factory)):
            raise ValueError("service factories and loaders must be callable")
        self._settings = _snapshot(settings, RetrievalSettings, "INDEX_CONFIGURATION_INVALID")
        self._manager = IndexManager(encoder)
        self._shared = SharedRetrieval(VectorRetriever(encoder, self._settings), max_entries=max_entries)
        self._load_artifact, self._index_path = load_artifact, index_path
        self._readiness_factory = readiness_factory
        self._runtime, self._provenance_factory = runtime, provenance_factory
        self._on_diagnostic, self._synthetic = on_diagnostic, synthetic_test_mode
        self._active = ContextVar("c_retrieval_query", default=None)

    def _source(self, sample_id):
        # The callback must perform B's disk/provenance verification on every call.
        artifact = _artifact_snapshot(self._load_artifact(sample_id))
        if artifact.manifest.sample_id != sample_id:
            raise RetrievalIndexError("SAMPLE_MISMATCH")
        return artifact

    def _load(self, sample_id):
        artifact = self._source(sample_id)
        index = self._manager.load_index(self._index_path(sample_id), artifact, self._settings)
        return artifact, index

    def index_readiness(self, sample_id, source_manifest_fingerprint):
        artifact = self._source(sample_id)
        state, index_fp = "stale", None
        if artifact.source_manifest_fingerprint == source_manifest_fingerprint:
            try:
                index = self._manager.load_index(self._index_path(sample_id), artifact, self._settings)
                state, index_fp = "ready", index.retriever_fingerprint
            except RetrievalIndexError as error:
                states = {"INDEX_NOT_FOUND": "not_found", "INDEX_CORRUPT": "corrupt",
                          "INDEX_STALE": "stale", "SAMPLE_MISMATCH": "stale"}
                if error.code not in states:
                    raise
                state = states[error.code]
        return self._readiness_factory(
            sample_id=sample_id, dataset_snapshot_sha256=artifact.manifest.dataset_snapshot_sha256,
            source_manifest_fingerprint=artifact.source_manifest_fingerprint,
            state=state, index_fingerprint=index_fp)

    def load_verified_index(self, sample):
        self._active.set(None)
        sample = _snapshot(sample, ConversationSample, "SAMPLE_INVALID")
        artifact, index = self._load(sample.sample_id)
        refs = {(turn.session_id, turn.dia_id) for session in sample.sessions for turn in session.turns}
        if any((r.session_id, r.dia_id) not in refs for m in artifact.memories for r in m.source_refs):
            raise RetrievalIndexError("ARTIFACT_CORRUPT")
        return index

    def retrieve(self, request, analysis, index):
        self._active.set(None)
        snapshot = self._shared.retrieve_with_trace(request, analysis, index)
        key = fingerprint({"request": request.model_dump(mode="json"), "analysis": analysis.model_dump(mode="json")})
        self._active.set((key, index, snapshot.final_candidate_ids))
        return snapshot.candidates

    def load_candidates(self, candidates, index):
        _, current = self._load(index.sample_id)
        if current != index:
            self._active.set(None)
            raise RetrievalIndexError("INDEX_STALE")
        return self._shared.load_candidates(candidates, index)

    def answer_vector_rag(self, request, analysis, candidates):
        active = self._active.get()
        self._active.set(None)  # A's synchronous invocation consumes the pinned query once.
        if active is None:
            raise RetrievalIndexError("INDEX_STALE")
        key, selected, ids = active
        if (key != fingerprint({"request": request.model_dump(mode="json"), "analysis": analysis.model_dump(mode="json")}) or
                request.sample_id != selected.sample_id or tuple(m.memory_id for m in candidates) != ids):
            raise RetrievalIndexError("SAMPLE_MISMATCH")
        artifact, current = self._load(request.sample_id)
        if current != selected:
            raise RetrievalIndexError("INDEX_STALE")
        answerer = VectorAnswerer(self._runtime, self._provenance_factory(artifact), selected,
                                 on_diagnostic=self._on_diagnostic, synthetic_test_mode=self._synthetic)
        return answerer(request, analysis, candidates)
