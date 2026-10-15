"""Shared provider-independent LLM call contract (no provider implementation)."""

from .protocol import (
    GenerationSettings,
    LLMAdapter,
    LLMMessage,
    LLMRequest,
    LLMResponse,
    TokenUsage,
    validate_response_for_request,
)
from .mock import MockLLMAdapter, MockPresetNotFound, request_fingerprint
from .deepseek import DeepSeekAdapterError, DeepSeekLLMAdapter

__all__ = [
    "GenerationSettings",
    "LLMAdapter",
    "LLMMessage",
    "LLMRequest",
    "LLMResponse",
    "TokenUsage",
    "validate_response_for_request",
    "MockLLMAdapter",
    "MockPresetNotFound",
    "request_fingerprint",
    "DeepSeekAdapterError",
    "DeepSeekLLMAdapter",
]
