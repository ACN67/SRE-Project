"""IC-001 public conversation containers; no raw LoCoMo or Gold payloads."""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from .artifacts import SHA256_HEX
from .models import ContractModel, SourceTurn, VersionedModel


class ConversationSession(ContractModel):
    """Original turn order within one source session; empty is structurally valid."""

    model_config = ConfigDict(strict=True, hide_input_in_errors=True)

    session_id: str = Field(min_length=1)
    turns: list[SourceTurn]

    @field_validator("session_id")
    @classmethod
    def nonblank_session_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("session_id must not be blank")
        return value

    @model_validator(mode="after")
    def unique_turns(self):
        seen: set[str] = set()
        for turn in self.turns:
            if turn.session_id != self.session_id:
                raise ValueError("turn session_id differs from parent session")
            if not turn.sample_id.strip() or not turn.dia_id.strip():
                raise ValueError("turn source identity must not be blank")
            if turn.caption_origin is not None and turn.caption_origin != "blip_caption":
                raise ValueError("caption_origin must be blip_caption")
            if turn.dia_id in seen:
                raise ValueError("duplicate dia_id in session")
            seen.add(turn.dia_id)
        return self


class ConversationSample(VersionedModel):
    """One real sample ID and its sessions in source order, without annotations."""

    model_config = ConfigDict(strict=True, hide_input_in_errors=True)

    sample_id: str = Field(min_length=1)
    source_order_1_based: int = Field(ge=1)
    sessions: list[ConversationSession]

    @field_validator("sample_id")
    @classmethod
    def nonblank_sample_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("sample_id must not be blank")
        return value

    @model_validator(mode="after")
    def consistent_sessions(self):
        seen: set[str] = set()
        for session in self.sessions:
            if session.session_id in seen:
                raise ValueError("duplicate session_id in sample")
            seen.add(session.session_id)
            if any(turn.sample_id != self.sample_id for turn in session.turns):
                raise ValueError("turn sample_id differs from parent sample")
        return self


class DatasetBundle(VersionedModel):
    """Snapshot identity and complete per-sample split mapping, before disk checks."""

    model_config = ConfigDict(strict=True, hide_input_in_errors=True)

    snapshot_sha256: str
    samples: list[ConversationSample]
    split_by_sample_id: dict[str, Literal["main_dev", "extra_dev", "test"]]

    @field_validator("snapshot_sha256")
    @classmethod
    def sha256_digest(cls, value: str) -> str:
        if not SHA256_HEX.fullmatch(value):
            raise ValueError("snapshot_sha256 must be a lowercase SHA-256 hex digest")
        return value

    @model_validator(mode="after")
    def complete_split(self):
        ids = [sample.sample_id for sample in self.samples]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate sample_id in dataset")
        if set(self.split_by_sample_id) != set(ids):
            raise ValueError("split_by_sample_id must match dataset sample IDs")
        return self
