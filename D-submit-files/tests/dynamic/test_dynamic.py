"""Synthetic D contract/integration tests; prescribed completions are not accuracy scores."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, datetime, timezone
import json

import pytest

from dynamic_memory.contracts import (MemoryRecord, QueryRequest, QueryAnalysis, ResolutionResult,
                                     SourceRef, MemoryDecision)
from dynamic_memory.contracts.memory_id import compute_memory_id
from dynamic_memory.llm.protocol import LLMResponse
from dynamic_memory.pipeline.runtime import create_runtime
from dynamic_memory.qa import DynamicAnswerer
from dynamic_memory.reasoning import (StateResolver, DynamicProvenance, DynamicReasoningError,
                                     DynamicServicePorts)
from dynamic_memory.reasoning.common import fingerprint


def memory(value, n, *, sample='synthetic-d', **kwargs):
    result = MemoryRecord(memory_id='mem_' + '0' * 20, sample_id=sample,
        content=f'Synthetic: Sam works at {value}.', entity='Sam', attribute='job', value=value,
        fact_kind='state', assertion_type='fact', polarity='positive',
        source_refs=[SourceRef(session_id=f's{n}', dia_id='d1')],
        extractor_version='synthetic-d-v1', **kwargs)
    result.memory_id = compute_memory_id(result)
    return result


def query(sample='synthetic-d', intent='current_state'):
    request = QueryRequest(query_id=f'q-{sample}', sample_id=sample, question='Where does Sam work now?')
    analysis = QueryAnalysis(query_id=request.query_id, sample_id=sample,
                             query_intent=intent, time_mode='latest_known')
    return request, analysis


def completion(request, memories, actions=('EXCLUDE', 'RETAIN'), relation='SUPERSEDE', uncertainty=None):
    edges = []
    if relation and len(memories) >= 2:
        left, right = memories[:2]
        if relation == 'CONFLICT' and left.memory_id > right.memory_id:
            left, right = right, left
        edges = [{'from_memory_id': left.memory_id, 'to_memory_id': right.memory_id,
            'relation': relation, 'scope_attribute': 'job',
            'evidence_refs': [r.model_dump() for m in (left, right) for r in m.source_refs],
            'reason': 'Synthetic scenario provides explicit evidence.'}]
    return {'schema_version': '2.1', 'query_id': request.query_id, 'relations': edges,
        'decisions': [{'memory_id': m.memory_id, 'action': action, 'reason': 'Synthetic decision.'}
                      for m, action in zip(memories, actions)],
        'effective_memory_ids': [m.memory_id for m, action in zip(memories, actions) if action == 'RETAIN'],
        'unresolved_conflict_ids': [m.memory_id for m, action in zip(memories, actions)
                                    if action == 'UNRESOLVED' and relation == 'CONFLICT'],
        'uncertainty': uncertainty}


class ScriptedAdapter:
    """Preset transport stub, never a replacement for semantic state reasoning."""
    def __init__(self, payload, change_answer=None):
        self.payload, self.change_answer = payload, change_answer
        self.calls = []

    def generate(self, request):
        self.calls.append(request)
        if request.task == 'state_reasoning':
            return LLMResponse(output_mode='json', raw_output=json.dumps(self.payload), finish_reason='stop')
        context = json.loads(request.messages[1].content.split('Context data:\n')[1].split('\n\nQuestion data:')[0])
        answer = {'status': 'answered', 'answer': 'Synthetic answer: ' + context[-1]['value'],
                  'evidence_refs': context[-1]['source_refs'], 'uncertainty': None}
        answer.update(self.change_answer or {})
        return LLMResponse(output_mode='text', raw_output=json.dumps(answer), finish_reason='stop')


def setup(*, payload=None, actions=('EXCLUDE', 'RETAIN'), relation='SUPERSEDE', uncertainty=None,
          adapter=None, estimator=None, strict=False, synthetic=True, diagnostics=None):
    request, analysis = query()
    memories = [memory('A', 1), memory('B', 2)]
    adapter = adapter or ScriptedAdapter(payload or completion(request, memories, actions, relation, uncertainty))
    runtime = create_runtime(synthetic_adapter=adapter, token_estimator=estimator,
        estimator_version='synthetic-d-budget-v1' if estimator else None, strict_budget=strict,
        sleep=lambda _: None)
    provenance = DynamicProvenance(fingerprint('synthetic-data'), 'synthetic-analysis-v1', '0' * 40,
                                   'development', fingerprint('synthetic-index'))
    kwargs = dict(synthetic_test_mode=synthetic, on_diagnostic=diagnostics.append if diagnostics is not None else None)
    return (StateResolver(runtime, provenance, **kwargs), DynamicAnswerer(runtime, provenance, **kwargs),
            request, analysis, memories, adapter, runtime)


def test_state_selection_actual_context_cache_and_diagnostics():
    diagnostics = []
    resolver, answerer, request, analysis, memories, adapter, _ = setup(diagnostics=diagnostics)
    result = resolver(request, analysis, memories)
    assert result.effective_memory_ids == [memories[1].memory_id]
    answer = answerer(request, analysis, result, memories)
    assert answer.answer == 'Synthetic answer: B'
    assert answer.used_memory_ids == [memories[1].memory_id]
    assert answer.evidence_refs == memories[1].source_refs
    assert memories[0].memory_id not in adapter.calls[-1].messages[-1].content
    assert answerer(request, analysis, resolver(request, analysis, memories), memories) == answer
    assert len(adapter.calls) == 2
    assert diagnostics[-1].cache_hit is True
    assert diagnostics[-1].excluded_memory_ids == (memories[0].memory_id,)
    assert diagnostics[0].call_summary.attempt_count == 1
    assert diagnostics[1].phase == 'answer'


@pytest.mark.parametrize('intent,actions,relation,expected_count', [
    ('historical_state', ('RETAIN', 'EXCLUDE'), 'SUPERSEDE', 1),
    ('state_history', ('RETAIN', 'RETAIN'), 'SUPERSEDE', 2),
    ('event_fact', ('RETAIN', 'EXCLUDE'), 'UPDATE', 1),
    ('general_fact', ('RETAIN', 'RETAIN'), None, 2),
    ('unknown', ('RETAIN', 'RETAIN'), None, 2),
])
def test_query_scoped_historical_and_general_completions(intent, actions, relation, expected_count):
    resolver, answerer, request, analysis, memories, _, _ = setup(actions=actions, relation=relation)
    analysis.query_intent = intent
    result = resolver(request, analysis, memories)
    assert len(result.effective_memory_ids) == expected_count
    answer = answerer(request, analysis, result, memories)
    assert len(answer.used_memory_ids) == expected_count
    assert memories[0].memory_id in answer.used_memory_ids


@pytest.mark.parametrize('actions,relation,status', [
    (('UNRESOLVED', 'UNRESOLVED'), 'CONFLICT', 'unresolved_conflict'),
    (('UNRESOLVED', 'EXCLUDE'), None, 'time_uncertain'),
    (('EXCLUDE', 'EXCLUDE'), None, 'no_evidence'),
])
def test_unresolved_results_never_call_qa(actions, relation, status):
    resolver, answerer, request, analysis, memories, adapter, _ = setup(
        actions=actions, relation=relation, uncertainty='Synthetic ambiguity.')
    result = resolver(request, analysis, memories)
    answer = answerer(request, analysis, result, memories)
    assert answer.status == status and answer.answer is None
    assert answer.used_memory_ids == [] and answer.context_source_refs == []
    assert len(adapter.calls) == 1


def test_unrelated_conflict_does_not_block_answer():
    resolver, answerer, request, analysis, memories, adapter, _ = setup()
    extra = memory('C', 3)
    memories.append(extra)
    adapter.payload = completion(request, memories, ('EXCLUDE', 'EXCLUDE', 'RETAIN'), 'CONFLICT')
    answer = answerer(request, analysis, resolver(request, analysis, memories), memories)
    assert answer.status == 'answered' and answer.used_memory_ids == [extra.memory_id]


def test_empty_and_time_parse_early_stop_no_model():
    resolver, answerer, request, analysis, memories, adapter, _ = setup()
    result = resolver(request, analysis, [])
    assert answerer(request, analysis, result, []).status == 'no_evidence'
    analysis.time_mode = 'unknown'
    result = resolver(request, analysis, memories)
    assert all(d.action == 'UNRESOLVED' for d in result.decisions)
    assert answerer(request, analysis, result, memories).status == 'time_uncertain'
    assert adapter.calls == []


@pytest.mark.parametrize('change', ['missing_decision', 'extra_decision', 'duplicate_decision', 'effective',
    'foreign_edge', 'foreign_ref', 'one_endpoint_ref', 'empty_reason', 'empty_scope', 'query',
    'retained_conflict', 'conflict_without_edge', 'duplicate_edge', 'unresolved_no_reason', 'schema'])
def test_invalid_model_resolutions_rejected_and_not_cached(change):
    resolver, _, request, analysis, memories, adapter, _ = setup()
    valid = deepcopy(adapter.payload)
    p = adapter.payload
    if change == 'missing_decision': p['decisions'].pop()
    elif change == 'extra_decision': p['decisions'].append({'memory_id': 'foreign', 'action': 'EXCLUDE', 'reason': 'bad'})
    elif change == 'duplicate_decision': p['decisions'].append(p['decisions'][0])
    elif change == 'effective': p['effective_memory_ids'] = []
    elif change == 'foreign_edge': p['relations'][0]['to_memory_id'] = 'foreign'
    elif change == 'foreign_ref': p['relations'][0]['evidence_refs'].append({'session_id': 'foreign', 'dia_id': 'd1'})
    elif change == 'one_endpoint_ref': p['relations'][0]['evidence_refs'].pop()
    elif change == 'empty_reason': p['decisions'][0]['reason'] = ' '
    elif change == 'empty_scope': p['relations'][0]['scope_attribute'] = ' '
    elif change == 'query': p['query_id'] = 'other'
    elif change == 'retained_conflict': p['unresolved_conflict_ids'] = [memories[1].memory_id]
    elif change == 'conflict_without_edge':
        p['decisions'][0]['action'] = 'UNRESOLVED'
        p['uncertainty'] = 'Synthetic uncertainty'
        p['unresolved_conflict_ids'] = [memories[0].memory_id]
    elif change == 'duplicate_edge': p['relations'].append(p['relations'][0])
    elif change == 'unresolved_no_reason': p['decisions'][0]['action'] = 'UNRESOLVED'
    elif change == 'schema': p['schema_version'] = '2.0'
    with pytest.raises(DynamicReasoningError): resolver(request, analysis, memories)
    adapter.payload = valid
    assert resolver(request, analysis, memories).effective_memory_ids
    assert len(adapter.calls) == 2


@pytest.mark.parametrize('patch', [
    {'evidence_refs': []}, {'evidence_refs': [{'session_id': 's1', 'dia_id': 'd1'}]},
    {'evidence_refs': [{'session_id': 'private-key-sentinel', 'dia_id': 'd1'}]},
    {'answer': ' '}, {'status': 'no_evidence'}, {'extra': 1},
])
def test_qa_cannot_cite_excluded_or_unknown_sources(patch):
    resolver, answerer, request, analysis, memories, adapter, _ = setup()
    result = resolver(request, analysis, memories)
    adapter.change_answer = patch
    with pytest.raises(DynamicReasoningError) as caught:
        answerer(request, analysis, result, memories)
    assert 'private-key-sentinel' not in str(caught.value)
    adapter.change_answer = None
    assert answerer(request, analysis, result, memories).status == 'answered'
    assert len(adapter.calls) == 3


@pytest.mark.parametrize('value,strict,code', [(999999, True, 'INPUT_BUDGET_EXCEEDED'),
    (None, True, 'INPUT_BUDGET_UNKNOWN'), (-1, False, 'INVALID_ESTIMATE')])
def test_reasoning_budget_no_partial_candidate_loss(value, strict, code):
    resolver, _, request, analysis, memories, adapter, _ = setup(estimator=lambda _: value, strict=strict)
    with pytest.raises(DynamicReasoningError) as caught: resolver(request, analysis, memories)
    assert caught.value.code == code and adapter.calls == []


def test_qa_budget_does_not_silently_drop_effective_evidence():
    def estimator(messages):
        return 999999 if messages[0].content.startswith('Give a short answer') else 10
    resolver, answerer, request, analysis, memories, adapter, _ = setup(estimator=estimator, strict=True)
    result = resolver(request, analysis, memories)
    with pytest.raises(DynamicReasoningError, match='INPUT_BUDGET_EXCEEDED'):
        answerer(request, analysis, result, memories)
    assert len(adapter.calls) == 1


@pytest.mark.parametrize('change', ['cross_sample', 'duplicate', 'forged', 'query', 'unknown_field', 'as_of'])
def test_input_validation_before_model(change):
    resolver, _, request, analysis, memories, adapter, _ = setup()
    if change == 'cross_sample': memories[0].sample_id = 'other'
    elif change == 'duplicate': memories.append(memories[0])
    elif change == 'forged': memories[0].content = 'Forged private content'
    elif change == 'query': analysis.query_id = 'other'
    elif change == 'unknown_field': memories[0] = memories[0].model_copy(update={'secret': 'private'})
    elif change == 'as_of': request.as_of = date(2020, 1, 1)
    with pytest.raises(DynamicReasoningError): resolver(request, analysis, memories)
    assert adapter.calls == []


def test_default_mock_and_unmarked_synthetic_disallowed():
    resolver, _, request, analysis, memories, adapter, _ = setup(synthetic=False)
    with pytest.raises(DynamicReasoningError, match='SYNTHETIC_OUTPUT_DISALLOWED'):
        resolver(request, analysis, memories)
    assert not adapter.calls
    resolver.runtime = create_runtime()
    resolver.synthetic = True
    with pytest.raises(DynamicReasoningError, match='SYNTHETIC_OUTPUT_DISALLOWED'):
        resolver(request, analysis, memories)


def test_cache_binds_question_analysis_candidates_and_cutoff():
    resolver, _, request, analysis, memories, adapter, _ = setup()
    resolver(request, analysis, memories)
    resolver(request, analysis, memories)
    assert len(adapter.calls) == 1
    request.question = 'Which company employs Sam?'
    resolver(request, analysis, memories)
    analysis.query_intent = 'state_history'
    resolver(request, analysis, memories)
    resolver.conversation_cutoff = lambda _: datetime(2024, 1, 1, tzinfo=timezone.utc)
    resolver(request, analysis, memories)
    assert len(adapter.calls) == 4


def test_model_failure_sanitized():
    class Broken(ScriptedAdapter):
        def generate(self, request): raise RuntimeError('private-key-sentinel')
    resolver, _, request, analysis, memories, _, _ = setup(adapter=Broken({}))
    with pytest.raises(DynamicReasoningError) as caught: resolver(request, analysis, memories)
    assert 'private-key-sentinel' not in str(caught.value)


def test_incompatible_resolution_rejected_at_qa_boundary():
    resolver, answerer, request, analysis, memories, adapter, _ = setup()
    result = resolver(request, analysis, memories)
    result.query_id = 'other'
    with pytest.raises(DynamicReasoningError): answerer(request, analysis, result, memories)
    assert len(adapter.calls) == 1


@pytest.mark.parametrize('kind', ['duplicate_json', 'truncated', 'wrong_mode', 'plain_text'])
def test_malformed_completions_fail_closed(kind):
    class Malformed(ScriptedAdapter):
        def generate(self, request):
            if kind == 'duplicate_json':
                return LLMResponse(output_mode='json', raw_output='{"schema_version":"2.1","query_id":"x","query_id":"y"}')
            if kind == 'plain_text':
                return LLMResponse(output_mode='text', raw_output='private-key-sentinel')
            if kind == 'wrong_mode':
                return LLMResponse(output_mode='text', raw_output='{}')
            return LLMResponse(output_mode='json', raw_output=json.dumps(self.payload), finish_reason='content_filter')
    request, _ = query()
    memories = [memory('A', 1), memory('B', 2)]
    resolver, _, request, analysis, memories, _, _ = setup(adapter=Malformed(completion(request, memories)))
    with pytest.raises(DynamicReasoningError): resolver(request, analysis, memories)


def test_provably_reversed_event_chronology_is_rejected():
    from dynamic_memory.contracts import TimeSpan
    resolver, _, request, analysis, memories, adapter, _ = setup()
    for m, year in zip(memories, (2024, 2023)):
        m.event_time = TimeSpan(start=datetime(year, 1, 1), end_exclusive=datetime(year, 1, 2),
                                precision='day', resolution='explicit')
        m.memory_id = compute_memory_id(m)
    adapter.payload = completion(request, memories)
    with pytest.raises(DynamicReasoningError): resolver(request, analysis, memories)


def test_conflict_must_include_both_endpoints_as_unresolved():
    resolver, _, request, analysis, memories, adapter, _ = setup(
        actions=('UNRESOLVED', 'EXCLUDE'), relation='CONFLICT', uncertainty='Synthetic conflict')
    with pytest.raises(DynamicReasoningError): resolver(request, analysis, memories)


def test_qa_uncertainty_allowed_with_evidence_scope_preserved():
    resolver, answerer, request, analysis, memories, adapter, _ = setup()
    result = resolver(request, analysis, memories)
    adapter.change_answer = {'status': 'time_uncertain', 'answer': None, 'uncertainty': 'Synthetic missing temporal proof.'}
    answer = answerer(request, analysis, result, memories)
    assert answer.status == 'time_uncertain' and answer.answer is None
    assert answer.used_memory_ids == [memories[1].memory_id]


def test_time_conflict_uses_shared_analyzer_and_no_model():
    from dynamic_memory.retrieval import analyze_query
    resolver, answerer, request, _, memories, adapter, _ = setup()
    request.question, request.as_of = 'Where did Sam work in 2020?', date(2021, 1, 1)
    analysis = analyze_query(request)
    assert analysis.time_mode == 'conflict'
    result = resolver(request, analysis, memories)
    assert answerer(request, analysis, result, memories).status == 'time_uncertain'
    assert adapter.calls == []


def test_real_deepseek_adapter_path_with_in_memory_sdk_client():
    from types import SimpleNamespace
    resolver, answerer, request, analysis, memories, adapter, _ = setup()
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        if 'response_format' in kwargs:
            raw = json.dumps(adapter.payload)
        else:
            raw = json.dumps({'status': 'answered', 'answer': 'Synthetic SDK completion: B.',
                              'evidence_refs': memories[1].model_dump(mode='json')['source_refs'],
                              'uncertainty': None})
        return SimpleNamespace(model='synthetic-sdk-model',
            choices=[SimpleNamespace(message=SimpleNamespace(content=raw), finish_reason='stop')],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20))
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    runtime = create_runtime(backend='deepseek', api_enabled=True, model_id='synthetic-sdk-model',
                             deepseek_client=client)
    resolver.runtime = answerer.runtime = runtime
    resolver.synthetic = answerer.synthetic = False
    result = resolver(request, analysis, memories)
    answer = answerer(request, analysis, result, memories)
    assert answer.status == 'answered'
    assert calls[0]['response_format'] == {'type': 'json_object'}
    assert 'response_format' not in calls[1]
    assert calls[0]['max_tokens'] == 2048 and calls[1]['max_tokens'] == 256
    assert calls[0]['temperature'] == calls[1]['temperature'] == 0
    assert len(calls) == 2


def test_superseded_future_plan_can_be_cancelled_before_planned_date():
    from dynamic_memory.contracts import TimeSpan
    resolver, _, request, analysis, memories, adapter, _ = setup()
    for m, year in zip(memories, (2025, 2024)):
        m.event_time = TimeSpan(start=datetime(year, 1, 1), end_exclusive=datetime(year, 1, 2),
                                precision='day', resolution='explicit')
    memories[0].assertion_type = 'plan'
    memories[1].polarity = 'negative'
    for m in memories: m.memory_id = compute_memory_id(m)
    adapter.payload = completion(request, memories)
    assert resolver(request, analysis, memories).effective_memory_ids == [memories[1].memory_id]


def test_empty_unknown_time_is_no_evidence_not_claimed_temporal_evidence():
    resolver, answerer, request, analysis, _, adapter, _ = setup()
    analysis.time_mode = 'unknown'
    result = resolver(request, analysis, [])
    assert answerer(request, analysis, result, []).status == 'no_evidence'
    assert adapter.calls == []


def test_incomplete_candidates_do_not_become_confirmed_answers():
    resolver, answerer, request, analysis, memories, adapter, _ = setup(
        uncertainty='candidate_incomplete: missing required earlier evidence.')
    result = resolver(request, analysis, memories)
    answer = answerer(request, analysis, result, memories)
    assert answer.status == 'no_evidence' and answer.answer is None
    assert len(adapter.calls) == 1
