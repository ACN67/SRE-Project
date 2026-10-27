"""Validated, isolated, read-only source lookup for IC-001 containers."""

from __future__ import annotations

from copy import deepcopy

from pydantic import ValidationError

from dynamic_memory.contracts import (
    ConversationSample, ConversationSession, DatasetBundle, SourceRef, SourceTurn,
)

from .loader import DataLoadError


class SourceRepositoryError(DataLoadError):
    """Stable, content-free source lookup failure with a ``code`` attribute."""


def _fail(code: str, message: str) -> None:
    raise SourceRepositoryError(code, message) from None


def _identity(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _fields_only(value: object) -> None:
    if set(value.__dict__) != set(type(value).model_fields) or value.__pydantic_extra__:
        _fail("DATA_FORMAT_INVALID", "container has unexpected fields")


class SourceRepository:
    """Index one validated bundle; returned DTOs are defensive deep copies.

    ``bundle=None`` represents the not-loaded state. Construction with a bad
    bundle fails before any index is published, so there is no partial state.
    """

    def __init__(self, bundle: DatasetBundle | None = None):
        self._snapshot_sha256: str | None = None
        self._samples: dict[str, ConversationSample] | None = None
        self._turns: dict[tuple[str, str, str], SourceTurn] | None = None
        self._owners: dict[tuple[str, str], set[str]] | None = None
        self._ordered_ids: tuple[str, ...] | None = None
        if bundle is None:
            return
        if not isinstance(bundle, DatasetBundle):
            _fail("DATA_FORMAT_INVALID", "dataset container has invalid type")
        try:
            snapshot = deepcopy(bundle)
            self._check_structure(snapshot)
            # model_copy(update=...) skips validation. Round-trip through plain
            # fields to force public DTO validators on every nested object.
            document = snapshot.model_dump(mode="python", warnings="error")
            validated = DatasetBundle.model_validate(document)
        except SourceRepositoryError:
            raise
        except (ValidationError, ValueError, TypeError, AttributeError, KeyError, RecursionError):
            _fail("DATA_FORMAT_INVALID", "dataset container cannot be validated")
        samples: dict[str, ConversationSample] = {}
        turns: dict[tuple[str, str, str], SourceTurn] = {}
        owners: dict[tuple[str, str], set[str]] = {}
        ordered_ids: list[str] = []
        for sample in validated.samples:
            samples[sample.sample_id] = sample
            ordered_ids.append(sample.sample_id)
            for session in sample.sessions:
                for turn in session.turns:
                    key = (sample.sample_id, session.session_id, turn.dia_id)
                    turns[key] = turn
                    owners.setdefault((session.session_id, turn.dia_id), set()).add(sample.sample_id)
        self._samples = samples
        self._turns = turns
        self._owners = owners
        self._ordered_ids = tuple(ordered_ids)
        self._snapshot_sha256 = validated.snapshot_sha256

    @property
    def snapshot_sha256(self) -> str:
        """Read-only snapshot identity from the validated dataset bundle."""
        self._ready()
        assert self._snapshot_sha256 is not None
        return self._snapshot_sha256

    @staticmethod
    def _check_structure(bundle: DatasetBundle) -> None:
        _fields_only(bundle)
        if not isinstance(bundle.samples, list):
            _fail("DATA_FORMAT_INVALID", "dataset samples must be a list")
        sample_ids: set[str] = set()
        source_orders: set[int] = set()
        for sample in bundle.samples:
            if not isinstance(sample, ConversationSample) or not _identity(sample.sample_id):
                _fail("DATA_FORMAT_INVALID", "sample identity is invalid")
            _fields_only(sample)
            if sample.sample_id in sample_ids:
                _fail("DATA_FORMAT_INVALID", "duplicate sample identity")
            sample_ids.add(sample.sample_id)
            source_order = sample.source_order_1_based
            if type(source_order) is not int or source_order < 1 or source_order in source_orders:
                _fail("DATA_FORMAT_INVALID", "sample source order is invalid or duplicate")
            source_orders.add(source_order)
            if not isinstance(sample.sessions, list):
                _fail("DATA_FORMAT_INVALID", "sample sessions must be a list")
            session_ids: set[str] = set()
            for session in sample.sessions:
                if not isinstance(session, ConversationSession) or not _identity(session.session_id):
                    _fail("DATA_FORMAT_INVALID", "session identity is invalid")
                _fields_only(session)
                if session.session_id in session_ids:
                    _fail("DATA_FORMAT_INVALID", "duplicate session identity")
                session_ids.add(session.session_id)
                if not isinstance(session.turns, list):
                    _fail("DATA_FORMAT_INVALID", "session turns must be a list")
                turn_ids: set[str] = set()
                for turn in session.turns:
                    if not isinstance(turn, SourceTurn):
                        _fail("DATA_FORMAT_INVALID", "turn has invalid type")
                    _fields_only(turn)
                    if not _identity(turn.sample_id) or not _identity(turn.session_id) or not _identity(turn.dia_id):
                        _fail("DATA_FORMAT_INVALID", "turn identity is invalid")
                    if turn.sample_id != sample.sample_id:
                        _fail("SAMPLE_MISMATCH", "turn belongs to a different sample")
                    if turn.session_id != session.session_id:
                        _fail("DATA_FORMAT_INVALID", "turn belongs to a different session")
                    if turn.dia_id in turn_ids:
                        _fail("DUPLICATE_SOURCE_REF", "duplicate source identity")
                    turn_ids.add(turn.dia_id)

    def _ready(self) -> None:
        if self._samples is None:
            _fail("DATA_NOT_LOADED", "source dataset is not loaded")

    def list_samples(self) -> list[str]:
        self._ready()
        return list(self._ordered_ids)

    def get_sample(self, sample_id: str) -> ConversationSample:
        self._ready()
        if not _identity(sample_id):
            _fail("DATA_FORMAT_INVALID", "sample ID is invalid")
        sample = self._samples.get(sample_id)
        if sample is None:
            _fail("SAMPLE_NOT_FOUND", "sample does not exist")
        return deepcopy(sample)

    def get_turn(self, sample_id: str, ref: SourceRef) -> SourceTurn:
        self._ready()
        if not _identity(sample_id):
            _fail("DATA_FORMAT_INVALID", "sample ID is invalid")
        if sample_id not in self._samples:
            _fail("SAMPLE_NOT_FOUND", "sample does not exist")
        if not isinstance(ref, SourceRef) or not _identity(ref.session_id) or not _identity(ref.dia_id):
            _fail("DATA_FORMAT_INVALID", "source reference is invalid")
        _fields_only(ref)
        try:
            SourceRef.model_validate(ref.model_dump(mode="python", warnings="error"))
        except (ValidationError, ValueError, TypeError, AttributeError):
            _fail("DATA_FORMAT_INVALID", "source reference is invalid")
        key = (sample_id, ref.session_id, ref.dia_id)
        turn = self._turns.get(key)
        if turn is not None:
            return deepcopy(turn)
        if (ref.session_id, ref.dia_id) in self._owners:
            _fail("SAMPLE_MISMATCH", "source belongs to another sample")
        _fail("SOURCE_NOT_FOUND", "source does not exist")
