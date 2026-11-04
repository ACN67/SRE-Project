# Stage 4 non-D readiness

Date: 2026-11-04

Status: `STAGE4_LIVE_COMPLETE`

This report covers the first real `conv-30` non-D QA run. The user separately
authorized the exact run fingerprint and USD ceiling before execution.

## Verified inputs

- Dataset SHA256: `79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4`
- Memory build: `build_bdb0dfc33cc241ce9299a123df2ce091`
- Memory count: 600
- Memory set fingerprint: `sha256:2674d020693bdc43dd9d59bf7661873d41dfcf0bd095656fc215a5155abadec9`
- Review identity: `deepseek-v4.1-flash-semantic-v3`
- BGE index fingerprint: `sha256:34d42d245361282cb40d5d5b1823aa36f5a984283336ba6ff945ec1b0707ec21`
- BGE model revision: `5c38ec7c405ec4b44b94cc5a9bb96e735b38267a`
- Index shape: 600 memories, 384 dimensions

The production loader accepted the current artifact only after separating
downstream QA settings from the immutable extraction configuration identity.
Changing QA token settings no longer makes Memory stale, while a real
extraction setting change still does.

## Fixed questions

The development pilot uses four question texts and their stable original QA
positions. Only question text and identity are used before prediction;
answers and evidence are not read by this runner.

| Query ID | Original index | Question |
| --- | ---: | --- |
| `qa_conv-30_2` | 2 | How do Jon and Gina both like to destress? |
| `qa_conv-30_25` | 25 | What does Jon's dance studio offer? |
| `qa_conv-30_54` | 54 | How is Gina's store doing? |
| `qa_conv-30_80` | 80 | What plans does Jon have after receiving advice at the networking event? |

Each question runs through:

1. `full_text`
2. `vector_rag` using the expanded C candidate set
3. `pure_vector_topk` using C raw Top-8

## Zero-cost dry run

Command:

```powershell
$env:PYTHONPATH = (Get-Location).Path + '\src;' + (Get-Location).Path
$env:PYTHONDONTWRITEBYTECODE = '1'
python -B scripts/stage4_non_d_fixed.py --mode dry-run `
  --output-dir results\stage4_non_d_20261104_dry05
```

Result:

- Status: `STAGE4_DRY_RUN_COMPLETE`
- Real network calls: **0**
- In-memory provider-shaped calls: **12**
- Questions: 4
- Methods: 3
- All 12 dry-run responses were persisted and reopened with SHA verification.
- Full Text, Vector RAG and Pure Vector Top-8 each returned an explicit status
  for every question, with no execution error in the fake provider harness.

The dry-run output directory is
`results/stage4_non_d_20261104_dry05`. Because `results/**` is ignored by Git,
the plan and dry-run evidence remain local.

Full Text used its documented 32,768-token fairness budget. On `conv-30` it
retained 155 recent visible turns and dropped the oldest 214 turns for every
question. This truncation is expected, recorded, and must remain visible in
real results.

The zero-cost retrieval check found the locally reviewed evidence for
questions 54 and 80 in the final candidate set. The chosen direct source turns
for questions 2 and 25 were not in their final top candidates. Other retrieved
turns may still support those answers, but this is a known retrieval-quality
risk and part of what the real pilot should measure.

## Exact paid-run gate

The final dry-run plan fingerprint is:

`sha256:0a2543ca162e67b44a9c347a4ff573b70cd6fafac652360619539088e28581c5`

The paid-run proposal is:

- Provider/model: `deepseek-flash`
- Thinking: enabled
- Reasoning effort: high
- API `max_tokens`: omitted
- Internal input ceiling: 32,768 tokens per call
- Provider default output reserve: 65,536 tokens per call
- New HTTP attempts: at most 12
- Retries: 0
- Maximum concurrency: 1
- Peak cache-miss input price: USD 0.30 per million
- Peak output price: USD 1.20 per million
- Conservative peak ceiling per call: USD 0.0884736
- Conservative peak ceiling for all 12 calls: USD 1.0616832

The price and omitted-parameter behavior were rechecked against DeepSeek's
official Chat Completions and pricing documentation on 2026-11-04 UTC.

No live request may be sent until the user creates a separate authorization
binding this exact run fingerprint and ceiling. The live runner requires:

- A fresh `user-chat-...` authorization ID
- `approved_by=ACN67`
- exact run fingerprint and method list
- exact USD ceiling
- zero retries and one concurrent request
- authorization valid on the day of execution
- the credential present in the local environment or Windows user registry

The wrapper writes one durable attempt record before every SDK request. A
failed or uncertain call stops the run, consumes its attempt, and is never
retried automatically under the same authorization. Completed responses and
usage are appended to the private call ledger before predictions are finalized.

## Verification

- Semantic review focused suite: 107 passed before the Stage 4 isolation fix
- Final affected unit and integration suite: 151 passed
- Unit and contract suites: 1052 passed
- ABCE non-Web repair suite: 11 passed, 4 Web cases deselected
- `git diff --check`: passed
- Live guard without approval: `APPROVAL_REQUIRED`, 0 network calls, and no
  output directory created

The four Streamlit cases were not rerun in this pass because the Streamlit
runner stalled in the constrained execution environment. They belong to Stage
5 and are not used as evidence for the Stage 4 paid-run gate.

## Approved live run

Authorization:

- ID: `user-chat-20261104-stage4-non-d-01`
- Approved by: `ACN67`
- Expiry: `2026-11-04T23:59:59Z`
- Exact run fingerprint:
  `sha256:0a2543ca162e67b44a9c347a4ff573b70cd6fafac652360619539088e28581c5`
- Approved ceiling: USD 1.0616832

Command:

```powershell
python -B scripts/stage4_non_d_fixed.py --mode live `
  --output-dir results\stage4_non_d_20261104_live01 `
  --approval results\stage4_non_d_20261104_approvals\approved.json
```

Result:

- Status: `STAGE4_LIVE_COMPLETE`
- HTTP attempts: 12 of 12
- Attempt states: 12 `completed`, 0 failed
- Retries: 0
- Model returned by every response: `deepseek-flash`
- Finish reason for every response: `stop`
- Prompt tokens reported: 54,171 (34,048 cache hit; 20,123 cache miss)
- Completion tokens reported: 4,069 (3,118 reasoning tokens)
- Peak-price estimate from reported usage: USD 0.011123988
- Authorized worst-case ceiling: USD 1.0616832

Prediction hashes:

- Full Text:
  `98236796fba2f93db2aa584778830100085fee928ab4867e7a73168315b85277`
- Vector RAG:
  `785abefb4d9194ff06e059ec846b525060682f056b39870919119f1680017b88`
- Pure Vector Top-K:
  `01916132738684bd631ac92a7e8506a083e4fa1576cb7ee680c698d366e389cc`

Answer status:

- Full Text: 4 answered
- Vector RAG: 3 answered, 1 `no_evidence`
- Pure Vector Top-8: 2 answered, 2 `no_evidence`

The `no_evidence` outcomes concentrate on the previously identified retrieval
gaps for questions 2 and 25. Vector RAG for question 80 answered with an
explicit qualifier that the supplied context did not confirm the networking
advice premise. These outputs are preserved without correction.

No Gold answer or Gold evidence was opened during prediction or this review.
Stage 5 may now verify these three prediction files and only then open the
authorized development Gold for scoring.
