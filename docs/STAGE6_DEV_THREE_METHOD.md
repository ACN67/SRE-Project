# Stage 6 development three-method runs

Date: 2026-11-04

Status: `HISTORICAL_FIXED_QUESTION_DIAGNOSTIC`

This fixed-question report is retained for audit history. The complete
757-question result and frozen configuration are in
[STAGE6_MAIN_DEV_FINAL.md](STAGE6_MAIN_DEV_FINAL.md).

This report covers four fixed development questions on each of `conv-26`,
`conv-30`, `conv-41` and `conv-42`. Each question ran through Full Text,
Vector RAG and Dynamic. The
results are development diagnostics from four conversations and sixteen
intentionally fixed questions; they are not a complete LoCoMo score or a
test-set claim.

## Execution protocol

- Model: `deepseek-flash`
- Thinking: enabled
- Reasoning effort: high
- API `max_tokens`: omitted
- Automatic retries: 0
- Maximum concurrency per run: 1
- Dynamic state reasoning and Dynamic answer generation share one runtime
- Vector RAG and Dynamic consume the same ordered CandidateSet
- Gold is opened only after every prediction file is reopened and its SHA256
  verified

Each development sample had four Full Text calls, four Vector RAG calls, four
Dynamic state calls and four Dynamic QA calls available under its approval.

## conv-26

Artifact:

- Build: `build_77bdbe5622334acb8fe1352bd2e57052`
- Memory count: 762
- Run fingerprint:
  `sha256:2f475ec20604779c8f18730fc99f626ace339e952bdacf62848c5b7f92458fd2`
- HTTP calls: 16/16 completed
- Reported usage: 64,492 prompt tokens and 14,114 completion tokens
- Conservative peak-price estimate from reported usage: USD `0.0362844`

Question outcomes:

| Method | Answered | No evidence | Time uncertain | Error |
| --- | ---: | ---: | ---: | ---: |
| Full Text | 4 | 0 | 0 | 0 |
| Vector RAG | 3 | 0 | 1 | 0 |
| Dynamic | 4 | 0 | 0 | 0 |

Scores:

| Method | LoCoMo composite | Text F1 | Evidence Recall@5 | Evidence Recall@10 |
| --- | ---: | ---: | ---: | ---: |
| Full Text | 0.000000 | 0.000000 | N/A | N/A |
| Vector RAG | 0.127778 | 0.127778 | 1.00 | 1.00 |
| Dynamic | 0.133114 | 0.133114 | 1.00 | 1.00 |

Dynamic produced 45 decisions across the four CandidateSets.

## conv-30

Artifact:

- Build: `build_bdb0dfc33cc241ce9299a123df2ce091`
- Memory count: 600
- Run fingerprint:
  `sha256:c8745206c8412ac7ba73619a0927852912c0e0860c76f14ed36ceb0c79380bf5`
- HTTP calls: 13, below the 16-call ceiling because some paths stopped early as
  `no_evidence` or `time_uncertain`
- Reported usage: 59,647 prompt tokens and 15,620 completion tokens
- Conservative peak-price estimate from reported usage: USD `0.0366381`

Question outcomes:

| Method | Answered | No evidence | Time uncertain | Error |
| --- | ---: | ---: | ---: | ---: |
| Full Text | 4 | 0 | 0 | 0 |
| Vector RAG | 2 | 2 | 0 | 0 |
| Dynamic | 1 | 2 | 1 | 0 |

Scores:

| Method | LoCoMo composite | Text F1 | Evidence Recall@5 | Evidence Recall@10 |
| --- | ---: | ---: | ---: | ---: |
| Full Text | 0.266494 | 0.263974 | N/A | N/A |
| Vector RAG | 0.305556 | 0.305556 | 0.25 | 0.50 |
| Dynamic | 0.107143 | 0.107143 | 0.25 | 0.50 |

Dynamic produced 41 decisions across the four CandidateSets.

## conv-41

Artifact:

