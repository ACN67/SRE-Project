# Stage B repair: safe diagnosis, human review and batch gate

The later High extraction output policy, durable authorization registry, reject/unclear review and C handoff are documented in [REAL_RUNTIME_STAGE_B_FINAL.md](REAL_RUNTIME_STAGE_B_FINAL.md). The 2048-token and price figures below describe the old B-R4 implementation and must not authorize a new paid run.

**Current result: offline implementation only.** No new DeepSeek inference is
authorized by this task book. The previous single B1 attempt remains spent
(`b1_api_attempts.json` = 1/1); its archived event has `API_STATUS` but no
numeric HTTP code. The exact server response and root cause are not
recoverable from the sanitized record. Do not reset or reuse that ledger.

## B-R1: what the evidence says

The prior Stage A probe used one short user message, nonthinking mode and
128/256 output tokens. B0's successful High probe used one short synthetic
user message, 200 input and 2048 output tokens. B1 used the same
`deepseek-flash` High model as B0, but with a system and user message,
`response_format=json_object`, a substantially longer extraction schema and
conversation payload (2025 input token *estimate*; 4096/2048 ceilings).
Those are confirmed request differences. A schema/parameter rejection,
account/balance issue, rate limit and server error are possibilities requiring
new evidence. None is established as the prior B1 status. The old event
cannot distinguish them.

