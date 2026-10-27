"""Synthetic deterministic retrieval checks, independent of model downloads."""

from datetime import date
import math

import pytest

from dynamic_memory.contracts.models import QueryRequest, SourceRef
from dynamic_memory.retrieval import VectorRetriever, analyze_query, IndexManager
from dynamic_memory.retrieval.index import fingerprint
from test_retrieval_index import FakeEncoder, artifact, settings, assert_code


class QueryEncoder(FakeEncoder):
    def __init__(self, vector=None):
        super().__init__()
        self.vector = [0.6, 0.8] if vector is None else vector
        self.questions = []

    def embed_query(self, question):
        self.questions.append(question)
        return self.vector


def setup(count=2, **changes):
    encoder = QueryEncoder()
    config = settings(**changes)
    index = IndexManager(encoder).build_index(artifact(count=count), config)
    request = QueryRequest(query_id="synthetic-q", sample_id=index.sample_id, question="What is Sam's job?")
    return encoder, index, request, analyze_query(request), VectorRetriever(encoder, config)


def test_semantic_ranking_and_rank_loaded_memories():
    encoder, index, request, analysis, retriever = setup()
    candidates = retriever.retrieve(request, analysis, index)
    records = retriever.load_candidates(candidates, index)
    assert "teacher" in records[0].content
    assert [item.score for item in candidates.items] == pytest.approx([1.0, 0.6])
    assert [item.rank for item in candidates.items] == [1, 2]
    assert all(item.origin == "semantic" for item in candidates.items)
    assert [m.memory_id for m in records] == [i.memory_id for i in candidates.items]
    assert [m.source_refs for m in records] == [i.source_refs for i in candidates.items]
    assert encoder.questions == [request.question]
    records[0].content = "changed outside"
    assert "teacher" in retriever.load_candidates(candidates, index)[0].content


def test_top8_ties_are_stable_and_custom_budget_is_respected():
    encoder, index, request, analysis, retriever = setup(count=13)
    encoder.vector = [1.0, 0.0]
    result = retriever.retrieve(request, analysis, index)
    expected = sorted(m.memory_id for m in index.memories if "football" in m.content)[:8]
    assert [i.memory_id for i in result.items] == expected
    assert result == retriever.retrieve(request, analysis, index)
    assert (result.base_top_k, result.max_candidates) == (8, 12)
    encoder, index, request, analysis, retriever = setup(count=13, base_top_k=3)
    assert len(retriever.retrieve(request, analysis, index).items) == 3


def test_empty_index_success_without_encoding():
    encoder, index, request, analysis, retriever = setup(count=0)
    result = retriever.retrieve(request, analysis, index)
    assert result.items == []
    assert retriever.load_candidates(result, index) == []
    assert encoder.questions == []


def test_unknown_time_and_hints_do_not_filter_negative_scores():
    encoder, index, request, analysis, retriever = setup()
    encoder.vector = [-1.0, 0.0]
    analysis.time_mode = "unknown"
    analysis.entity_hints = ["not in sample"]
    analysis.attribute_hints = ["no match"]
    result = retriever.retrieve(request, analysis, index)
    assert len(result.items) == 2
    assert [i.score for i in result.items] == pytest.approx([-0.6, -1.0])


@pytest.mark.parametrize("vector", [[], [0.0, 0.0], [1.0], [1.0, 1.0],
                                    [math.nan, 0.0], [math.inf, 0.0], [True, 0], ["1", 0], None])
def test_bad_query_vectors(vector):
    encoder, index, request, analysis, retriever = setup()
    encoder.vector = vector
    assert_code("EMBEDDING_OUTPUT_INVALID", lambda: retriever.retrieve(request, analysis, index))


