# Stage 6 conv-26 memory pipeline

Date: 2026-11-04

Status: `CONV26_MEMORY_PIPELINE_COMPLETE`

This report covers `main_dev/conv-26` extraction repair, v3 semantic review,
strict Memory publication, a real BGE index, and no-network A/B/C/D/E
production wiring. It does not claim paid Dynamic quality, three-method QA, or
independent-test acceptance.

## Extraction

- Windows: 83/83 complete.
- Draft facts: 887.
- Original successful prefix: 62 checkpoints.
- Repair batch: the first failed window plus 20 remaining windows.
- Repair HTTP attempts: 21/21, with no SDK retry.
- Original prefix checkpoints remained byte-identical.
- Queue fingerprint:
  `sha256:3859a4dd5fa106c74eab1c981a1f3291ea6d14e314c1a5d5de87fa752d63785e`

## Semantic review

The first 83-call review batch produced 30 replayable checkpoints and one paid
response whose JSON was malformed. The 52 never-attempted windows were then
reviewed in a separate batch. A separately authorized one-call repair retried
only the malformed window.

- Valid checkpoints before repair: 82.
- Repair calls: 1, with zero retries.
- Repair result: 5 approved, 4 unclear, 0 rejected.
- Final checkpoints: 83/83, with all 82 prefix hashes unchanged.
- Final facts: 887.
- Approved: 771.
- Unclear: 116.
- Rejected: 0.
- Reviewer: `deepseek-v4.1-flash-semantic-v3`.
- Review fingerprint:
  `sha256:99e11243cdccec5d08a277e2b01ee84e5c2666eaed65378554d3878179ce1fd9`

The final approval, authority, receipts, and every checkpoint were replayed by
`verify_review_files` with `run_kind=real`.

## Published Memory

Publication used `scripts/stage6_publish_sample.py`, which performed zero
network model calls. It reopened the immutable queue, loaded and validated all
83 extraction checkpoints, recomputed approved facts, reran strict evidence
checks, normalized the records, merged conservative duplicates, published the
artifact, and loaded it again through the production artifact port.

- Build ID: `build_77bdbe5622334acb8fe1352bd2e57052`
- Memory count: 762
- Memory set fingerprint:
  `sha256:187c79b3febce81ffe2b3423656de3b67cf0b864dcd77015abed96d60b8d8d23`
- Manifest SHA256:
  `sha256:5821e777ecae715451450cf259f9f9cfd29b18dd2e9dcae43af7562791a76bbc`
- Queue SHA256:
  `sha256:3859a4dd5fa106c74eab1c981a1f3291ea6d14e314c1a5d5de87fa752d63785e`

## BGE index

- Model: `bge-small-en-v1.5`
- Revision: `5c38ec7c405ec4b44b94cc5a9bb96e735b38267a`
- Dimension: 384
- Index fingerprint:
  `sha256:e84e0602e01c52b4988b22d0ff41e8ca16417fba03cf36d677733157d650c508`
- No-network retrieval smoke: 9 ordered candidates, all source IDs verified
  against the published artifact.

## A/B/C/D/E production wiring

The production factory now accepts an explicit `SRE_PRODUCTION_SAMPLE_ID`.

- Web AppTest loaded 762 `conv-26` Memories without exceptions.
- The only exposed sample was `conv-26`.
- Query methods were `full_text`, `vector_rag`, and `dynamic`.
- A real artifact and real BGE index were composed with a fake provider-shaped
  D client and produced zero real network calls.
- Vector RAG and Dynamic observed the same 9 ordered candidates.
- Dynamic produced 9 decisions and 9 effective IDs in the no-network smoke.

These checks prove wiring and contract compatibility. They do not prove model
state accuracy or answer quality.

## Evidence

- Publication summary:
  `results/stage6_conv26_publication01/publication_summary.json`
- Checkpoint and lineage audit:
  `results/stage6_conv26_publication01/lineage_audit.json`
- D wiring summary:
  `results/stageD_conv26_wiring01/summary.json`

## Fixed-question three-method verification

The fixed four-question development pilot now includes Full Text, Vector RAG,
and Dynamic. Predictions were verified by SHA before the Gold fields were
opened.

- Dynamic LoCoMo composite: `0.133114`
- Vector RAG composite: `0.127778`
- Full Text composite: `0.000000`
- Vector and Dynamic Evidence Recall@5/@10: `1.00 / 1.00`
- Dynamic decisions across four questions: 45
- Real Web Dynamic query and original-source navigation: passed

See [Stage 6 development three-method run](STAGE6_DEV_THREE_METHOD.md).

## Pending

- Dynamic relation, decision, and effective-set metrics against independent
  state Gold.
- Full main_dev question set rather than the four-question diagnostic subset.
- Independent test data and final system acceptance.
