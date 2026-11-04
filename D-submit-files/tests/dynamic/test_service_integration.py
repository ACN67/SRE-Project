"""Actual B/C/A/D service wiring with synthetic completions, never formal scores."""

from concurrent.futures import ThreadPoolExecutor
import json

from dynamic_memory.contracts import QueryRequest
from dynamic_memory.integration.offline import prepare_source, prepare_artifacts, prepare_indexes, create_service
from dynamic_memory.llm.protocol import LLMResponse
from dynamic_memory.pipeline.runtime import create_runtime
from dynamic_memory.reasoning import DynamicServicePorts, DynamicProvenance
from dynamic_memory.reasoning.common import fingerprint
from dynamic_memory.web.bindings import WebBindings


class SyntheticDynamic:
    def generate(self, request):
        if request.task == 'state_reasoning':
            text = request.messages[-1].content
            query = json.loads(text.split('Query data:\n')[1].split('\n\nCandidate context data:')[0])['request']
            memories = json.loads(text.split('Candidate context data:\n')[1].split('\n\nOutput JSON schema:')[0])['memories']
            selected = [m for m in memories if 'notebook' in m['content']]
            raw = {'schema_version': '2.1', 'query_id': query['query_id'], 'relations': [],
                'decisions': [{'memory_id': m['memory_id'], 'action': 'RETAIN' if m in selected else 'EXCLUDE',
                               'reason': 'Synthetic notebook fixture selection.'} for m in memories],
                'effective_memory_ids': [m['memory_id'] for m in selected],
                'unresolved_conflict_ids': [], 'uncertainty': None}
            return LLMResponse(output_mode='json', raw_output=json.dumps(raw), finish_reason='stop')
        text = request.messages[-1].content
        memories = json.loads(text.split('Context data:\n')[1].split('\n\nQuestion data:')[0])
        raw = {'status': 'answered', 'answer': 'Synthetic: blue notebook.',
               'evidence_refs': memories[0]['source_refs'], 'uncertainty': None}
        return LLMResponse(output_mode='text', raw_output=json.dumps(raw), finish_reason='stop')


def test_b_builder_c_index_a_orchestrator_d_ports_and_e_bindings(tmp_path):
    root = tmp_path / 'synthetic-state'
    prepare_source(root)
    prepare_artifacts(root)
    indexes = prepare_indexes(root)
    runtime = create_runtime(synthetic_adapter=SyntheticDynamic(), sleep=lambda _: None)
    ports = DynamicServicePorts(runtime, synthetic_test_mode=True,
        provenance_factory=lambda sample: DynamicProvenance(fingerprint('synthetic-data'),
            'synthetic-analysis-v1', '0' * 40, 'development', indexes[sample]))
    service = create_service(root, resolve=ports.resolve, answer_dynamic=ports.answer_dynamic)
    bindings = WebBindings(service=service, label='Synthetic B/C/A/D/E', synthetic=True, dynamic_available=True)
    def run(n):
        sample = f'synthetic-{n % 2}'
        request = QueryRequest(query_id=f'synthetic-d-{n}', sample_id=sample, question='What does Ada keep?')
        vector = bindings.run_query(request, 'vector_rag')
        dynamic = bindings.run_query(request, 'dynamic')
        assert dynamic.retrieved == vector.retrieved
        assert dynamic.answer_result.status == 'answered'
        assert dynamic.answer_result.answer == 'Synthetic: blue notebook.'
        assert len(dynamic.resolution.decisions) == len(vector.retrieved.items) == 2
        assert len(dynamic.answer_result.used_memory_ids) == 1
        used = dynamic.answer_result.used_memory_ids[0]
        assert 'notebook' in service.get_memory(sample, used).content
        assert service.get_turn(sample, dynamic.answer_result.evidence_refs[0]).text
        return dynamic.sample_id
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(run, range(12)))
    assert set(results) == {'synthetic-0', 'synthetic-1'}
