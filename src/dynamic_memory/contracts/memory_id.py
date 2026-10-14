"""Deterministic v2.1 Memory ID from normalized canonical payload."""

import hashlib
import json
import re
import unicodedata

from .models import MemoryRecord

FIELDS = ("sample_id", "content", "entity", "attribute", "value", "fact_kind", "assertion_type", "polarity", "recorded_at", "event_time", "source_refs")


def _normalize(value):
    if isinstance(value, str):
        return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value).strip())
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalize(item) for key, item in value.items()}
    return value


def canonical_memory_payload(memory: MemoryRecord) -> dict:
    raw = memory.model_dump(mode="json", exclude={"schema_version", "memory_id", "extractor_version"})
    payload = {key: raw[key] for key in FIELDS}
    payload["source_refs"] = sorted(payload["source_refs"], key=lambda r: (r["session_id"], r["dia_id"]))
    return _normalize(payload)


def compute_memory_id(memory: MemoryRecord) -> str:
    serialized = json.dumps(canonical_memory_payload(memory), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "mem_" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:20]


def validate_memory_id(memory: MemoryRecord) -> None:
    if memory.memory_id != compute_memory_id(memory):
        raise ValueError("memory_id does not match canonical SHA-256 payload")
