# Stage 6 paid-run approval gate

Date: 2026-11-04

Status: `CONV26_SEMANTIC_REVIEW_APPROVAL_REQUIRED`

The remaining `main_dev` conversations do not have real extraction outputs.
No HTTP request may be made for them until a new authorization binds the exact
sample, run fingerprint, call count, model policy and USD ceiling.

## Zero-cost window plans

The extraction prompt was constructed offline for every planned window. Input
bytes are used as a conservative token upper bound. The output reserve is the
service default of 65,536 tokens for each call.

| Sample | Visible turns | Windows / max attempts | Request input bytes | Peak worst extraction cost |
| --- | ---: | ---: | ---: | ---: |
| `conv-26` | 419 | 83 | 263,895 | USD 6.6065541 |
| `conv-41` | 663 | 123 | 389,127 | USD 9.7898517 |
| `conv-42` | 629 | 108 | 338,614 | USD 8.5950498 |

Shared policy:

- Model: `deepseek-flash`
- Thinking: enabled
- Reasoning effort: high
- API `max_tokens`: omitted
- Peak cache-miss input: USD 0.30 per million tokens
- Output: USD 1.20 per million tokens
- Retries: 0
- Maximum concurrency: 1

These are conservative extraction ceilings, not forecasts. Actual usage in the
`conv-30` non-D QA pilot was much lower than its ceiling.

## Remaining approvals

For each sample, the following stages require independent approvals:

1. Real extraction: one HTTP attempt per window, maximum counts above.
2. Semantic review: one HTTP attempt per completed window, with an exact queue,
   request fingerprints, input bytes and cost ceiling generated after
   extraction. This cannot be approved meaningfully before extraction.
3. Non-D QA: twelve HTTP attempts per sample for four fixed questions across
   Full Text, Vector RAG and Pure Vector Top-K. The current worst-case ceiling is
   USD 1.0616832 per sample.

The next actionable approval is the `conv-26` semantic review. PR/merge, D
integration and final test do not belong to this batch.

## conv-26 first attempt

The authorized first run stopped exactly at its first schema failure:

- Completed checkpoints: 62 of 83
- Attempt records: 63 (62 successful, 1 failed)
- Failed window: `window_30022e3f2cccff3fecf0545b`
- Failure code: `SCHEMA_INVALID`
- Facts in successful checkpoints: 639
- Successful-call usage: 53,859 prompt tokens and 431,728 completion tokens
  (368,161 reasoning tokens)
- Known peak-price estimate for 62 successful calls: USD 0.531898116
- The failed call's provider usage is unknown and is not counted as zero

The runner did not continue to windows 63–83 and did not retry the failed
window. A separate repair approval is required because the first approval
specified zero retries.

Proposed repair batch:

- Windows: failed window plus the remaining 20 windows
- Maximum new HTTP attempts: 21
- Model profile and maximum concurrency: unchanged
- Retry model generation: one fresh attempt for the failed window, still no
  SDK retries
- Remaining request input bytes upper bound: 67,800
- Peak worst-case cost ceiling: USD 1.6718472
- Prefix-preserving source state: all 62 completed checkpoints are read-only

## conv-26 repair result

The approved repair completed all remaining windows.

- Final successful windows: 83/83
- Draft facts: 887
- Repair HTTP attempts: 21/21
- Repair attempt states: 21 completed
- Original checkpoint prefix: 62 files, all SHA256 values unchanged
- Queue fingerprint:
  `sha256:3859a4dd5fa106c74eab1c981a1f3291ea6d14e314c1a5d5de87fa752d63785e`
- Repair-call usage: 18,603 prompt and 153,779 completion tokens
- Repair peak-price estimate: USD 0.189137268

The original run used 63 attempts (62 successful plus one schema failure).
Repair used 21 attempts (the retry plus 20 remaining windows). No automatic
retry occurred inside either batch.

## Next approval

The formal v3 semantic review plan is ready:

- Exact run fingerprint:
  `sha256:842b0cf7a82f2ec05a2555fba21be788c1d414e09244d275406b50ba95c0b193`
- Queue: 83 windows, 887 facts
- Maximum new HTTP attempts: 83
- Model: `deepseek-flash`, thinking/high, API `max_tokens` omitted
- Retries: 0
- Maximum concurrency: 1
- Conservative input bytes: 748,281
- Peak worst-case cost ceiling: USD 6.7518699
