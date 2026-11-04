"""Run D on an explicitly supplied, upstream-verified candidate snapshot."""

import argparse
from dataclasses import asdict
from datetime import datetime
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from dynamic_memory.contracts import MemoryRecord, QueryAnalysis, QueryRequest
from dynamic_memory.pipeline.runtime import create_runtime
from dynamic_memory.qa import DynamicAnswerer
from dynamic_memory.reasoning import DynamicProvenance, StateResolver
from dynamic_memory.reasoning.smoke import run_smoke


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--smoke', action='store_true', help='Scripted synthetic demo; no API or accuracy score.')
    source.add_argument('--input', type=Path, help='JSON containing verified request/analysis/candidates/provenance/cutoff.')
    parser.add_argument('--output', type=Path, help='Create a new result file; never overwrite existing files.')
    parser.add_argument('--api-enabled', action='store_true', help='Explicitly enable a real DeepSeek API call.')
    parser.add_argument('--model-id', help='Explicit model ID; no implicit production model selection.')
    args = parser.parse_args()
    try:
        if args.smoke:
            if args.api_enabled or args.model_id:
                parser.error('--smoke cannot enable a real API/model')
            result = run_smoke()
        else:
            if not args.api_enabled or not args.model_id:
                parser.error('real input requires --api-enabled and --model-id; credentials use DEEPSEEK_API_KEY')
            data = json.loads(args.input.read_text(encoding='utf-8'))
            if set(data) != {'request', 'analysis', 'candidates', 'provenance', 'conversation_cutoff'}:
                raise ValueError('invalid input envelope')
            request = QueryRequest.model_validate(data['request'])
            analysis = QueryAnalysis.model_validate(data['analysis'])
            memories = [MemoryRecord.model_validate(m) for m in data['candidates']]
            provenance = DynamicProvenance(**data['provenance'])
            cutoff = datetime.fromisoformat(data['conversation_cutoff']) if data['conversation_cutoff'] else None
            diagnostics = []
            # Conservative byte estimate, with per-message framing reserve.
            # This is a local budget bound, not a provider tokenizer or exact count.
            runtime = create_runtime(backend='deepseek', api_enabled=True, model_id=args.model_id,
                api_key=os.environ.get('DEEPSEEK_API_KEY'), strict_budget=True,
                token_estimator=lambda messages: sum(len(m.content.encode('utf-8')) + 64 for m in messages),
                estimator_version='d-utf8-byte-upper-estimate-v1')
            resolver = StateResolver(runtime, provenance, conversation_cutoff=lambda _: cutoff,
                                     on_diagnostic=diagnostics.append)
            answerer = DynamicAnswerer(runtime, provenance, on_diagnostic=diagnostics.append)
            resolution = resolver(request, analysis, memories)
            answer = answerer(request, analysis, resolution, memories)
            result = {'synthetic_fixture': False, 'not_a_formal_score': True,
                'resolution': resolution.model_dump(mode='json'), 'answer': answer.model_dump(mode='json'),
                'diagnostics': [asdict(d) for d in diagnostics]}
        serialized = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            with args.output.open('x', encoding='utf-8') as stream:
                stream.write(serialized + '\n')
        else:
            print(serialized)
    except Exception as exc:
        # Do not echo provider bodies, paths, question text, credentials or JSON.
        code = getattr(exc, 'code', None)
        print(f'Dynamic run failed: {code if isinstance(code, str) and code.isupper() else "INPUT_OR_EXECUTION_INVALID"}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