The current [DeepSeek error table](https://api-docs.deepseek.com/quick_start/error_codes/)
lists 400, 401, 402, 422, 429, 500 and 503 categories. The
[Chat Completions specification](https://api-docs.deepseek.com/api/create-chat-completion/),
[JSON mode guide](https://api-docs.deepseek.com/guides/json_mode/) and
[thinking guide](https://api-docs.deepseek.com/guides/thinking_mode/) describe
the B1 request's `response_format`, `extra_body.thinking` and
`reasoning_effort` fields. This confirms documented parameter shape, **not**
that the previous server accepted the specific B1 request. The OpenAI Python
SDK in the test venv is 2.54.0. No real request was sent for this diagnosis.

`DeepSeekAdapterError` retains its original external `code` while carrying
`safe_status_code` (integer or null) and a fixed `safe_reason`. The guarded
summary and recorded event carry only those fields, never provider text,
headers or exception repr. Fake SDK tests cover 400/401/402/403/422/429/
500/503, unknown and absent status; they also exercise an HTTP error with
`response.status_code`. Zero automatic retries remain configured. Old B1's
number remains **unknown** because its event predates this change.

## B-R2: independent review boundary

The conservative EvidenceChecker only approves an exact single speaker
sentence and checks entity, attribute, value, explicit time, negation,
polarity and plan/fact labels in that sentence. Caption-only, paraphrase,
multi-source and relationship inference remain unclear. It is intentionally
narrower than semantic entailment.

After every window has a validated draft checkpoint, the batch runner writes
`private_review/queue.json` under a user-local TEMP state directory. It has
the exact drafts and cited SourceTurns for human inspection; never copy it to
Git or a desktop report. `scripts/stage_b_review_tool.py` can inspect counts
and create a **PENDING** template. It never creates an approved decision or
reviewer authority. An independent reviewer must compare the private queue's
source references, text and facts, fill the approval JSON, and have a
separate manually provisioned authority file that allowlists the reviewer,
exact run, queue, approval file SHA, run kind and expiry. Both files live
outside the runner's state directory. `--verify` checks their exact structure
and hashes. A rejected, missing, stale, duplicate or cross-window decision
cannot be imported. Zero-fact windows still need an explicit approved window
decision. The verified review is bound to B's per-window normalization and
the production store rechecks each fact's source turns and reconstructed
Memory set before a real publish.

This is a **manual trust process**, not cryptographic proof of a person's
identity. A local user who can rewrite both independent files can forge the
claim. The runner never produces an approval file or authority entry, and
the report must identify who performed actual review before a real publish.
The 63-window review shown in tests uses a visibly fake `offline_test`
reviewer and synthetic data only.

## B-R3: runner and resume contract

`scripts/stage_b_repair_runner.py` defaults to `--dry-run`, using the frozen
`main_dev/conv-30` loader, without a provider call or new ledger. It checks
the frozen LoCoMo SHA, split, 63-window plan, previous pilot window identity,
model/prompt/High settings, input byte estimate and relevant source code
fingerprints. A code, prompt, source or plan change changes the run identity
and prevents reusing old checkpoints.

The `--live` route requires a separate human supplied approval file, matching
authorization ID, explicit `--confirm-paid-run`, today's checked official
peak prices ($0.30 per million uncached input tokens, $1.20 per million
output tokens), unexpired deadline and fixed High model/snapshot/plan. The
single-window mode requires exactly one new attempt and at most USD 0.02.
The batch mode is independently approved, at most 63 new HTTP attempts.
Every new window reserves a 4096 input / 2048 output ceiling and worst-case
cost **before** calling the SDK. The real budget ledger is fixed under
`%LOCALAPPDATA%\SRE-Project-BudgetLedgers\<authorization-id>`; changing
`--state-dir` cannot reset it. A cross-process run lock prevents concurrent
spending. SDK and guarded retries are both zero. A failure, missing usage or
crash leaves a spent reservation. The checkpoint and budget ledger cannot
silently refund or repeat it. Local raw draft checkpoints stay under the
separate private TEMP state directory.

`--resume` without `--live` reads valid checkpoints, builds a private review
queue and, when separate review/authority files are provided, may finish
review, publication and indexing with **zero new requests**. It cannot fill
missing windows. `--status` is read-only; with an approval file it validates
the budget ledger and displays sanitized attempt/usage totals. The runner
stops at the first extraction error, budget denial or stale checkpoint. A
reported zero-fact window is accepted only after valid JSON and explicit
window approval. A successful pilot ends at `PILOT_REVIEWED` without a full
sample artifact. A complete batch can publish via `ArtifactPorts(...,
production=True)` and then build C's fixed local BGE index, verify readback
and test a source-safe no-Gold query. The pilot and batch run identities are
distinct: **pilot results are not automatically reused** in a batch.

Fake SDK tests run all 63 windows, pause after 5, resume without repeating
those calls, import separately supplied fake review files, publish and index.
Other tests inject 402, a crash after spend, corrupt ledger/checkpoint,
review queue drift, budget exhaustion, concurrent execution, zero facts and
duplicate publish. These tests establish program behaviour only; they are
not paid DeepSeek or real LoCoMo production evidence.

## Reproduction and future authorization

Run from the repository root with `PYTHONPATH` set to this checkout's
`src;root` and the Stage A/B test venv Python:

```powershell
python -B scripts/stage_b_repair_runner.py --dry-run
python -B scripts/stage_b_repair_runner.py --mode pilot --dry-run
python -B scripts/stage_b_repair_runner.py --status --state-dir <private-TEMP-state>
python -B scripts/stage_b_repair_runner.py --resume --state-dir <private-TEMP-state> --approval-file <human-approved-budget-file> --authorization-id <user-approved-id> --review-file <separate-review-file> --authority-file <separate-authority-file>
```

The following **command structure is not permission to execute it**. It may
be used only after the user separately approves a new pilot or whole batch,
including max calls, tokens, USD ceiling, expiry and reviewer arrangement:

```powershell
python -B scripts/stage_b_repair_runner.py --mode pilot --live --confirm-paid-run --state-dir <new-private-TEMP-state> --approval-file <human-approved-budget-file> --authorization-id <user-approved-id>
python -B scripts/stage_b_repair_runner.py --mode batch --live --confirm-paid-run --state-dir <new-private-TEMP-state> --approval-file <separate-batch-budget-file> --authorization-id <separate-user-approved-id>
```

The approval file must match `BatchApproval` and be placed outside the state
directory by the user. This project never creates a real approval file.
Before any future paid run, recheck the
[current official prices](https://api-docs.deepseek.com/quick_start/pricing/)
and API availability. The old B1, A and B0 ledgers stay untouched. Current
real attempts in this repair run: **0**. Real `conv-30` MemoryArtifact and C
index: **absent**. Stage C, E and D are still outside this work.
