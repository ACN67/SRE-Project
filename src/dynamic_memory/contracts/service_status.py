"""IC-012 read-only build diagnosis; additive to business schema 2.1."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DatasetState = Literal["unloaded", "ready", "load_error"]
MemoryState = Literal["not_built", "complete", "partial", "failed", "stale", "corrupt"]

_ERROR = {
    "not_built": "BUILD_NOT_FOUND",
    "partial": "ARTIFACT_INCOMPLETE",
    "failed": "MEMORY_BUILD_FAILED",
    "stale": "ARTIFACT_STALE",
    "corrupt": "ARTIFACT_CORRUPT",
}


class BuildStatus(BaseModel):
    """A single coherent diagnostic snapshot, never proof of disk integrity."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True,
                              hide_input_in_errors=True)

    schema_version: Literal["2.1"] = "2.1"
    dataset_state: DatasetState
    memory_state: MemoryState
    partial_build: bool
    build_id: str | None = None
    error_code: str | None = None

    @model_validator(mode="after")
    def consistent_state(self):
        if self.dataset_state != "ready":
            code = "DATA_NOT_LOADED" if self.dataset_state == "unloaded" else "DATA_LOAD_FAILED"
            if (self.memory_state != "not_built" or self.partial_build or
                    self.build_id is not None or self.error_code != code):
                raise ValueError("dataset diagnostic is inconsistent")
            return self
        if self.memory_state == "complete":
            if self.partial_build or not self.build_id or not self.build_id.strip() or self.error_code is not None:
                raise ValueError("complete build diagnostic is inconsistent")
        elif self.memory_state == "not_built":
            if self.partial_build or self.build_id is not None or self.error_code != _ERROR["not_built"]:
                raise ValueError("not-built diagnostic is inconsistent")
        elif (self.partial_build != (self.memory_state in ("partial", "failed")) or
              not self.build_id or not self.build_id.strip() or self.error_code != _ERROR[self.memory_state]):
            raise ValueError("noncomplete build diagnostic is inconsistent")
        return self
