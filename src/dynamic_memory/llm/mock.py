"""Deterministic offline LLM adapter for contract integration only."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping

from .protocol import LLMRequest, LLMResponse, validate_response_for_request

_FINGERPRINT_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


class MockPresetNotFound(LookupError):
    """Strict Mock has no fixture for this exact request."""


def request_fingerprint(request: LLMRequest) -> str:
    """Stable key over the complete validated request, including messages/settings."""

    if not isinstance(request, LLMRequest):
        raise TypeError("request must be an LLMRequest")
    validated = LLMRequest.model_validate(request.model_dump(mode="python"))
    canonical = json.dumps(
        validated.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class MockLLMAdapter:
    """Return generic synthetic outputs or exact in-memory preset responses.

    Presets are indexed by request_fingerprint(request). They are validated and
    serialized at construction, so later caller mutation cannot change a result.
    strict=True rejects missing presets; otherwise a generic synthetic result is
    returned. No file, key, clock, random source, model or network is accessed.
    """

    def __init__(
        self,
        presets: Mapping[str, LLMResponse] | None = None,
        *,
        strict: bool = False,
    ) -> None:
        if not isinstance(strict, bool):
            raise TypeError("strict must be a bool")
        if presets is not None and not isinstance(presets, Mapping):
            raise TypeError("presets must be a mapping from fingerprint to LLMResponse")
        saved: dict[str, str] = {}
        for key, response in (presets or {}).items():
            if not isinstance(key, str) or not _FINGERPRINT_PATTERN.fullmatch(key):
                raise ValueError("preset key must be a sha256 request fingerprint")
            if not isinstance(response, LLMResponse):
                raise TypeError("preset response must be an LLMResponse")
            checked = LLMResponse.model_validate(response.model_dump(mode="python", exclude={"parsed_json"}))
            saved[key] = checked.model_dump_json(exclude={"parsed_json"})
        self._presets = saved
        self._strict = strict

    def generate(self, request: LLMRequest) -> LLMResponse:
        fingerprint = request_fingerprint(request)
        saved = self._presets.get(fingerprint)
        if saved is not None:
            response = LLMResponse.model_validate_json(saved)
        elif self._strict:
            raise MockPresetNotFound(f"no Mock preset for {fingerprint}")
        elif request.output_mode == "json":
            raw = json.dumps(
                {"mock": True, "synthetic": True, "task": request.task, "request_fingerprint": fingerprint},
                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            )
            response = LLMResponse(output_mode="json", raw_output=raw)
        else:
            response = LLMResponse(
                output_mode="text",
                raw_output=f"Synthetic Mock response for {fingerprint}; no factual answer was generated.",
            )
        validate_response_for_request(request, response)
        return response
