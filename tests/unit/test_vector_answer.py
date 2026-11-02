"""Synthetic QA through the actual shared runtime, without network or paid API."""

import json

import pytest

from dynamic_memory.contracts.models import QueryResponse, SourceTurn
from dynamic_memory.contracts.validation import validate_response_context
from dynamic_memory.llm.protocol import LLMResponse
from dynamic_memory.pipeline.runtime import create_runtime
from dynamic_memory.retrieval import VectorAnswerer, VectorAnswerProvenance, VectorAnswerError, QUERY_ANALYSIS_VERSION
from dynamic_memory.retrieval.index import fingerprint
from test_vector_retrieval import setup


class SyntheticQA:
    def __init__(self, change=None):
        self.calls = []
        self.change = change

    def generate(self, request):
        self.calls.append(request)
        context = request.messages[1].content.split("Context data:\n", 1)[1].split("\n\nQuestion data:", 1)[0]
        memories = json.loads(context)
        payload = {"status": "answered", "answer": "Synthetic: Sam is a teacher.",
                   "evidence_refs": memories[0]["source_refs"], "uncertainty": None}
        if self.change: payload.update(self.change)
        return LLMResponse(output_mode="text", raw_output=json.dumps(payload), finish_reason="stop")


def answer_setup(*, adapter=None, estimator=None, strict=False, synthetic=True, diagnostics=None):
    _, index, request, analysis, retriever = setup()
    candidates = retriever.retrieve(request, analysis, index)
    memories = retriever.load_candidates(candidates, index)
    adapter = adapter or SyntheticQA()
    runtime = create_runtime(synthetic_adapter=adapter, token_estimator=estimator,
                             estimator_version="synthetic-estimator-v1" if estimator else None,
                             strict_budget=strict, sleep=lambda _: None)
    provenance = VectorAnswerProvenance(fingerprint("synthetic-data"), QUERY_ANALYSIS_VERSION, "0" * 40, "development")
    answerer = VectorAnswerer(runtime, provenance, index, synthetic_test_mode=synthetic,
                             on_diagnostic=diagnostics.append if diagnostics is not None else None)
    return answerer, request, analysis, memories, adapter, candidates, runtime


def assert_error(code, call):
    with pytest.raises(VectorAnswerError) as caught:
        call()
    assert caught.value.code == code
    assert "private" not in str(caught.value)


def test_answer_public_context_contract_and_cache():
    diagnostics = []
    answerer, request, analysis, memories, adapter, candidates, runtime = answer_setup(diagnostics=diagnostics)
    answer = answerer(request, analysis, memories)
    assert answer.status == "answered"
    assert answer.used_memory_ids == [m.memory_id for m in memories]
    assert answer.evidence_refs == memories[0].source_refs
    response = QueryResponse(query_id=request.query_id, sample_id=request.sample_id, question=request.question,
                             method="vector_rag", query_analysis=analysis, retrieved=candidates,
                             resolution=None, answer_result=answer)
    turns = [SourceTurn(sample_id=m.sample_id, session_id=r.session_id, dia_id=r.dia_id,
                        speaker="Synthetic Sam", text=m.content) for m in memories for r in m.source_refs]
    validate_response_context(response, memories, turns)
    assert answerer(request, analysis, memories) == answer
    assert len(adapter.calls) == 1
    assert diagnostics[-1].included_memory_ids == tuple(answer.used_memory_ids)
    assert not diagnostics[-1].input_budget_verified
    request.question = "What job does Sam have?"
    answerer(request, analysis, memories)
    assert len(adapter.calls) == 2


