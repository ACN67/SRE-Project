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
from .structured_output import StructuredOutputError, validate_structured_output
from .prompts import (
    DEFAULT_PROMPT_TEMPLATES, MEMORY_EXTRACTION_PROMPT, QA_PROMPT,
    STATE_REASONING_PROMPT, PromptTemplate, PromptTemplateError, RenderedPrompt,
    build_prompt_request, get_prompt_template, render_prompt,
)

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
    "StructuredOutputError",
    "validate_structured_output",
    "PromptTemplate",
    "PromptTemplateError",
    "RenderedPrompt",
    "render_prompt",
    "build_prompt_request",
    "get_prompt_template",
    "DEFAULT_PROMPT_TEMPLATES",
    "MEMORY_EXTRACTION_PROMPT",
    "STATE_REASONING_PROMPT",
    "QA_PROMPT",
]
