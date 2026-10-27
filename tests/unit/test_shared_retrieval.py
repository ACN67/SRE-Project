"""Shared candidates across dispatches, concurrency, invalidation and eviction."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from dynamic_memory.retrieval import SharedRetrieval, IndexManager
from test_vector_retrieval import setup
from test_retrieval_index import artifact, settings, assert_code


def test_same_query_across_methods_encodes_once_and_copies():
    encoder, index, request, analysis, retriever = setup()
    shared = SharedRetrieval(retriever)
    first = shared.retrieve(request, analysis, index)
    second = shared.retrieve(request, analysis, index)
    assert first == second and first is not second
    first.items[0].source_refs[0].dia_id = "changed"
    assert shared.retrieve(request, analysis, index) == second
    assert len(encoder.questions) == 1
    assert [m.memory_id for m in shared.load_candidates(second, index)] == [i.memory_id for i in second.items]


def test_concurrent_dispatches_share_single_execution():
    encoder, index, request, analysis, retriever = setup()
    shared = SharedRetrieval(retriever)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: shared.retrieve(request, analysis, index), range(8)))
    assert all(result == results[0] for result in results)
    assert len(encoder.questions) == 1


def test_question_analysis_source_changes_and_stale_encoder():
    encoder, index, request, analysis, retriever = setup()
    shared = SharedRetrieval(retriever)
    shared.retrieve(request, analysis, index)
    request.question = "Where does Sam work?"
    shared.retrieve(request, analysis, index)
    analysis.entity_hints = ["Sam", "synthetic-alias-hint"]
    shared.retrieve(request, analysis, index)
    new_index = IndexManager(encoder).build_index(artifact(build="new-build"), settings())
    shared.retrieve(request, analysis, new_index)
    assert len(encoder.questions) == 4
    encoder.identity = encoder.identity.model_copy(update={"model_revision": "changed"})
    assert_code("INDEX_STALE", lambda: shared.retrieve(request, analysis, new_index))
    assert len(encoder.questions) == 4


def test_lru_eviction_and_explicit_clear():
    encoder, index, request, analysis, retriever = setup()
    shared = SharedRetrieval(retriever, max_entries=1)
    shared.retrieve(request, analysis, index)
    other = request.model_copy(update={"question": "Another question"})
    shared.retrieve(other, analysis, index)
    shared.retrieve(request, analysis, index)
    assert len(encoder.questions) == 3
    shared.clear()
    shared.retrieve(request, analysis, index)
    assert len(encoder.questions) == 4


@pytest.mark.parametrize("size", [0, -1, True, 1.5])
def test_invalid_capacity(size):
    *_, retriever = setup()
    with pytest.raises(ValueError):
        SharedRetrieval(retriever, max_entries=size)