- Build: `build_6b536f48ab734ed8b4f0314542a93826`
- Memory count: 909
- Run fingerprint:
  `sha256:636fc4a46a7fd600ddc9a69cb48aeba9e3bf29789e528167d46cbec159794911`
- HTTP calls: 15/16; one Dynamic path stopped before QA.
- Reported usage: 62,610 prompt tokens and 14,836 completion tokens.
- Conservative peak-price estimate from reported usage: USD `0.0365945`.

| Method | Answered | No evidence | Time uncertain | Error |
| --- | ---: | ---: | ---: | ---: |
| Full Text | 4 | 0 | 0 | 0 |
| Vector RAG | 4 | 0 | 0 | 0 |
| Dynamic | 3 | 1 | 0 | 0 |

| Method | LoCoMo composite | Text F1 | Evidence Recall@5 | Evidence Recall@10 |
| --- | ---: | ---: | ---: | ---: |
| Full Text | 0.070261 | 0.066175 | N/A | N/A |
| Vector RAG | 0.212202 | 0.212202 | 1.00 | 1.00 |
| Dynamic | 0.112500 | 0.112500 | 1.00 | 1.00 |

Dynamic produced 43 decisions across the four CandidateSets.

## conv-42

Artifact:

- Build: `build_f940cab139c04fa480445a47feb49995`
- Memory count: 857
- Run fingerprint:
  `sha256:36805336160b5902dd7f10ab8ee58de2d7390e5f8bfc25623e1fb627c3ed798a`
- HTTP calls: 15/16; several paths stopped before QA.
- Reported usage: 64,465 prompt tokens and 30,885 completion tokens.
- Conservative peak-price estimate from reported usage: USD `0.0564026`.

| Method | Answered | No evidence | Time uncertain | Error |
| --- | ---: | ---: | ---: | ---: |
| Full Text | 4 | 0 | 0 | 0 |
| Vector RAG | 1 | 3 | 0 | 0 |
| Dynamic | 1 | 2 | 1 | 0 |

| Method | LoCoMo composite | Text F1 | Evidence Recall@5 | Evidence Recall@10 |
| --- | ---: | ---: | ---: | ---: |
| Full Text | 0.012821 | 0.012821 | N/A | N/A |
| Vector RAG | 0.000000 | 0.000000 | 0.535714 | 0.642857 |
| Dynamic | 0.000000 | 0.000000 | 0.535714 | 0.642857 |

Dynamic produced 43 decisions across the four CandidateSets.

## Four-sample aggregate

These values aggregate sixteen fixed diagnostic questions across the four
main-dev conversations. Each conversation contributes four questions.

| Method | LoCoMo composite | Evidence Recall@5 | Evidence Recall@10 |
| --- | ---: | ---: | ---: |
| Full Text | 0.087394 | N/A | N/A |
| Vector RAG | 0.161384 | 0.696429 | 0.785714 |
| Dynamic | 0.088189 | 0.696429 | 0.785714 |

Vector RAG has the highest aggregate diagnostic score. Dynamic improves over
Vector RAG on `conv-26`, but underperforms on this selected four-question set
for the other conversations. This mixed result is retained as an observed
research outcome.

## Web replay

The saved predictions were replayed through the Web bindings without another
provider call.

- `conv-26`: 12/12 question-method responses accepted by WebBindings.
- `conv-30`: 12/12 question-method responses accepted by WebBindings.
- `conv-41`: 12/12 question-method responses accepted by WebBindings.
- `conv-42`: 12/12 question-method responses accepted by WebBindings.
- Streamlit AppTest rendered Dynamic results for all four samples without
  exceptions.
- Memory IDs, source references and answer context passed the existing
  interface checks.

## Known limits

- State relationship, decision and effective-set accuracy remains
  `NO_STATE_GOLD`.
- Four questions per conversation are useful for pipeline diagnosis, not for
  estimating overall model quality.
- Dynamic did not outperform Vector RAG on the aggregate diagnostic set. The
  outputs are retained without modification and require later failure analysis.
- This report covers four fixed questions per sample; the full main_dev QA
  inventory remains to be run before formal test execution.
