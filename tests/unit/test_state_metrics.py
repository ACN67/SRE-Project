"""Explicit synthetic annotations and hand-calculated state denominators."""

from dataclasses import replace

import pytest

from dynamic_memory.contracts import (AnswerResult, CandidateItem, CandidateSet,
    ConversationSample, ConversationSession, DatasetBundle, MemoryDecision, MemoryRecord,
    QueryResponse, RelationEdge, ResolutionResult, SourceRef, SourceTurn)
from dynamic_memory.data.repository import SourceRepository
from evaluation.metrics.state_metrics import (DecisionGold, GoldItemMapping, MemoryCatalog,
    RelationGold, StateGold, score_state, validate_gold_memory_mapping)
from evaluation.question_manifest import EvaluationInputError


def fixture():
    sample = "synthetic-state"
    refs = [SourceRef(session_id="s", dia_id=f"d{i}") for i in range(1, 4)]
    sources = SourceRepository(DatasetBundle(snapshot_sha256="a" * 64,
        samples=[ConversationSample(sample_id=sample, source_order_1_based=1,
            sessions=[ConversationSession(session_id="s", turns=[SourceTurn(
                sample_id=sample, session_id="s", dia_id=f"d{i}", speaker="Synthetic", text="synthetic")
                for i in range(1, 4)])])], split_by_sample_id={sample: "main_dev"}))
    memories = tuple(MemoryRecord(memory_id=f"mem_{i:020x}", sample_id=sample,
        content=f"Synthetic fact {i}", entity="Person", attribute="role", value=f"role{i}",
        fact_kind="state", assertion_type="fact", polarity="positive", source_refs=[ref],
        extractor_version="synthetic") for i, ref in enumerate(refs, 1))
    catalog = MemoryCatalog(sample, "synthetic-memory-fp", "synthetic-data-fp", memories, True)
    maps = tuple(GoldItemMapping(f"g{i}", sample, (ref,), "Person", "role", f"role{i}",
        (memory.memory_id,), "manual source and field check", "synthetic_fixture",
        catalog.data_fingerprint, catalog.fingerprint)
        for i, (ref, memory) in enumerate(zip(refs, memories), 1))
    gold = StateGold(sample, "q", "synthetic_fixture",
        (RelationGold("g1", "g2", "role", "UPDATE"),
         RelationGold("g1", "g3", "role", "NO_RELATION")),
        (DecisionGold("g1", "EXCLUDE"), DecisionGold("g2", "RETAIN"),
         DecisionGold("g3", "UNRESOLVED")), ("g2",), True)
    candidates = CandidateSet(query_id="q", sample_id=sample, base_top_k=2, max_candidates=3,
        retriever_fingerprint="synthetic-retriever", items=[CandidateItem(
            memory_id=m.memory_id, rank=i, score=1/i, source_refs=m.source_refs, origin="semantic")
            for i, m in enumerate(memories, 1)])
    resolution = ResolutionResult(query_id="q", relations=[RelationEdge(
        from_memory_id=memories[0].memory_id, to_memory_id=memories[1].memory_id,
        relation="UPDATE", scope_attribute="role", reason="synthetic")],
        decisions=[MemoryDecision(memory_id=m.memory_id, action=action, reason="synthetic")
                   for m, action in zip(memories, ("EXCLUDE", "RETAIN", "UNRESOLVED"))],
        effective_memory_ids=[memories[1].memory_id])
    response = QueryResponse(query_id="q", sample_id=sample, question="Synthetic?", method="dynamic",
        query_analysis=None, retrieved=candidates, resolution=resolution,
        answer_result=AnswerResult(status="no_evidence", uncertainty="synthetic"))
    return response, gold, catalog, maps, sources


def test_hand_calculated_relations_decisions_effective_and_negative():
    response, gold, catalog, maps, sources = fixture()
    report = score_state(response, gold, catalog, maps, sources)
    relation, decision, effective = report.relation_label_metrics, report.decision_metrics, report.effective_set_metrics
    assert (relation.total_gold_units, relation.mapped_units, relation.candidate_covered_units,
            relation.scored_units, relation.end_to_end_accuracy) == (2, 2, 2, 2, 1)
    assert relation.confusion == (("NO_RELATION", "NO_RELATION", 1), ("UPDATE", "UPDATE", 1))
    assert (decision.total_gold_units, decision.scored_units, decision.end_to_end_accuracy) == (3, 3, 1)
    assert decision.confusion == (("EXCLUDE", "EXCLUDE", 1), ("RETAIN", "RETAIN", 1),
                                  ("UNRESOLVED", "UNRESOLVED", 1))
    assert (effective.precision, effective.recall, effective.f1, effective.exact_match) == (1, 1, 1, True)


