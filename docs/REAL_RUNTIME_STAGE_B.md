# Stage B: controlled extraction, reviewed publication, local index

This stage is **partial**. The single authorized DeepSeek B1 HTTP attempt returned
`API_STATUS`; no valid draft, reviewed Memory, full `conv-30` artifact or real C
index exists. The 63-window publication and C handoff have passed with an
explicitly synthetic Fake SDK fixture. Do not represent those files as LoCoMo
results. Stage C and D are outside this run.

## Identities and boundaries

- Frozen LoCoMo `data/raw/locomo10.json` SHA-256:
  `79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4`.
  `configs/data_split.yaml` remains fixed. Only `main_dev/conv-30` was used for
  the paid pilot; its complete extraction plan has 63 windows. Conversation
  turns and permitted captions are the only business inputs. Gold QA, answer,
  evidence, observations and summaries are excluded.
- The High opt-in profile is `configs/models_high_pilot.yaml`:
  `deepseek-flash`, thinking enabled, reasoning effort high, 4096 input, 2048
  output, zero retries, one concurrent request. Defaults remain Mock and API
  disabled. Key is read only from Process/User environment inside the process.
- `scripts/stage_b1_pilot.py` is dry-run by default. `--live
  --confirm-low-cost` is the paid gate, with its own one-attempt ledger. **That
  attempt is spent for this run. Do not repeat it.** Full 63-window inference
  needs separate explicit user authorization and a new hard cost/attempt cap.

## B1 and review

The pilot path loads the frozen dataset through the repository, selects a
deterministic caption-free window, renders the shared B prompt, sends through
the A runtime/recorded executor, then uses B extraction and normalization.
The chosen `conv-30` window was `window_b122e288395635db8dc555dc` with
three source refs and estimated input 2025 tokens. The only HTTP attempt failed
with sanitized `API_STATUS`, zero retries and unknown actual usage. No draft or
review queue entry was produced.

`strict_evidence_checker` is independent of the extraction model. It approves
only an exact sentence from one cited speaker turn with visible entity/value,
consistent negation, polarity, plan and explicit time. Caption-only claims,
multi-turn composites, paraphrases and uncertain relationships stay `unclear`
and prevent normalization. This narrow rule is **not a general semantic
entailment proof**. Broader claims require a trusted human reviewer; do not
turn a model's own assertion into an approval. Error codes contain no source
text. A successful raw draft may be kept for manual review only in a private
non-Git directory.

## B2 publication and resume

`MemoryArtifactStore.publish` remains Mock-only. `ReviewedMemoryArtifactStore`
is an opt-in `ArtifactPorts(..., production=True)` path. It requires a complete
`MemoryBuildResult`, the full deterministic window plan, all-success
diagnostics, valid source refs, unchanged model/prompt/config identities, and
a separately supplied `ReviewAttestation` with one approved receipt per
window. Real publication additionally requires the frozen LoCoMo snapshot,
`main_dev` and a non-test reviewer identity. The receipt is a trusted input;
the store verifies its consistency and hashes but cannot prove who performed
the external review. Its model is always recorded as `deepseek-flash` with
thinking enabled/high and `run_kind` (`real` or `offline_test`). Existing Mock
manifest identity is preserved. Immutable build directories, content hashes,
strict load and an atomic current pointer are retained. Failure leaves the
previous pointer intact.

`WindowCheckpointStore` is an optional private per-window extraction draft
store. The caller fixes a run fingerprint covering sample, model, prompt,
policy and settings; each file further hashes its exact window input and
source refs plus A's complete `CallIdentity` (prompt, config, execution policy,
model and data fingerprints). It validates its own bytes on replay. A durable attempt claim is
written **before** a new paid call; an already claimed window without a valid
checkpoint is blocked from automatic retry. It defaults to zero new attempts.
Only a separately authorized runner may configure `max_new_attempts`; run it
single-process/single-concurrency. This is a fail-closed resume mechanism,
not a full-batch API authorization or a batch runner. A crash between provider
success and checkpoint write leaves the attempt spent for manual review.

The automated Fake SDK test builds all 63 synthetic windows, publishes a
non-Mock `offline_test` artifact, verifies strict readback and downstream
consumer preflight. It also rejects partial reports, mismatched receipts,
tampered files, stale checkpoints and repeated claims. It never sends HTTP.

## B3 local resources and index

Persistent model snapshot:
`C:\Users\Keshu\Documents\SRE-Project-RuntimeAssets\models\bge-small-en-v1.5\5c38ec7c405ec4b44b94cc5a9bb96e735b38267a`.
CPU venv:
`C:\Users\Keshu\Documents\SRE-Project-RuntimeAssets\venv-cpu`.
The BGE revision is the path suffix; `model.safetensors` SHA-256 is
`3c9f31665447c8911517620762200d2245a2518d6e7208acc78cd9db317e21ad`.
Ten copied files were checked against their B0 source. The actual local model
fingerprint is `sha256:c01b6c4b1ea551efc212a5482f614512fa6f914cfe33165fd32e59d7bdc61cd3`.
Sentence Transformers 5.7.0, Transformers 5.22.1 and CPU PyTorch 2.15.0 were
installed. No remote code is trusted; loading is offline. The document prompt
is empty and the query prompt is `Represent this sentence for searching
relevant passages: `. The encoder uses normalized 384-dimensional vectors;
its configuration fingerprint includes the real path, prompts and library
versions. Weight or preprocessing changes therefore invalidate the index.

`scripts/stage_b3_synthetic_smoke.py` reuses the explicit Fake fixture to
publish 63 synthetic Memories, then uses C's actual `LocalIndexEncoder`,
`IndexManager` and `VectorRetriever` to build, save, reload and query a real
BGE index. It is **synthetic B input with real local embedding**, not the real
`conv-30` index. The saved files remain in the separate Stage B TEMP evidence
directory, outside Git.

## Reproduction

From this repository root in PowerShell, set `PYTHONPATH` to this checkout's
`src;root` and use the old project Python only for repository tests. For the
B1 free check run `python -B scripts/stage_b1_pilot.py` without live switches.
Do not use its paid switches again for this Stage B ledger. Run tests with
`python -B -m pytest -q -p no:cacheprovider` in this checkout.

For the synthetic B3 check, use the persistent CPU venv and set
`HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1`:

```powershell
& 'C:\Users\Keshu\Documents\SRE-Project-RuntimeAssets\venv-cpu\Scripts\python.exe' -B scripts/stage_b3_synthetic_smoke.py --model 'C:\Users\Keshu\Documents\SRE-Project-RuntimeAssets\models\bge-small-en-v1.5\5c38ec7c405ec4b44b94cc5a9bb96e735b38267a' --revision 5c38ec7c405ec4b44b94cc5a9bb96e735b38267a --output "$env:TEMP\sre-b3-synthetic-replay"
```

Before Stage C, obtain an independently reviewed **real** complete B
artifact, load it through the production ArtifactPorts, and build C's index
from that exact source manifest. A and E Full Text/Vector RAG and Gold
evaluation remain pending. D is out of scope.
