"""Synthetic history recall, budgets and shared snapshot regression tests."""

import math
from datetime import datetime, timezone

import pytest

from dynamic_memory.contracts import MemoryArtifact, memory_set_fingerprint
from dynamic_memory.contracts.memory_id import compute_memory_id
from dynamic_memory.contracts.models import QueryRequest
from dynamic_memory.retrieval import IndexManager, VectorRetriever, analyze_query, save_index
from test_retrieval_index import artifact, settings, FakeEncoder


def history_artifact(scenario="employment", extra=0):
    histories = {
        "employment": [("Sam works at B.", "state", "fact", "positive"),
                       ("Sam left A.", "event", "fact", "negative"),
                       ("Sam worked at A.", "state", "fact", "positive")],
        "adoption_stage": [("Sam canceled the adoption plan.", "event", "fact", "negative"),
                           ("Sam planned to adopt a dog.", "state", "plan", "positive"),
                           ("Sam might adopt a cat.", "state", "uncertain", "unknown")],
        "business": [("Sam closed the shop.", "event", "fact", "negative"),
                     ("Sam opened the shop.", "event", "fact", "positive"),
                     ("Sam ran the shop.", "state", "fact", "positive")],
    }
    rows = histories[scenario]
    source = artifact(count=14 + extra)
    memories = []
    for i, memory in enumerate(source.memories):
        if i < 3:
            text, kind, assertion, polarity = rows[i]
            entity, attr = "Sam", scenario
        elif i < 10:
            text, kind, assertion, polarity = f"Distractor {i}", "state", "fact", "positive"
            entity, attr = None, None
        else:
            text, kind, assertion, polarity = f"Other history {i}", "state", "hypothetical", "positive"
            entity, attr = "Sam", scenario
        if i == 10: entity = "Samantha"  # no substring/alias join
        if i == 11: attr = "unrelated_attribute"
        if i == 12: entity = " "
        updated = memory.model_copy(update={"content": text, "entity": entity, "attribute": attr,
                                            "fact_kind": kind, "assertion_type": assertion, "polarity": polarity,
                                            "recorded_at": datetime(2030 if i == 2 else 2020, 1, 1, tzinfo=timezone.utc)})
        memories.append(updated.model_copy(update={"memory_id": compute_memory_id(updated)}))
    manifest = source.manifest.model_copy(update={"memory_set_fingerprint": memory_set_fingerprint(memories)})
    return MemoryArtifact(manifest=manifest, memories=tuple(memories))


class HistoryEncoder(FakeEncoder):
    def __init__(self, source):
        super().__init__()
        self.query_calls = 0
        self.scores = {m.content: (0.99 if i == 0 else 0.90 if 3 <= i < 10 else 0.1)
                       for i, m in enumerate(source.memories)}

    def embed_documents(self, texts):
        return [[self.scores[text], math.sqrt(1 - self.scores[text] ** 2)] for text in texts]

    def embed_query(self, question):
        self.query_calls += 1
        return [1.0, 0.0]


def run(source, **changes):
    config = settings(**changes)
    encoder = HistoryEncoder(source)
    index = IndexManager(encoder).build_index(source, config)
    request = QueryRequest(query_id="synthetic-history", sample_id=index.sample_id,
                           question="What is Sam's current situation?")
    retriever = VectorRetriever(encoder, config)
    snapshot = retriever.retrieve_with_trace(request, analyze_query(request), index)
    return snapshot, retriever, index, encoder, request


@pytest.mark.parametrize("scenario", ["employment", "adoption_stage", "business"])
def test_needed_history_recalled_without_validity_judgments(scenario):
    source = history_artifact(scenario)
    snapshot, retriever, index, _, _ = run(source)
    required = {m.memory_id for m in source.memories[:3]}
    assert required <= set(snapshot.final_candidate_ids)
    assert not required <= set(snapshot.initial_topk_ids)
    assert len(snapshot.initial_topk_ids) == 8
    assert len(snapshot.expanded_ids) == 3
    assert snapshot.final_candidate_ids[:8] == snapshot.initial_topk_ids
    assert len(set(snapshot.final_candidate_ids)) == len(snapshot.final_candidate_ids)
    loaded = retriever.load_candidates(snapshot.candidates, index)
    assert [m.memory_id for m in loaded] == list(snapshot.final_candidate_ids)
    originals = {m.memory_id: m for m in source.memories}
    assert all(m == originals[m.memory_id] for m in loaded)  # no state decisions or edits
    assert all(i.origin == "history_expansion" for i in snapshot.candidates.items[8:])
    excluded = {m.memory_id for m in source.memories[10:13]}
    assert not excluded.intersection(snapshot.expanded_ids)


