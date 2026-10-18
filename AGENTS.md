# E module workspace

- For Web changes read `ai-skills/dynamic-memory-web/SKILL.md` and `docs/WEB_INTEGRATION.md`.
- Technical authority: `TECHNICAL_SPEC.md` schema 2.1 and `src/dynamic_memory/contracts/`.
- Keep production UI under `src/dynamic_memory/web/`; do not implement A/B/C/D business logic or duplicate public DTOs.
- Synthetic fixtures must remain visibly labeled. Do not read Gold, extract memories, build indexes, or call models from browsing actions.
- Run `.venv/Scripts/python.exe -m pytest tests/web -q` and the contract suite for behavioral changes.
- Follow the README review workflow; do not commit or push without the required explicit user confirmation. Never commit credentials, virtual environments, or generated caches.
