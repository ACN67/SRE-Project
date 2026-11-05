"""Conservative provider-independent token estimate for context budgeting.

DeepSeek does not expose a local tokenizer through the SDK.  The previous
estimator used UTF-8 bytes as tokens and therefore overestimated mixed English
JSON prompts by roughly 3-4x.  This estimator keeps a safety margin while
tracking the approximate token behavior of English and CJK text.
"""

from __future__ import annotations

import math
import unicodedata

from .protocol import LLMMessage

ESTIMATOR_VERSION = "calibrated-unicode-framing-v2"


def estimate_text_tokens(text: str) -> int:
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    ascii_units = 0
    cjk_units = 0
    other_unicode = 0
    for char in text:
        if char.isascii():
            ascii_units += 1
        elif "\u3400" <= char <= "\u9fff" or "\uf900" <= char <= "\ufaff":
            cjk_units += 1
        else:
            category = unicodedata.category(char)
            other_unicode += 1 if category[0] in {"L", "N"} else 0
    # English/code is normally near four characters per token. Three is a
    # conservative approximation that leaves room for punctuation and JSON.
    return math.ceil(ascii_units / 3.0) + cjk_units + math.ceil(other_unicode / 2.0) + 16


def estimate_messages_tokens(messages: tuple[LLMMessage, ...]) -> int:
    if not isinstance(messages, tuple) or not messages:
        raise ValueError("messages must be a non-empty tuple")
    return sum(estimate_text_tokens(message.content) + 16 for message in messages)
