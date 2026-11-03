# Stage B four-window repair runbook

The original Prompt v3 batch remains read-only. It has 59 validated checkpoints
and four spent, failed windows. The new `repair4` mode selects only those four
original window IDs, with their unchanged input bytes and SourceRefs. It stops
after **one** new request per invocation. A failure is spent; the same call is
never retried automatically. The old authorization cannot fund the repair.

## Offline preparation and approval boundary

1. Preserve the private `baseline.json` made before editing code. It records
   hashes for all 59 old checkpoints, the original ledger, registry, approval,
   data, and all 63 window inputs/SourceRefs.
2. Run the targeted and full offline tests after the last code change. Confirm
   `git diff --check` and inspect the dry run:
   `python -B -m scripts.stage_b_repair_runner --mode repair4 --dry-run`.
3. Recheck DeepSeek's official Flash pricing and omitted `max_tokens` default
   **on the approval date**. Peak reserve is USD 0.3589824 per window and
   USD 1.4359296 for four one-time requests, at USD 0.30/M cache-miss input,
   USD 1.20/M output, 934464 input and 65536 output token ceilings.
4. Obtain a separate user approval file: v2 `BatchApproval`, mode `repair4`,
   a fresh `authorization_id`, exact ordered `selected_window_ids`, data,
   plan and run fingerprints, 4 maximum new attempts, daily price/default
   verification dates, and expiry. This task book is not that approval.
5. Only then invoke `stage_b_enroll_authorization --mode repair4` with
   `--confirm-separate-user-approval`. Enrollment creates a zero-attempt
   marker. It makes no HTTP call. Use a new state directory outside Git.

## Controlled repair

Use `stage_b_repair_runner --mode repair4 --live --confirm-paid-run` with the
new approval file, authorization ID and isolated state directory. Each call
rechecks the original 59 checkpoint bytes and the old ledger/registry/approval
SHA before it can reserve a new attempt. The command sends at most one HTTP
request per invocation. Inspect its sanitized result, usage ledger and
checkpoint before invoking it again. Stop after a failure, unknown spend,
network/credential/budget error, or approval/code fingerprint change. A
changed prompt or code needs its own separate authorization; do not amend an
existing paid approval or ledger.

After four valid real checkpoints exist, run the **zero-network**
`stage_b_merge_repair` command with the new state and approval paths. It
revalidates both ledgers and all 63 checkpoints and writes a separate
`merged/manifest.json` and `merged/queue.json`. The manifest includes each
window's original run fingerprint, call fingerprint, authorization, approval
hash, checkpoint hash, input hash, SourceRef fingerprint, prompt and code
fingerprints. It never edits the original run. Missing, duplicate or stale
origins reject the merge. Disk readback is checked twice.

The merged queue is **ready for independent human fact review**, not an
approved artifact. Automatic evidence checks may flag unclear facts. No
`ReviewApproval`, `TrustedReviewAuthority`, production MemoryArtifact or BGE
index may be created by this repair task without a real independent reviewer.

Official references: DeepSeek [pricing](https://api-docs.deepseek.com/quick_start/pricing/)
and [Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion/).
