"""Read-only LoCoMo snapshot inspection and explicit split freezing.

Only sample identifiers and minimal conversation shape are inspected. Never
include source content or parser exceptions in diagnostics.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from dynamic_memory.pipeline.settings import DataSplitSettings


ORDERS = {"main_dev": (1, 2, 3, 4), "extra_dev": (5, 6, 7, 8), "test": (9, 10)}
_SHA = re.compile(r"[0-9a-f]{64}\Z")


class _ReadableDumper(yaml.SafeDumper):
    def increase_indent(self, flow: bool = False, indentless: bool = False):
        return super().increase_indent(flow, False)


class DataSnapshotError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class SnapshotInfo:
    path: Path
    sha256: str
    size_bytes: int
    sample_ids: tuple[str, ...]


def _valid_ids(ids: object, count: int) -> bool:
    return (
        isinstance(ids, (list, tuple))
        and len(ids) == count
        and all(isinstance(x, str) and bool(x.strip()) for x in ids)
        and len(set(ids)) == count
    )


def inspect_snapshot(path: str | Path) -> SnapshotInfo:
    """Hash original bytes and inspect only IDs and conversation shape."""
    source = Path(path)
    try:
        if not source.is_file():
            raise DataSnapshotError("DATA_FILE_NOT_FOUND", "snapshot is not a regular file")
        raw = source.read_bytes()
    except DataSnapshotError:
        raise
    except OSError:
        raise DataSnapshotError("DATA_FILE_NOT_FOUND", "snapshot cannot be read") from None
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise DataSnapshotError("DATA_FORMAT_INVALID", "snapshot is not UTF-8 JSON") from None
    if not isinstance(document, list) or len(document) != 10:
        raise DataSnapshotError("DATA_FORMAT_INVALID", "snapshot must contain ten samples")
    ids: list[str] = []
    for item in document:
        if not isinstance(item, dict):
            raise DataSnapshotError("DATA_FORMAT_INVALID", "sample must be an object")
        sample_id = item.get("sample_id")
        if not isinstance(sample_id, str) or not sample_id.strip():
            raise DataSnapshotError("DATA_FORMAT_INVALID", "sample_id must be a nonempty string")
        if not isinstance(item.get("conversation"), dict) or not item["conversation"]:
            raise DataSnapshotError("DATA_FORMAT_INVALID", "conversation must be a nonempty object")
        ids.append(sample_id)
    if len(set(ids)) != 10:
        raise DataSnapshotError("DUPLICATE_SAMPLE_ID", "sample identifiers must be unique")
    return SnapshotInfo(source, hashlib.sha256(raw).hexdigest(), len(raw), tuple(ids))


def _state(config: DataSplitSettings) -> str:
    values = [config.snapshot_sha256, config.sample_ids]
    values.extend(getattr(config.groups, name).sample_ids for name in ORDERS)
    if all(value is None for value in values):
        return "unfrozen"
    if any(value is None for value in values):
        return "partial"
    return "frozen"


def validate_split(snapshot: SnapshotInfo, split_config: DataSplitSettings) -> None:
    """Reject unfrozen or inconsistent configuration without positional fallback."""
    if _state(split_config) != "frozen":
        raise DataSnapshotError("SPLIT_UNFROZEN", "split fields are not fully frozen")
    if not _SHA.fullmatch(split_config.snapshot_sha256 or ""):
        raise DataSnapshotError("SPLIT_MISMATCH", "snapshot SHA has invalid format")
    if split_config.snapshot_sha256 != snapshot.sha256:
        raise DataSnapshotError("DATA_SNAPSHOT_MISMATCH", "snapshot SHA differs")
    if not _valid_ids(split_config.sample_ids, 10):
        raise DataSnapshotError("SPLIT_MISMATCH", "global sample IDs are invalid")
    if tuple(split_config.sample_ids or ()) != snapshot.sample_ids:
        raise DataSnapshotError("SPLIT_MISMATCH", "global sample ID order differs")
    seen: list[str] = []
    for name, order in ORDERS.items():
        group = getattr(split_config.groups, name)
        if tuple(group.source_order_1_based) != order:
            raise DataSnapshotError("SPLIT_MISMATCH", "group source order differs")
        if not _valid_ids(group.sample_ids, len(order)):
            raise DataSnapshotError("SPLIT_MISMATCH", "group sample IDs are invalid")
        expected = tuple(snapshot.sample_ids[index - 1] for index in order)
        if tuple(group.sample_ids or ()) != expected:
            raise DataSnapshotError("SPLIT_MISMATCH", "group sample IDs differ")
        seen.extend(group.sample_ids or ())
    if tuple(seen) != snapshot.sample_ids:
        raise DataSnapshotError("SPLIT_MISMATCH", "groups overlap or omit samples")


def _read_config(path: Path) -> tuple[dict, DataSplitSettings]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError
        config = DataSplitSettings.model_validate(document)
    except (OSError, UnicodeError, yaml.YAMLError, ValidationError, ValueError):
        raise DataSnapshotError("SPLIT_MISMATCH", "split configuration cannot be parsed") from None
    return document, config


def freeze_split_config(
    snapshot: SnapshotInfo, config_path: str | Path, *, apply: bool = False
) -> DataSplitSettings:
    """Preview a frozen split; write atomically only with explicit apply=True."""
    path = Path(config_path)
    document, current = _read_config(path)
    if not _SHA.fullmatch(snapshot.sha256) or not _valid_ids(snapshot.sample_ids, 10):
        raise DataSnapshotError("DATA_FORMAT_INVALID", "snapshot metadata is invalid")
    if _state(current) == "frozen":
        validate_split(snapshot, current)
        return current
    if _state(current) == "partial":
        raise DataSnapshotError("SPLIT_UNFROZEN", "partially frozen configuration needs review")
    for name, order in ORDERS.items():
        if tuple(getattr(current.groups, name).source_order_1_based) != order:
            raise DataSnapshotError("SPLIT_MISMATCH", "group source order differs")
    document["snapshot_sha256"] = snapshot.sha256
    document["sample_ids"] = list(snapshot.sample_ids)
    for name, order in ORDERS.items():
        document["groups"][name]["sample_ids"] = [snapshot.sample_ids[i - 1] for i in order]
    proposed = DataSplitSettings.model_validate(document)
    validate_split(snapshot, proposed)
    if apply:
        temp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", dir=path.parent,
                prefix=f".{path.name}.", suffix=".tmp", delete=False,
            ) as stream:
                temp_name = stream.name
                yaml.dump(document, stream, Dumper=_ReadableDumper, sort_keys=False, allow_unicode=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, path)
        except OSError:
            raise DataSnapshotError("SPLIT_MISMATCH", "split configuration could not be written") from None
        finally:
            if temp_name and os.path.exists(temp_name):
                os.unlink(temp_name)
    return proposed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect LoCoMo snapshot and fixed split")
    parser.add_argument("snapshot", type=Path, help="raw locomo10.json path")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[3] / "configs" / "data_split.yaml")
    parser.add_argument("--freeze", action="store_true", help="explicitly freeze an unfrozen split")
    args = parser.parse_args(argv)
    try:
        info = inspect_snapshot(args.snapshot)
        _, config = _read_config(args.config)
        if args.freeze:
            config = freeze_split_config(info, args.config, apply=True)
        else:
            if _state(config) == "unfrozen":
                print(f"unfrozen sha256={info.sha256} count={len(info.sample_ids)}")
                for name, order in ORDERS.items():
                    print(f"{name}: {', '.join(info.sample_ids[i - 1] for i in order)}")
                return 2
            validate_split(info, config)
        print(f"frozen sha256={info.sha256} count={len(info.sample_ids)}")
        for name in ORDERS:
            print(f"{name}: {', '.join(getattr(config.groups, name).sample_ids or [])}")
        return 0
    except DataSnapshotError as error:
        print(str(error))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
