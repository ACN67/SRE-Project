# Stage 5 non-D evaluation and Web

Date: 2026-11-04

Status: `STAGE5_NON_D_PASS`

This stage evaluates the four fixed Stage 4 predictions and verifies the real
non-D Web path. It does not claim Dynamic RAG, D state metrics, or full system
acceptance.

## Gold isolation

The evaluation runner reopened and verified all three prediction files by
their recorded SHA256 before opening the raw Gold fields. The question subset
was then checked against the frozen `main_dev` snapshot, split, original QA
index, question text and stable query ID.

Gold was opened as `course_gold`. State metrics were not opened or substituted:

`NOT_TESTED / BLOCKED_BY_D`

## QA score

Four questions were scored for each method. All four had Gold; there were no
missing Gold records or execution failures.

| Method | LoCoMo composite mean | Conditioned mean | Text F1 main |
| --- | ---: | ---: | ---: |
| Full Text | 0.291826 | 0.291826 | 0.293841 |
| Vector RAG | 0.180344 | 0.180344 | 0.180344 |
| Pure Vector Top-8 | 0.303618 | 0.303618 | 0.303618 |

These figures cover one conversation and four deliberately selected questions.
They are a pipeline and error-diagnosis result, not a general accuracy claim.
Pure Vector's higher score on this tiny set must not be described as a formal
three-method comparison because Dynamic is unavailable and the denominator is
only four questions.

Per-question scores:

| Query | Category | Full Text | Vector RAG | Pure Top-8 |
| --- | ---: | ---: | ---: | ---: |
| `qa_conv-30_2` | 4 | 0.066667 | 0.000000 | 0.000000 |
| `qa_conv-30_25` | 1 | 0.420513 | 0.000000 | 0.000000 |
| `qa_conv-30_54` | 4 | 0.071429 | 0.380952 | 0.888889 |
| `qa_conv-30_80` | 4 | 0.608696 | 0.340426 | 0.325581 |

## Evidence-turn recall

Full Text has no ranked Memory candidate set, so Evidence Recall is
`not_applicable`.

| Method | Recall@5 | Recall@10 |
| --- | ---: | ---: |
| Full Text | N/A | N/A |
| Vector RAG | 0.25 | 0.50 |
| Pure Vector Top-8 | 0.25 | 0.50 |

Vector RAG and Pure Top-8 have the same recall on this sample. The observed
candidate sets still differ in size and extension composition.

## Real Web

Production non-D bindings were implemented in
`src/dynamic_memory/integration/production.py`. They load the strict B artifact,
real C BGE index and A service. Startup performs no model request.

Verified through the Streamlit server and Streamlit AppTest:

- Health endpoint returned HTTP 200.
- The only exposed sample is `conv-30`.
- Memory browse displayed 600 records with a ready banner.
- A memory detail loaded its public fields and exact original turn.
- Image caption remained separate from speaker text.
- Service status displayed dataset `ready`, Memory `complete`, build
  `build_bdb0dfc33cc241ce9299a123df2ce091`, and index `ready`.
- Before the later D integration, the query page exposed `full_text` and
  `vector_rag` only. The current production factory now also exposes
  `dynamic`; the Stage 5 non-D result above remains unchanged.
- A conflicting-date query returned `time_uncertain` before retrieval or
  generation; the call ledger remained at 12/12.

The browser control surface rejected the localhost navigation by product
permission policy, so no graphical screenshot was captured. No circumvention
was attempted. Streamlit AppTest verified the same page states and interactions.

## Evidence

- Stage score summary:
  `results/stage5_non_d_20261104_eval01/stage5_summary.json`
- QA scoring manifests:
  `results/stage5_non_d_20261104_eval01/<method>/qa_scoring_manifest.json`
- Evidence recall:
  `results/stage5_non_d_20261104_eval01/<method>/evidence_recall.json`
- Web checks and logs:
  `results/stage5_web_live01`

No additional paid HTTP call was made during scoring or Web verification.

Verification totals:

- Web suite: `93 passed`
- Unit and contract suite: `1053 passed`
- ABCE non-Web integration repairs: `11 passed`
- `git diff --check`: passed
