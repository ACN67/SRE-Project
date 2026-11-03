"""Private, fail-closed per-window extraction checkpoints for authorized runs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict
from pathlib import Path

from pydantic import ValidationError

from dynamic_memory.llm import CallIdentity
from dynamic_memory.memory.extraction import AtomicFactDraft, WindowExtractionResult
from dynamic_memory.memory.windows import ExtractionWindow


class CheckpointError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(f"{code}: window checkpoint unavailable")


def _bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _hash(value: object) -> str:
    return "sha256:" + hashlib.sha256(_bytes(value)).hexdigest()


def call_identity_fingerprint(identity: CallIdentity) -> str:
    if not isinstance(identity, CallIdentity):
        raise CheckpointError("CHECKPOINT_IDENTITY_INVALID")
    return _hash(asdict(identity))


class WindowCheckpointStore:
    """Keeps validated drafts off Git; a caller must still review before publication.

    A missing checkpoint is not permission to call a paid model. The caller's
    separate attempt ledger and authorization gate must decide that.
    """

    def __init__(self, root: Path, *, run_fingerprint: str, max_new_attempts: int = 0):
        if (not isinstance(root, Path) or not isinstance(run_fingerprint, str)
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", run_fingerprint)
                or type(max_new_attempts) is not int or max_new_attempts < 0):
            raise CheckpointError("CHECKPOINT_IDENTITY_INVALID")
        self.root = root
        self.run_fingerprint = run_fingerprint
        self.max_new_attempts = max_new_attempts
        root.mkdir(parents=True, exist_ok=True)

    def _path(self, window: ExtractionWindow) -> Path:
        if not re.fullmatch(r"window_[0-9a-f]{24}", window.extraction_window_id):
            raise CheckpointError("CHECKPOINT_IDENTITY_INVALID")
        return self.root / f"{window.extraction_window_id}.json"

    def claim_new_attempt(self, window: ExtractionWindow, call_fingerprint: str) -> None:
        """Durably spend an attempt before sending; no implicit retry after crash."""
        path = self.root / "attempts.json"
        identity = self._identity(window, call_fingerprint)
        try:
            if path.exists():
                record = json.loads(path.read_bytes())
                attempts = record["attempts"]
                if record["digest"] != _hash(attempts) or not isinstance(attempts, list):
                    raise CheckpointError("CHECKPOINT_CORRUPT_OR_STALE")
            else:
                attempts = []
            if identity in attempts:
                raise CheckpointError("CHECKPOINT_ATTEMPT_SPENT")
            if len(attempts) >= self.max_new_attempts:
                raise CheckpointError("CHECKPOINT_BUDGET_EXHAUSTED")
            attempts.append(identity)
            fd, temporary = tempfile.mkstemp(prefix=".attempts-", dir=self.root)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(_bytes({"attempts": attempts, "digest": _hash(attempts)}))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        except CheckpointError:
            raise
        except (OSError, ValueError, TypeError, KeyError):
            raise CheckpointError("CHECKPOINT_CORRUPT_OR_STALE") from None

    def _identity(self, window: ExtractionWindow, call_fingerprint: str) -> str:
        if not isinstance(call_fingerprint, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", call_fingerprint):
            raise CheckpointError("CHECKPOINT_IDENTITY_INVALID")
        return _hash({"run": self.run_fingerprint, "sample": window.sample_id,
                      "window": window.extraction_window_id, "input": window.input_text,
                      "source_refs": [ref.model_dump(mode="json") for ref in window.source_refs],
                      "call": call_fingerprint})

    def load(self, window: ExtractionWindow, call_fingerprint: str) -> WindowExtractionResult | None:
        path = self._path(window)
        if not path.exists():
            return None
        try:
            raw = path.read_bytes()
            record = json.loads(raw)
            body = record["body"]
            if record["digest"] != _hash(body) or body["identity"] != self._identity(window, call_fingerprint):
                raise ValueError("digest or identity mismatch")
            if body["sample_id"] != window.sample_id or body["window_id"] != window.extraction_window_id:
                raise ValueError("window mismatch")
            facts = tuple(AtomicFactDraft.model_validate(item) for item in body["facts"])
            allowed = {(ref.session_id, ref.dia_id) for ref in window.source_refs}
            if any(not {(ref.session_id, ref.dia_id) for ref in fact.source_refs}.issubset(allowed) for fact in facts):
                raise ValueError("source mismatch")
            return WindowExtractionResult(window.sample_id, window.extraction_window_id, facts,
                body["template_fingerprint"], body["input_tokens_estimated"], "checkpoint")
        except (OSError, ValueError, TypeError, KeyError, ValidationError):
            raise CheckpointError("CHECKPOINT_CORRUPT_OR_STALE") from None

    def save(self, window: ExtractionWindow, result: WindowExtractionResult,
             call_fingerprint: str) -> None:
        if result.sample_id != window.sample_id or result.extraction_window_id != window.extraction_window_id:
            raise CheckpointError("CHECKPOINT_IDENTITY_INVALID")
        allowed = {(ref.session_id, ref.dia_id) for ref in window.source_refs}
        if any(not {(ref.session_id, ref.dia_id) for ref in fact.source_refs}.issubset(allowed) for fact in result.facts):
            raise CheckpointError("CHECKPOINT_IDENTITY_INVALID")
        body = {"identity": self._identity(window, call_fingerprint), "sample_id": result.sample_id,
                "window_id": result.extraction_window_id,
                "facts": [fact.model_dump(mode="json") for fact in result.facts],
                "template_fingerprint": result.template_fingerprint,
                "input_tokens_estimated": result.input_tokens_estimated}
        payload = _bytes({"body": body, "digest": _hash(body)})
        path = self._path(window)
        if path.exists():
            if self.load(window, call_fingerprint) != WindowExtractionResult(result.sample_id, result.extraction_window_id,
                result.facts, result.template_fingerprint, result.input_tokens_estimated, "checkpoint"):
                raise CheckpointError("CHECKPOINT_CONFLICT")
            return
        fd, temporary = tempfile.mkstemp(prefix=".checkpoint-", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if path.exists():
                raise CheckpointError("CHECKPOINT_CONFLICT")
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