def test_whole_memory_prefix_budget_and_no_outside_citations():
    diagnostics = []
    def estimator(messages):
        return 999999 if messages[1].content.count('"memory_id"') > 1 else 10
    answerer, request, analysis, memories, adapter, _, _ = answer_setup(estimator=estimator, strict=True, diagnostics=diagnostics)
    answer = answerer(request, analysis, memories)
    assert answer.used_memory_ids == [memories[0].memory_id]
    assert diagnostics[0].dropped_memory_ids == (memories[1].memory_id,)
    assert diagnostics[0].input_budget_verified
    assert memories[1].memory_id not in adapter.calls[0].messages[1].content
    adapter = SyntheticQA({"evidence_refs": [memories[1].source_refs[0].model_dump()]})
    answerer, request, analysis, memories, _, _, _ = answer_setup(adapter=adapter, estimator=estimator)
    assert_error("MODEL_FAILED", lambda: answerer(request, analysis, memories))


@pytest.mark.parametrize("estimate,strict,code", [(None, True, "INPUT_BUDGET_UNKNOWN"),
                                                   (-1, False, "INVALID_ESTIMATE"),
                                                   (True, False, "INVALID_ESTIMATE"),
                                                   (999999, True, "INPUT_BUDGET_EXCEEDED")])
def test_budget_failure_never_calls_model(estimate, strict, code):
    answerer, request, analysis, memories, adapter, _, _ = answer_setup(estimator=lambda _: estimate, strict=strict)
    assert_error(code, lambda: answerer(request, analysis, memories))
    assert adapter.calls == []


def test_empty_and_conflicting_time_early_stop():
    from datetime import date
    from dynamic_memory.retrieval import analyze_query
    answerer, request, analysis, memories, adapter, _, _ = answer_setup()
    assert answerer(request, analysis, []).status == "no_evidence"
    request.question = "Where did Sam work in 2020?"
    request.as_of = date(2021, 1, 1)
    assert answerer(request, analyze_query(request), memories).status == "time_uncertain"
    assert adapter.calls == []


@pytest.mark.parametrize("change,code", [("sample", "SAMPLE_MISMATCH"), ("query", "QUERY_ANALYSIS_MISMATCH"),
                                         ("memory", "CANDIDATES_INVALID"), ("duplicate", "CANDIDATES_INVALID")])
def test_bad_inputs_rejected_before_model(change, code):
    answerer, request, analysis, memories, adapter, _, _ = answer_setup()
    if change == "sample": request.sample_id = "other"
    elif change == "query": analysis.query_id = "other"
    elif change == "memory": memories[0].content = "private forged content"
    else: memories.append(memories[0])
    assert_error(code, lambda: answerer(request, analysis, memories))
    assert adapter.calls == []


@pytest.mark.parametrize("change", [{"evidence_refs": []}, {"answer": " "},
                                     {"evidence_refs": [{"session_id": "private", "dia_id": "forged"}]},
                                     {"status": "no_evidence"}, {"extra": "bad"}])
def test_invalid_answers_are_not_cached(change):
    adapter = SyntheticQA(change)
    answerer, request, analysis, memories, _, _, _ = answer_setup(adapter=adapter)
    assert_error("MODEL_FAILED", lambda: answerer(request, analysis, memories))
    adapter.change = None
    assert answerer(request, analysis, memories).status == "answered"
    assert len(adapter.calls) == 2


def test_model_uncertainty_and_explicit_synthetic_gate():
    adapter = SyntheticQA({"status": "no_evidence", "answer": None, "evidence_refs": [], "uncertainty": "Not enough evidence."})
    answerer, request, analysis, memories, _, _, _ = answer_setup(adapter=adapter)
    assert answerer(request, analysis, memories).status == "no_evidence"
    answerer, request, analysis, memories, adapter, _, _ = answer_setup(synthetic=False)
    assert_error("SYNTHETIC_ANSWER_DISALLOWED", lambda: answerer(request, analysis, memories))
    assert adapter.calls == []


def test_provider_failure_sanitized():
    class Broken(SyntheticQA):
        def generate(self, request):
            raise RuntimeError("private credentials and question")
    answerer, request, analysis, memories, _, _, _ = answer_setup(adapter=Broken())
    assert_error("MODEL_FAILED", lambda: answerer(request, analysis, memories))


