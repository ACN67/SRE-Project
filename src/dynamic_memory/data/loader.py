"""B-02: normalize validated LoCoMo conversations into public IC-001 DTOs."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

import yaml
from pydantic import ValidationError

from dynamic_memory.contracts import ConversationSample, ConversationSession, DatasetBundle, SourceTurn
from dynamic_memory.pipeline.settings import DataSplitSettings

from .snapshot import DataSnapshotError, inspect_snapshot, validate_split


class DataLoadError(DataSnapshotError):
    """Stable, content-free loader failure with a ``code`` attribute."""


_SESSION = re.compile(r"session_[1-9][0-9]*\Z")
_ENGLISH_TIME = re.compile(r"[0-9]{1,2}:[0-9]{2} [aApP][mM] on [0-9]{1,2} [A-Za-z]+, [0-9]{4}\Z")
_ISO_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})?\Z")


def _fail(code: str, message: str) -> None:
    raise DataLoadError(code, message) from None


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            _fail("DATA_FORMAT_INVALID", "duplicate JSON key")
        result[key] = value
    return result


def _parse_json(raw: bytes) -> object:
    try:
        return json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique_pairs,
            parse_constant=lambda _: _fail("DATA_FORMAT_INVALID", "invalid JSON constant"),
        )
    except DataLoadError:
        raise
    except (UnicodeError, ValueError, RecursionError):
        _fail("DATA_FORMAT_INVALID", "invalid UTF-8 JSON")


def _read_split(path: str | Path) -> DataSplitSettings:
    try:
        document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        _fail("SPLIT_MISMATCH", "split configuration cannot be read")
    if not isinstance(document, dict):
        _fail("SPLIT_MISMATCH", "split configuration must be an object")
    try:
        return DataSplitSettings.model_validate(document)
    except ValidationError:
        _fail("SPLIT_MISMATCH", "split configuration is invalid")


def _date(value: object) -> datetime | None:
    if value is None or (isinstance(value, str) and value.strip().lower() in {"", "unknown"}):
        return None
    if not isinstance(value, str):
        _fail("DATA_FORMAT_INVALID", "session date has invalid type")
    try:
        if _ENGLISH_TIME.fullmatch(value):
            return datetime.strptime(value, "%I:%M %p on %d %B, %Y")
        if _ISO_TIME.fullmatch(value):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        pass
    _fail("DATA_FORMAT_INVALID", "session date is invalid")


def _required_string(turn: dict, key: str, *, allow_empty: bool = False) -> str:
    value = turn.get(key)
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        _fail("DATA_FORMAT_INVALID", "turn has a missing or invalid required field")
    return value


def _parse_sample(item: dict, source_order_1_based: int) -> ConversationSample:
    """Pure, small synthetic-fixture parsing helper; disk validation stays in load_conversations."""
    if not isinstance(item, dict):
        _fail("DATA_FORMAT_INVALID", "sample must be an object")
    sample_id = item.get("sample_id")
    conversation = item.get("conversation")
    if not isinstance(sample_id, str) or not sample_id.strip():
        _fail("DATA_FORMAT_INVALID", "sample ID is invalid")
    if not isinstance(conversation, dict):
        _fail("DATA_FORMAT_INVALID", "conversation must be an object")
    sessions: list[ConversationSession] = []
    for key, turns_raw in conversation.items():
        if key in {"speaker_a", "speaker_b"} or (key.endswith("_date_time") and _SESSION.fullmatch(key[:-10])):
            continue
        if not _SESSION.fullmatch(key):
            _fail("DATA_FORMAT_INVALID", "conversation has an invalid session field")
        if not isinstance(turns_raw, list):
            _fail("DATA_FORMAT_INVALID", "session turns must be a list")
        recorded_at = _date(conversation.get(f"{key}_date_time"))
        turns: list[SourceTurn] = []
        seen: set[str] = set()
        for raw_turn in turns_raw:
            if not isinstance(raw_turn, dict):
                _fail("DATA_FORMAT_INVALID", "turn must be an object")
            speaker = _required_string(raw_turn, "speaker")
            dia_id = _required_string(raw_turn, "dia_id")
            text = _required_string(raw_turn, "text", allow_empty=True)
            if dia_id in seen:
                _fail("DUPLICATE_SOURCE_REF", "duplicate turn identity")
            seen.add(dia_id)
            caption = raw_turn.get("blip_caption")
            if caption is not None and not isinstance(caption, str):
                _fail("DATA_FORMAT_INVALID", "image caption has invalid type")
            caption = caption if caption and caption.strip() else None
            try:
                turns.append(SourceTurn(
                    sample_id=sample_id, session_id=key, dia_id=dia_id,
                    speaker=speaker, text=text, recorded_at=recorded_at,
                    image_caption=caption,
                    caption_origin="blip_caption" if caption is not None else None,
                ))
            except ValidationError:
                _fail("DATA_FORMAT_INVALID", "turn cannot be validated")
        try:
            sessions.append(ConversationSession(session_id=key, turns=turns))
        except ValidationError:
            _fail("DATA_FORMAT_INVALID", "session cannot be validated")
    try:
        return ConversationSample(sample_id=sample_id, source_order_1_based=source_order_1_based, sessions=sessions)
    except ValidationError:
        _fail("DATA_FORMAT_INVALID", "sample cannot be validated")


def load_conversations(path: str, split_path: str) -> DatasetBundle:
    """Load ten original-order samples only after B-01 snapshot and split checks."""
    try:
        info = inspect_snapshot(path)
    except RecursionError:
        _fail("DATA_FORMAT_INVALID", "snapshot JSON nesting is invalid")
    split = _read_split(split_path)
    validate_split(info, split)
    try:
        raw = Path(path).read_bytes()
    except OSError:
        _fail("DATA_FILE_NOT_FOUND", "snapshot cannot be read")
    if hashlib.sha256(raw).hexdigest() != info.sha256:
        _fail("DATA_SNAPSHOT_MISMATCH", "snapshot changed during loading")
    document = _parse_json(raw)
    if not isinstance(document, list) or len(document) != 10:
        _fail("DATA_FORMAT_INVALID", "snapshot must contain ten samples")
    samples = [_parse_sample(item, i) for i, item in enumerate(document, 1)]
    if tuple(sample.sample_id for sample in samples) != info.sample_ids:
        _fail("DATA_SNAPSHOT_MISMATCH", "sample order changed during loading")
    mapping = {
        sample_id: name
        for name in ("main_dev", "extra_dev", "test")
        for sample_id in getattr(split.groups, name).sample_ids
    }
    try:
        return DatasetBundle(snapshot_sha256=info.sha256, samples=samples, split_by_sample_id=mapping)
    except ValidationError:
        _fail("SPLIT_MISMATCH", "dataset container cannot be validated")