def test_candidate_missing_and_model_error_denominators():
    response, gold, catalog, maps, sources = fixture()
    shortened = response.retrieved.model_copy(update={"items": response.retrieved.items[:2]})
    resolution = response.resolution.model_copy(update={"decisions": response.resolution.decisions[:2]})
    partial = response.model_copy(update={"retrieved": shortened, "resolution": resolution})
    report = score_state(partial, gold, catalog, maps, sources)
    assert (report.relation_label_metrics.total_gold_units, report.relation_label_metrics.candidate_covered_units,
            report.relation_label_metrics.candidate_missing_units,
            report.relation_label_metrics.end_to_end_accuracy,
            report.relation_label_metrics.candidate_conditioned_accuracy) == (2, 1, 1, .5, 1)
    assert (report.decision_metrics.total_gold_units, report.decision_metrics.candidate_covered_units,
            report.decision_metrics.candidate_missing_units, report.decision_metrics.end_to_end_accuracy) == (3, 2, 1, 2/3)
    assert report.effective_set_metrics.conditional_f1 == 1
    changed_decisions = tuple(replace(decision, label="RETAIN" if decision.item_id == "g3" else "EXCLUDE")
                              for decision in gold.decisions)
    missed_effective = score_state(partial, replace(gold, effective_item_ids=("g3",),
                                                  decisions=changed_decisions), catalog, maps, sources)
    assert missed_effective.effective_set_metrics.status == "candidate_missing"
    assert missed_effective.effective_set_metrics.f1 == 0
    assert missed_effective.effective_set_metrics.conditional_f1 is None
    failed = response.model_copy(update={"resolution": None, "answer_result": AnswerResult(
        status="error", error={"code": "MODEL_FAILED", "message": "synthetic"})})
    failed_report = score_state(failed, gold, catalog, maps, sources)
    assert failed_report.relation_label_metrics.model_error_units == 2
    assert failed_report.relation_label_metrics.end_to_end_accuracy == 0
    assert failed_report.effective_set_metrics.status == "model_error"
    assert failed_report.effective_set_metrics.f1 == 0


def test_direction_attribute_and_conflicting_labels():
    response, gold, catalog, maps, sources = fixture()
    reversed_edge = RelationEdge(from_memory_id=catalog.memories[1].memory_id,
        to_memory_id=catalog.memories[0].memory_id, relation="UPDATE", scope_attribute="role", reason="synthetic")
    wrong = response.model_copy(update={"resolution": response.resolution.model_copy(update={"relations": [reversed_edge]})})
    report = score_state(wrong, gold, catalog, maps, sources)
    assert report.relation_label_metrics.end_to_end_accuracy == .5
    assert report.relation_label_metrics.unexpected_predictions == 1
    wrong_attribute = RelationEdge(from_memory_id=catalog.memories[0].memory_id,
        to_memory_id=catalog.memories[1].memory_id, relation="UPDATE", scope_attribute="location", reason="synthetic")
    changed = response.model_copy(update={"resolution": response.resolution.model_copy(update={"relations": [wrong_attribute]})})
    assert score_state(changed, gold, catalog, maps, sources).relation_label_metrics.unexpected_predictions == 1
    on_negative = RelationEdge(from_memory_id=catalog.memories[0].memory_id,
        to_memory_id=catalog.memories[2].memory_id, relation="UPDATE", scope_attribute="role", reason="synthetic")
    changed = response.model_copy(update={"resolution": response.resolution.model_copy(update={
        "relations": [response.resolution.relations[0], on_negative]})})
    assert score_state(changed, gold, catalog, maps, sources).relation_label_metrics.end_to_end_accuracy == .5
    conflict = replace(gold, relations=gold.relations + (RelationGold("g1", "g2", "role", "SUPERSEDE"),))
    with pytest.raises(EvaluationInputError, match="RELATION_GOLD_CONFLICT"):
        score_state(response, conflict, catalog, maps, sources)
    outside = RelationEdge(from_memory_id=catalog.memories[0].memory_id,
        to_memory_id="missing", relation="UPDATE", scope_attribute="role", reason="synthetic")
    changed = response.model_copy(update={"resolution": response.resolution.model_copy(update={"relations": [outside]})})
    with pytest.raises(EvaluationInputError, match="STATE_RESOLUTION_INVALID"):
        score_state(changed, gold, catalog, maps, sources)