@pytest.mark.parametrize("mode", ["truncated", "duplicate_keys", "plain_text"])
def test_incomplete_or_ambiguous_text_envelope_is_rejected(mode):
    class Invalid(SyntheticQA):
        def generate(self, request):
            valid = super().generate(request)
            if mode == "truncated": return valid.model_copy(update={"finish_reason": "length"})
            if mode == "duplicate_keys":
                return valid.model_copy(update={"raw_output": '{"status":"answered","status":"no_evidence"}'})
            return valid.model_copy(update={"raw_output": "Synthetic plain answer without verifiable citation."})
    answerer, request, analysis, memories, _, _, _ = answer_setup(adapter=Invalid())
    assert_error("MODEL_FAILED", lambda: answerer(request, analysis, memories))


def test_default_mock_cannot_be_presented_as_business_answer():
    answerer, request, analysis, memories, _, _, _ = answer_setup()
    answerer._runtime = create_runtime()
    assert_error("SYNTHETIC_ANSWER_DISALLOWED", lambda: answerer(request, analysis, memories))


@pytest.mark.parametrize("change", ["query_id", "question", "analysis"])
def test_diagnostics_identify_query_even_when_memories_are_identical(change):
    from dataclasses import asdict
    diagnostics = []
    answerer, request, analysis, memories, _, candidates, _ = answer_setup(diagnostics=diagnostics)
    answerer(request, analysis, memories)
    answerer(request, analysis, memories)
    assert diagnostics[0] == diagnostics[1]  # Stable on identical query/cache hit.
    if change == "query_id":
        request.query_id = analysis.query_id = "another-query"
    elif change == "question":
        request.question = "What work does Sam do?"
    else:
        analysis.query_intent = "state_history"
    answerer(request, analysis, memories)
    record = diagnostics[-1]
    assert record.included_memory_ids == diagnostics[0].included_memory_ids
    assert record.query_fingerprint != diagnostics[0].query_fingerprint
    assert record.query_id == request.query_id
    assert record.sample_id == request.sample_id
    assert record.retriever_fingerprint == candidates.retriever_fingerprint
    assert record.query_fingerprint == fingerprint({"request": request.model_dump(mode="json"),
        "analysis": analysis.model_dump(mode="json"), "index": candidates.retriever_fingerprint})
    serialized = json.dumps(asdict(record))
    assert request.question not in serialized
    assert all(m.content not in serialized for m in memories)


@pytest.mark.parametrize("path", ["empty", "conflict", "unknown_budget", "exceeded_budget"])
def test_early_diagnostics_keep_query_identity(path):
    from datetime import date
    from dynamic_memory.retrieval import analyze_query
    diagnostics = []
    estimator = (lambda _: None) if path == "unknown_budget" else (lambda _: 999999)
    answerer, request, analysis, memories, _, candidates, _ = answer_setup(
        diagnostics=diagnostics, estimator=estimator, strict=True)
    if path == "empty":
        answerer(request, analysis, [])
    elif path == "conflict":
        request.question = "Where did Sam work in 2020?"
        request.as_of = date(2021, 1, 1)
        analysis = analyze_query(request)
        answerer(request, analysis, memories)
    else:
        code = "INPUT_BUDGET_UNKNOWN" if path == "unknown_budget" else "INPUT_BUDGET_EXCEEDED"
        assert_error(code, lambda: answerer(request, analysis, memories))
    assert len(diagnostics) == 1
    assert diagnostics[0].query_id == request.query_id
    assert diagnostics[0].sample_id == request.sample_id
    assert diagnostics[0].query_fingerprint == fingerprint({"request": request.model_dump(mode="json"),
        "analysis": analysis.model_dump(mode="json"), "index": candidates.retriever_fingerprint})
