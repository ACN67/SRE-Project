---
name: dynamic-memory-web
description: Develop and integrate member E's Streamlit memory browser with the Dynamic Memory schema 2.1 service. Use for memory filtering, details, source inspection, UI tests, and service handoff in this repository.
---

# Dynamic Memory Web

Read `docs/WEB_INTEGRATION.md` from the repository root before editing the Web service seam. `TECHNICAL_SPEC.md` and implemented public contracts take precedence over old standalone fixtures.

## Current increment

Implement only read-only browsing in this increment: samples, memories, local filtering, pagination, details, and original source inspection. Query submission, evidence decisions, and relation rendering are subsequent increments. Do not silently migrate unfinished query mocks into production.

## Integration rules

- Own only `src/dynamic_memory/web/`. Import MemoryRecord, SourceRef, SourceTurn and render_visible_turn from public contracts. Do not introduce alternative DTO definitions, canonical hashing, parsing, or inference.
- A supplies an initialized service through WebBindings; use the four read-only methods documented in the integration guide. The trusted environment variable DYNAMIC_MEMORY_WEB_FACTORY selects a module:function factory. Configuration failure must stay visible; never fall back silently to synthetic data.
- Keep bindings and selected records session-scoped. Clear selection on sample, filter, page, service, or selected-memory changes. Never cache mutable per-user state globally.
- Validate returned identifiers against the requested sample, memory, and source before rendering. event_time and recorded_at may be null. Unknown values must remain unknown.
- Keep fixtures in demo.py and label them synthetic. Use the public compute_memory_id helper. No LoCoMo Gold or raw file access from UI.
- Preserve source text, display caption and origin separately, and reuse the public visible-text renderer. Never download source images.
- Escape dynamic HTML; use native accessible widgets. Do not expose raw backend exception messages or credentials in UI or logs.

## Verification

Use the repository virtual environment to run `python -m pytest tests/web -q`, then the full contract suite. Exercise typed service injection, missing/invalid data, null time, source navigation, selection resets, and escaped content. Run `python scripts/run_web.py` and verify HTTP readiness when possible. Separate synthetic UI validation from real A/B integration, which requires an actual initialized service.