@pytest.mark.parametrize("target,field,value,code", [
    ("request", "sample_id", "other", "SAMPLE_MISMATCH"),
    ("analysis", "sample_id", "other", "SAMPLE_MISMATCH"),
    ("analysis", "query_id", "other", "QUERY_ANALYSIS_MISMATCH"),
    ("request", "question", " ", "QUERY_INVALID"),
    ("request", "as_of", date(2020, 1, 1), "QUERY_ANALYSIS_MISMATCH"),
    ("analysis", "resolved_as_of", date(2020, 1, 1), "QUERY_ANALYSIS_MISMATCH"),
])
def test_preflight_mismatch_never_encodes(target, field, value, code):
    encoder, index, request, analysis, retriever = setup()
    setattr(request if target == "request" else analysis, field, value)
    assert_code(code, lambda: retriever.retrieve(request, analysis, index))
    assert encoder.questions == []


def test_conflict_stops_and_explicit_time_mismatch_rejected():
    encoder, index, request, analysis, retriever = setup()
    request.question = "Where did Sam work in 2020?"
    request.as_of = date(2021, 1, 1)
    assert_code("QUERY_TIME_CONFLICT", lambda: retriever.retrieve(request, analyze_query(request), index))
    analysis.time_mode = "as_of"
    analysis.resolved_as_of = date(2020, 1, 1)
    assert_code("QUERY_ANALYSIS_MISMATCH", lambda: retriever.retrieve(request, analysis, index))
    assert encoder.questions == []


def test_model_settings_staleness_and_mid_encoding_change():
    encoder, index, request, analysis, retriever = setup()
    changed = VectorRetriever(encoder, settings(base_top_k=7))
    assert_code("INDEX_STALE", lambda: changed.retrieve(request, analysis, index))
    encoder.identity = encoder.identity.model_copy(update={"model_revision": "changed"})
    assert_code("INDEX_STALE", lambda: retriever.retrieve(request, analysis, index))
    assert encoder.questions == []
    encoder, index, request, analysis, retriever = setup()
    def changing(question):
        encoder.identity = encoder.identity.model_copy(update={"encoding_fingerprint": fingerprint("changed")})
        return [0.6, 0.8]
    encoder.embed_query = changing
    assert_code("INDEX_STALE", lambda: retriever.retrieve(request, analysis, index))


def test_encoder_failure_is_sanitized():
    encoder, index, request, analysis, retriever = setup()
    def broken(question):
        raise RuntimeError("private question and local paths")
    encoder.embed_query = broken
    assert_code("EMBEDDING_FAILED", lambda: retriever.retrieve(request, analysis, index))


@pytest.mark.parametrize("change,code", [
    ("sample", "SAMPLE_MISMATCH"), ("fingerprint", "INDEX_STALE"),
    ("budget", "CANDIDATES_INVALID"), ("duplicate", "CANDIDATES_INVALID"),
    ("unknown", "CANDIDATES_INVALID"), ("ranks", "CANDIDATES_INVALID"),
    ("nan", "CANDIDATES_INVALID"), ("source", "CANDIDATE_SOURCE_MISMATCH"),
    ("duplicate_source", "CANDIDATE_SOURCE_MISMATCH"),
])
def test_loading_rejects_forged_or_mutated_candidates(change, code):
    encoder, index, request, analysis, retriever = setup()
    result = retriever.retrieve(request, analysis, index)
    if change == "sample": result.sample_id = "other"
    elif change == "fingerprint": result.retriever_fingerprint = "other"
    elif change == "budget": result.base_top_k = 7
    elif change == "duplicate": result.items[1].memory_id = result.items[0].memory_id
    elif change == "unknown": result.items[0].memory_id = "mem_" + "0" * 20
    elif change == "ranks": result.items.reverse()
    elif change == "nan": result.items[0].score = math.nan
    elif change == "source": result.items[0].source_refs = [SourceRef(session_id="s1", dia_id="forged")]
    elif change == "duplicate_source": result.items[0].source_refs *= 2
    assert_code(code, lambda: retriever.load_candidates(result, index))


def test_saved_index_can_be_searched_after_reload(tmp_path):
    from dynamic_memory.retrieval import save_index
    encoder, index, request, analysis, retriever = setup()
    path = tmp_path / "index.json"
    save_index(index, path)
    reloaded = IndexManager(encoder).load_index(path, artifact(), settings())
    assert retriever.retrieve(request, analysis, reloaded) == retriever.retrieve(request, analysis, index)