def test_mapping_ambiguity_missing_and_provenance():
    response, gold, catalog, maps, sources = fixture()
    ambiguous = replace(maps[2], matched_memory_ids=(catalog.memories[1].memory_id, catalog.memories[2].memory_id),
                        match_status="ambiguous")
    report = score_state(response, gold, catalog, maps[:2] + (ambiguous,), sources)
    assert report.decision_metrics.unmapped_units == 1
    missing = replace(maps[2], matched_memory_ids=(), match_status="extraction_missing")
    report = score_state(response, gold, catalog, maps[:2] + (missing,), sources)
    assert report.decision_metrics.extraction_missing_units == 1
    incomplete = replace(catalog, complete=False)
    with pytest.raises(EvaluationInputError, match="GOLD_MAPPING_INVALID"):
        score_state(response, gold, incomplete, maps[:2] + (missing,), sources)
    report = score_state(response, gold, incomplete, maps[:2] + (replace(missing, match_status="unmapped"),), sources)
    assert report.decision_metrics.unmapped_units == 1
    with pytest.raises(EvaluationInputError, match="GOLD_MAPPING_ID_INVALID"):
        validate_gold_memory_mapping(catalog, maps[:2] + (replace(maps[2], matched_memory_ids=("missing",)),), sources)
    with pytest.raises(EvaluationInputError, match="GOLD_MAPPING_SOURCE_INVALID"):
        validate_gold_memory_mapping(catalog, maps[:2] + (replace(maps[2], source_refs=(SourceRef(session_id="s", dia_id="absent"),)),), sources)


def test_gold_decisions_must_agree_with_explicit_effective_set():
    response, gold, catalog, maps, sources = fixture()
    # A Gold EXCLUDE cannot simultaneously be declared effective.
    contradictory = replace(gold, effective_item_ids=("g1",))
    with pytest.raises(EvaluationInputError, match="STATE_GOLD_INCONSISTENT"):
        score_state(response, contradictory, catalog, maps, sources)
    # A declared RETAIN cannot disappear from an explicitly provided effective set.
    contradictory = replace(gold, effective_item_ids=())
    with pytest.raises(EvaluationInputError, match="STATE_GOLD_INCONSISTENT"):
        score_state(response, contradictory, catalog, maps, sources)
    # Partial decision annotation does not invent labels for undeclared items.
    partial = replace(gold, decisions=(DecisionGold("g1", "EXCLUDE"),))
    result = score_state(response, partial, catalog, maps, sources)
    assert result.effective_set_metrics.f1 == 1
    assert result.decision_metrics.total_gold_units == 1
    # When effective-set Gold is unavailable, no cross-metric consistency is presumed.
    unavailable = replace(gold, effective_item_ids=None)
    assert score_state(response, unavailable, catalog, maps, sources).effective_set_metrics.status == "gold_unavailable"


def test_empty_effective_and_gold_unavailable():
    response, gold, catalog, maps, sources = fixture()
    empty_candidates = response.retrieved.model_copy(update={"items": []})
    empty_resolution = ResolutionResult(query_id="q")
    empty_response = response.model_copy(update={"retrieved": empty_candidates, "resolution": empty_resolution})
    empty_gold = replace(gold, relations=(), decisions=(), effective_item_ids=())
    report = score_state(empty_response, empty_gold, catalog, maps, sources)
    assert (report.effective_set_metrics.precision, report.effective_set_metrics.recall,
            report.effective_set_metrics.f1, report.effective_set_metrics.exact_match) == (1, 1, 1, True)
    unavailable = score_state(response, None, catalog, (), sources)
    assert unavailable.relation_label_metrics.status == "gold_unavailable"
    assert unavailable.decision_metrics.status == "gold_unavailable"
    assert unavailable.effective_set_metrics.status == "gold_unavailable"
