# Stage A: real runtime preparation (2026-11-03)

## Scope and result

Stage A verified a tiny synthetic DeepSeek call path, the official LoCoMo snapshot against the frozen split, and two local embedding candidates. It did not run real memory extraction, indexing, QA, Gold scoring, or D. Baseline `main`/`origin/main`: `e53aac9ba3ba2762165e7a17a719ed0992f4541d`; no commit or push was made.

| Area | Already present | Verified or added now | Later work |
|---|---|---|---|
| LLM | `DeepSeekLLMAdapter`, explicit opt-in Runtime, guarded retries, JSON/usage checks, Mock default | OpenAI SDK 2.54.0; two synthetic `deepseek-flash` calls; capped smoke entry | Real B extraction, A/E QA and business compatibility |
| Data | Frozen split, snapshot validator, conversation loader, `SourceRepository` | Official SHA match, ten IDs, 5,882 SourceRef checks, repeatability and Gold field isolation | B artifact publication; C index on real memories |
| Retrieval | Local-only `EmbeddingConfig`, `LocalIndexEncoder` file fingerprint, `EncoderIdentity`, `IndexManager` | Two pinned model candidates and machine assessment | Install Torch/ST, download one pinned snapshot, real vector smoke and recall checks |

## DeepSeek safety and protocol

`scripts/stage_a_deepseek_smoke.py` defaults to a dry run. A paid run needs both `--live` and `--confirm-low-cost`. It reads `DEEPSEEK_API_KEY` from Process environment, falling back to Windows User environment in memory. It never accepts a key argument or prints key material. A persistent TEMP ledger claims a slot before each request and rejects a third. The Guarded adapter is set to zero retries and one concurrent request; the SDK client independently sets `max_retries=0` and a 20-second timeout. Only short synthetic content is used. The repo default remains `mock` and API disabled.

The existing adapter uses Chat Completions at `https://api.deepseek.com`, adds `thinking.type=disabled`, and sends `response_format=json_object` for JSON. It rejects blank JSON, abnormal finish reasons, malformed usage, invalid JSON, and model mismatch. Provider exceptions are mapped to sanitized codes. The probe was explicitly run once; its two slots are exhausted. Results: text OK, 9 input/1 output token, 3.516 s; JSON valid `ok: true, label: synthetic`, 51 input/12 output tokens, 1.765 s. Both returned model `deepseek-flash`, finish reason `stop`, with one Guarded attempt and zero retries each. Content was not logged; SHA values and detailed non-sensitive metrics are in the TEMP evidence file. At the official peak, cache-miss Flash rates of $0.30/M input and $1.20/M output, 60 input + 13 output tokens imply an upper estimate of USD 0.0000336; actual billing may differ with cache hits and account terms. Sources: [Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/), [Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/), [JSON Output](https://api-docs.deepseek.com/guides/json_mode/), [model and price](https://api-docs.deepseek.com/quick_start/pricing/), [change log](https://api-docs.deepseek.com/updates/).

## Official LoCoMo snapshot

Downloaded on 2026-11-03 from the [official repository](https://github.com/snap-research/locomo) at commit `3eb6f2c585f5e1699204e3c3bdf7adc5c28cb376`, path `data/locomo10.json`. Its [license](https://github.com/snap-research/locomo/blob/main/LICENSE.txt) is CC BY-NC 4.0; usage here is noncommercial coursework. The ignored local file is `data/raw/locomo10.json`, 2,805,274 bytes, SHA256 `79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4`, exactly matching `configs/data_split.yaml`.

`scripts/verify_locomo_stage_a.py` used `inspect_snapshot`, `validate_split`, `load_conversations`, and `SourceRepository`. Source order is `conv-26, conv-30, conv-41, conv-42, conv-43, conv-44, conv-47, conv-48, conv-49, conv-50`; the frozen groups are 4/4/2 in that order. All 5,882 turns round-trip through SourceRef; 1,226 turns carry allowed image captions. A second load was equal. The public DTO has no `qa`, `answer`, `evidence`, or `summary` fields; the loader builds only conversation DTOs. Raw QA/Gold was neither printed nor sent to DeepSeek. The source file remains Git ignored.

## Embedding candidates and machine boundary

Machine: Intel i7-14650HX (16 cores/24 logical), 15.6 GiB RAM, ~134.5 GiB free on C:, NVIDIA RTX 5060 Laptop GPU with 8,151 MiB total VRAM (7,899 MiB free at inspection). Python 3.11.9; `sentence-transformers` and `torch` are absent. CUDA runtime was therefore not exercised. No model weights were downloaded.

| Candidate | Fixed revision | License | Vector / max input | Safetensors weight | Prompt | Assessment |
|---|---|---|---|---|---|---|
| [sentence-transformers/all-MiniLM-L6-v2](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2) | `1110a243fdf4706b3f48f1d95db1a4f5529b4d41` | Apache-2.0 | 384 / 256 wordpieces | 90,868,376 B | none | Lightweight CPU baseline; long turns truncate sooner. |
| [BAAI/bge-small-en-v1.5](https://huggingface.co/BAAI/bge-small-en-v1.5) | `5c38ec7c405ec4b44b94cc5a9bb96e735b38267a` | MIT | 384 / 512 tokens | 133,466,304 B | short query: `Represent this sentence for searching relevant passages: `; passage: none | Recommended first real retrieval candidate; more room for English history at modest resource cost. |

Revisions and weight sizes came from the Hugging Face model API and pinned repository tree; sizes describe the selected safetensors file, not total installed Torch packages or full cache. Both use `sentence-transformers` plus its Torch/Transformers dependencies and should fit CPU RAM and nominal GPU VRAM, but actual CUDA support and throughput remain unverified. Long conversations still require chunking and retrieval design. Set `query_prompt` for BGE and keep `document_prompt` empty; the current embedder supports both. `LocalIndexEncoder` requires a dedicated local model directory, hashes all files, and uses local-only loading; `IndexManager` checks identity against retrieval settings. Keep `configs/retrieval.yaml` model unset until evaluation.

Suggested later B/C preparation, after approval of a model and environment: install the project's `.[embedding]` extra in a suitable venv, download the chosen immutable revision to a dedicated local cache, run `python -m dynamic_memory.retrieval.embedding_smoke --model <local-path> --revision <commit>` with current repository `src` on `PYTHONPATH`, then validate `LocalIndexEncoder`/`IndexManager` on a tiny synthetic set before real indexing. Do not infer formal Recall from the present work.

## Tests and caveats

The existing DeepSeek/Runtime focused suite: 37 passed. New smoke safety suite: 8 passed. A specific regression was added for whitespace-only JSON with `finish_reason=stop`. Final full suite with this checkout's `src` on `PYTHONPATH`: 1122 passed in 33.53 s; `git diff --check` passed. An earlier full run had one Streamlit AppTest 3-second timeout; its isolated rerun and the subsequent full reruns passed. Running the shared venv without `PYTHONPATH` imported another SRE-Project checkout and failed collection; always set this checkout's `src` path. No production B/C/A/E compatibility or D tests were attempted.
