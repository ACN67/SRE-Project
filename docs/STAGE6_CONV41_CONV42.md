# Stage 6 conv-41 and conv-42 memory pipelines

Date: 2026-11-04

Status: `CONV41_CONV42_MEMORY_PIPELINE_COMPLETE`

This report covers real extraction, v3 semantic review, strict Memory
publication, BGE indexing, production wiring and fixed-question three-method QA
for the remaining two `main_dev` conversations.

## conv-41

Extraction:

- Windows: 123/123
- Draft facts: 1,100
- HTTP extraction calls: 123
- Run fingerprint:
  `sha256:c0a83159ea0d01763b4777169a40c7c3d5ceafda27cc354c86539ff6ff2b377a`
- Queue fingerprint:
  `sha256:51ae3abb8d15a792d02672f7caca76187b2565b754343bb795109813f59e218d`

Semantic review:

- Approved: 918
- Unclear: 182
- Rejected: 0
- Reviewer: `deepseek-v4.1-flash-semantic-v3`
- Review fingerprint:
  `sha256:14b429f418443605664f6adb316e872b2f9a09fdc213e991c7d8934fb49c1059`

Publication:

- Build ID: `build_6b536f48ab734ed8b4f0314542a93826`
- Memory count: 909
- Memory set fingerprint:
  `sha256:8ea0d51519492bff2dcce2f710f63b0bfcb5f737bba4c583953d3d4cfba76373`
- Index fingerprint:
  `sha256:463e5490c6faf264f70f9cb1a7bc9abe05399202605a4ffd6a4f9def3d7b87c9`
- Retrieval smoke: 12 candidates
- Fixed-question three-method run: 15 real HTTP calls
- LoCoMo composite: Full Text `0.070261`, Vector RAG `0.212202`,
  Dynamic `0.112500`
- Evidence Recall@5/@10: Vector and Dynamic both `1.00 / 1.00`

## conv-42

Extraction:

- Windows: 108/108
- Draft facts: 1,131
- HTTP extraction calls: 108
- Run fingerprint:
  `sha256:f97de8e16d37ebe1fa875c30089967b4311a11bcb311149d9b790a16a8247fc5`
- Queue fingerprint:
  `sha256:ee070a36394be9f8cf5115f4f1b7e4a55500bc9b41885b8f1a9adec0ed07348b`

Semantic review:

- Approved: 868
- Unclear: 263
- Rejected: 0
- Reviewer: `deepseek-v4.1-flash-semantic-v3`
- Review fingerprint:
  `sha256:dd7cdbb905fddba0823fb4a42f495a72534896cfb597eb0066f417c74b8e8d57`

Publication:

- Build ID: `build_f940cab139c04fa480445a47feb49995`
- Memory count: 857
- Memory set fingerprint:
  `sha256:de124429cca76ee46b2277b6010f15ab2df48c97374eec5d942ec381a0e52523`
- Index fingerprint:
  `sha256:4367b4f5886aa15f740936d18780ddc052306fd38e08900222b021af6e32cc0e`
- Retrieval smoke: 11 candidates
- Fixed-question three-method run: 15 real HTTP calls
- LoCoMo composite: Full Text `0.012821`, Vector RAG `0.000000`,
  Dynamic `0.000000`
- Evidence Recall@5/@10: Vector and Dynamic both `0.535714 / 0.642857`

## A/B/C/D/E wiring

- `conv-41`: real artifact and index load, same 12 ordered candidates for
  Vector RAG and Dynamic, 12 decisions and 12 effective IDs, 0 HTTP calls.
- `conv-42`: real artifact and index load, same 11 ordered candidates for
  Vector RAG and Dynamic, 11 decisions and 11 effective IDs, 0 HTTP calls.
- Web AppTest loaded 909 and 857 memories respectively without exceptions.
- Both Web workspaces expose `full_text`, `vector_rag` and `dynamic`.
- Saved fixed-question predictions were replayed through WebBindings, including
  Dynamic results and original-source navigation, without extra provider calls.

## Evidence

- `results/stage6_conv41_extract02/extraction_summary.json`
- `results/stage6_conv41_semantic_complete02/semantic_review_approval.json`
- `results/stage6_conv41_publication01/publication_summary.json`
- `results/stage6_dev41_three_live01/run_summary.json`
- `results/stage6_dev41_eval01/stage6_dev_summary.json`
- `results/stageD_conv41_wiring01/summary.json`
- `results/stage6_conv42_extract02/extraction_summary.json`
- `results/stage6_conv42_semantic_complete02/semantic_review_approval.json`
- `results/stage6_conv42_publication01/publication_summary.json`
- `results/stage6_dev42_three_live01/run_summary.json`
- `results/stage6_dev42_eval01/stage6_dev_summary.json`
- `results/stageD_conv42_wiring01/summary.json`

## Pending

- Full-question three-method runs for these samples.
- Independent Dynamic state Gold and formal state metrics.
- Formal test conversations and final acceptance.
