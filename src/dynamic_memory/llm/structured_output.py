"""Offline, provider-independent validation of one JSON LLM response."""

from __future__ import annotations

import json
import math
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from dynamic_memory.contracts.models import SCHEMA_VERSION, VersionedModel

from .protocol import LLMRequest, LLMResponse, validate_response_for_request

T = TypeVar("T", bound=BaseModel)


class StructuredOutputError(ValueError):
    """Sanitized failure with a stable code for the calling pipeline."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class _JSONIntegrityError(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _JSONIntegrityError("duplicate key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise _JSONIntegrityError("non-finite number")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise _JSONIntegrityError("non-finite number")
    return parsed


def validate_structured_output(request: LLMRequest, response: LLMResponse, schema: type[T]) -> T:
    """Validate an existing JSON completion against the caller's Pydantic schema.

    This checks structure only. Source existence, canonical Memory IDs and factual
    support need their own context-dependent checks in the business pipeline.
    """

    if not isinstance(schema, type) or not issubclass(schema, BaseModel):
        raise StructuredOutputError("INVALID_ARGUMENT", "schema must be a Pydantic model class") from None
    if not isinstance(request, LLMRequest) or not isinstance(response, LLMResponse):
        raise StructuredOutputError("INVALID_ARGUMENT", "expected LLMRequest and LLMResponse") from None
    try:
        checked_request = LLMRequest.model_validate(request.model_dump(mode="python"))
    except (ValidationError, ValueError, TypeError):
        raise StructuredOutputError("INVALID_ARGUMENT", "LLMRequest validation failed") from None
    if checked_request.output_mode != "json" or checked_request.task == "qa":
        raise StructuredOutputError("INVALID_ARGUMENT", "JSON schema validation requires a JSON task") from None
    if response.finish_reason is not None and response.finish_reason != "stop":
        raise StructuredOutputError("RESPONSE_INCOMPLETE", "completion did not finish normally") from None
    try:
        checked_response = LLMResponse.model_validate(response.model_dump(mode="python"))
    except (ValidationError, ValueError, TypeError):
        raise StructuredOutputError("RESPONSE_INCONSISTENT", "LLMResponse validation failed") from None
    try:
        validate_response_for_request(checked_request, checked_response)
    except ValueError:
        raise StructuredOutputError("RESPONSE_INCONSISTENT", "response differs from request") from None
    try:
        parsed = json.loads(
            checked_response.raw_output,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except (json.JSONDecodeError, _JSONIntegrityError):
        raise StructuredOutputError("JSON_INTEGRITY", "JSON content is invalid or ambiguous") from None
    if not isinstance(parsed, dict):
        raise StructuredOutputError("JSON_INTEGRITY", "JSON root must be an object") from None
    if not isinstance(response.parsed_json, dict) or response.parsed_json != parsed:
        raise StructuredOutputError("RESPONSE_INCONSISTENT", "parsed_json differs from raw_output") from None
    if issubclass(schema, VersionedModel) and parsed.get("schema_version") != SCHEMA_VERSION:
        raise StructuredOutputError("SCHEMA_INVALID", "explicit schema_version 2.1 is required") from None
    try:
        return schema.model_validate(parsed)
    except ValidationError as exc:
        raise StructuredOutputError("SCHEMA_INVALID", f"business schema validation failed ({exc.error_count()} errors)") from None