def test_budget_truncation_ties_and_pure_vector_are_explicit():
    source = history_artifact(extra=6)
    snapshot, retriever, index, _, _ = run(source)
    assert len(snapshot.final_candidate_ids) == 12
    assert len(snapshot.expanded_ids) == 4
    assert snapshot.expansion_pool_ids == tuple(sorted(snapshot.expansion_pool_ids))
    assert snapshot.expanded_ids == snapshot.expansion_pool_ids[:4]
    assert snapshot.budget_excluded_ids == snapshot.expansion_pool_ids[4:]
    assert tuple(i.memory_id for i in snapshot.pure_vector.items) == snapshot.initial_topk_ids
    assert all(i.origin == "semantic" for i in snapshot.pure_vector.items)
    assert len(retriever.load_candidates(snapshot.pure_vector, index)) == 8
    assert not set(snapshot.expanded_ids).intersection(snapshot.initial_topk_ids)


def test_no_room_and_custom_pure_budget():
    source = history_artifact()
    snapshot, retriever, index, _, _ = run(source, max_candidates=8, optional_pure_vector_top_k=3)
    assert snapshot.expanded_ids == ()
    assert snapshot.budget_excluded_ids == snapshot.expansion_pool_ids
    assert len(snapshot.pure_vector.items) == 3
    assert len(retriever.load_candidates(snapshot.pure_vector, index)) == 3


def test_shared_snapshot_isolated_between_method_consumers_and_reload(tmp_path):
    source = history_artifact()
    snapshot, retriever, index, encoder, request = run(source)
    vector_input, dynamic_input = snapshot.candidates, snapshot.candidates
    assert vector_input == dynamic_input
    assert vector_input is not dynamic_input
    assert encoder.query_calls == 1
    vector_input.items[0].source_refs[0].dia_id = "consumer mutation"
    assert dynamic_input == snapshot.candidates
    assert snapshot.pure_vector.items[0].source_refs[0].dia_id != "consumer mutation"
    path = tmp_path / "index.json"
    save_index(index, path)
    loaded = IndexManager(encoder).load_index(path, source, settings())
    assert retriever.retrieve_with_trace(request, analyze_query(request), loaded) == snapshot
    # Same ID with a changed question must have a distinct trace identity.
    request.question = "Where did Sam work previously?"
    assert retriever.retrieve_with_trace(request, analyze_query(request), loaded).query_fingerprint != snapshot.query_fingerprint


def test_input_order_does_not_change_snapshot():
    source = history_artifact()
    snapshot, retriever, index, encoder, request = run(source)
    reordered = source.model_copy(update={"memories": tuple(reversed(source.memories))})
    index2 = IndexManager(encoder).build_index(reordered, settings())
    assert retriever.retrieve_with_trace(request, analyze_query(request), index2) == snapshot


def test_multiple_anchor_groups_prioritize_initial_rank_not_timestamp():
    source = history_artifact()
    memories = list(source.memories)
    # Give one of the semantic distractors its own supported group and a sibling.
    for position in (3, 10):
        memory = memories[position].model_copy(update={"entity": "Alex", "attribute": "residence"})
        memories[position] = memory.model_copy(update={"memory_id": compute_memory_id(memory)})
    source = MemoryArtifact(manifest=source.manifest.model_copy(
        update={"memory_set_fingerprint": memory_set_fingerprint(memories)}), memories=tuple(memories))
    snapshot, retriever, index, encoder, request = run(source)
    sam_ids = {memories[i].memory_id for i in (1, 2, 13)}
    assert set(snapshot.expanded_ids[:3]) == sam_ids
    assert snapshot.expanded_ids[3] == memories[10].memory_id
    # An as-of query still exposes potentially relevant future/older records to D.
    from datetime import date
    request.as_of = date(2019, 1, 1)
    dated = retriever.retrieve_with_trace(request, analyze_query(request), index)
    assert dated.final_candidate_ids == snapshot.final_candidate_ids
