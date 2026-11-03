"""Verify the official frozen LoCoMo snapshot without exposing dialogue or Gold."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from dynamic_memory.contracts import SourceRef
from dynamic_memory.data.loader import load_conversations
from dynamic_memory.data.repository import SourceRepository
from dynamic_memory.data.snapshot import inspect_snapshot, validate_split
from dynamic_memory.pipeline.settings import load_settings


def verify(raw: Path, config_dir: Path) -> dict:
    info = inspect_snapshot(raw)
    split = load_settings(config_dir).data_split
    validate_split(info, split)
    first = load_conversations(str(raw), str(config_dir / "data_split.yaml"))
    second = load_conversations(str(raw), str(config_dir / "data_split.yaml"))
    if first != second:
        raise ValueError("REPEATED_LOAD_MISMATCH")
    repository = SourceRepository(first)
    if tuple(repository.list_samples()) != info.sample_ids:
        raise ValueError("SAMPLE_ORDER_MISMATCH")
    count = 0
    captions = 0
    for sample in first.samples:
        for session in sample.sessions:
            for turn in session.turns:
                ref = SourceRef(session_id=session.session_id, dia_id=turn.dia_id)
                if repository.get_turn(sample.sample_id, ref) != turn:
                    raise ValueError("SOURCE_REF_MISMATCH")
                fields = set(turn.model_dump())
                if fields & {"qa", "answer", "evidence", "summary"}:
                    raise ValueError("GOLD_FIELD_LEAK")
                count += 1
                captions += turn.image_caption is not None
    if set(first.model_dump()) & {"qa", "answer", "evidence", "summary"}:
        raise ValueError("GOLD_FIELD_LEAK")
    digest = hashlib.sha256(json.dumps(first.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
    return {"sha256": info.sha256, "bytes": info.size_bytes, "sample_ids": list(info.sample_ids),
            "groups": {name: list(getattr(split.groups, name).sample_ids) for name in
                       ("main_dev", "extra_dev", "test")},
            "turns": count, "image_caption_turns": captions, "source_ref_checked": count,
            "repeated_load_equal": True, "gold_fields_absent": True, "bundle_sha256": digest}


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    result = verify(root / "data" / "raw" / "locomo10.json", root / "configs")
    print(json.dumps(result, ensure_ascii=False))
