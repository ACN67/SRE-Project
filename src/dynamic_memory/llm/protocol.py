"""Provider-independent LLM call contract; creating these objects has no I/O."""

from __future__ import annotations

import json
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value}")


class LLMModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class LLMMessage(LLMModel):
    role: Literal["system", "user"]
    content: str = Field(min_length=1)

    @model_validator(mode="after")
    def nonblank_content(self) -> LLMMessage:
        if not self.content.strip():
            raise ValueError("message content must not be blank")
        return self


class GenerationSettings(LLMModel):
    """Per-call limits supplied by the caller, typically from AppSettings."""

    max_input_tokens: PositiveInt
    max_output_tokens: PositiveInt
    temperature: float = Field(ge=0, le=2)
    thinking: Literal["disabled", "enabled"]


class LLMRequest(LLMModel):
    messages: tuple[LLMMessage, ...] = Field(min_length=1)
    task: Literal["memory_extraction", "state_reasoning", "qa"]
    output_mode: Literal["text", "json"]
    settings: GenerationSettings
    model_id: str | None = None

    @model_validator(mode="after")
    def task_contract(self) -> LLMRequest:
        if not any(message.role == "user" for message in self.messages):
            raise ValueError("messages must include a user message")
        if self.model_id is not None and not self.model_id.strip():
            raise ValueError("model_id must be nonblank or null")
        required_mode = "text" if self.task == "qa" else "json"
        if self.output_mode != required_mode:
            raise ValueError(f"{self.task} requires output_mode={required_mode}")
        return self


class TokenUsage(LLMModel):
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)


class LLMResponse(LLMModel):
    """Successful raw result; provider failures must raise, not become empty text."""

    output_mode: Literal["text", "json"]
    raw_output: str = Field(min_length=1)
    parsed_json: dict[str, Any] | None = None
    finish_reason: str | None = None
    usage: TokenUsage | None = None
    model_id: str | None = None

    @model_validator(mode="after")
    def valid_output(self) -> LLMResponse:
        if not self.raw_output.strip():
            raise ValueError("raw_output must not be blank")
        if self.finish_reason is not None and not self.finish_reason.strip():
            raise ValueError("finish_reason must be nonblank or null")
        if self.model_id is not None and not self.model_id.strip():
            raise ValueError("model_id must be nonblank or null")
        if self.output_mode == "json" and self.finish_reason == "length":
            raise ValueError("JSON output was truncated")
        if self.output_mode == "text":
            if self.parsed_json is not None:
                raise ValueError("text output must not supply parsed_json")
            return self
        try:
            parsed = json.loads(
                self.raw_output,
                parse_constant=_reject_json_constant,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError("JSON output is not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("JSON output must be an object")
        if self.parsed_json is not None and self.parsed_json != parsed:
            raise ValueError("parsed_json differs from raw_output")
        object.__setattr__(self, "parsed_json", parsed)
        return self


@runtime_checkable
class LLMAdapter(Protocol):
    """Synchronous provider contract; A-03/A-04 will supply implementations."""

    def generate(self, request: LLMRequest) -> LLMResponse: ...


def validate_response_for_request(request: LLMRequest, response: LLMResponse) -> None:
    """Check provider output against the call without interpreting business facts."""

    if response.output_mode != request.output_mode:
        raise ValueError("response output_mode differs from request")
    if request.model_id is not None and response.model_id is not None and response.model_id != request.model_id:
        raise ValueError("response model_id differs from request")
