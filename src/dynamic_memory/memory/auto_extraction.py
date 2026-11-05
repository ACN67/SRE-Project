"""Deterministic, source-grounded fact extraction for reviewed Memory builds.

The extractor keeps source wording instead of paraphrasing it. This makes every
draft independently checkable against one cited turn and avoids provider calls
in the offline memory-build path.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from dynamic_memory.llm import LLMRequest, LLMResponse, TokenUsage
from dynamic_memory.memory.extraction import (
    AtomicFactDraft,
    MEMORY_DRAFT_PROMPT,
    WindowExtractionResult,
)
from dynamic_memory.memory.windows import ExtractionWindow
from dynamic_memory.pipeline.runtime import LLMRuntime, create_runtime

AUTO_EXTRACTOR_VERSION = "auto-utterance-v10-state-attributes"
AUTO_EXTRACTION_PROFILE = "auto-utterance-v10-state-attributes"

_CAPTION = re.compile(r"\s*\[image_caption:\s*(.*?)\]\s*$", re.I | re.S)
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])|\n+")
_NAME = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b")
_FILLER = re.compile(
    r"^(?:yes|yeah|yep|no|nope|okay|ok|sure|right|exactly|absolutely|"
    r"thanks|thank you|lol|haha|hmm|wow|nice|cool|great|sounds good)"
    r"(?:\s+(?:yes|yeah|okay|ok|sure|great|nice|cool))?[.!]*$",
    re.I,
)
_GREETING = re.compile(
    r"^(?:hey|hi|hello|thanks|thank you|good (?:morning|afternoon|evening|night)|"
    r"nice (?:to meet|meeting) you|see you|wow|yep|yeah|okay|ok)"
    r"(?:\s*,?\s*(?:[A-Z][a-z]+|\w+)){0,3}[.!]*$",
    re.I,
)
_SOCIAL = re.compile(
    r"^(?:thanks|thank you)\s+for\s+(?:askin['’]?|checking|your message|"
    r"thinking of me|being there|the update)\b[^.!?]{0,40}[.!]*$",
    re.I,
)
_PLAN = re.compile(
    r"\b(?:will|would|going to|gonna|plan(?:ned|ning)?|intend(?:ed|ing)?|"
    r"hope|hoping|want|wants|wanted|aim|aiming|need to|has to|have to)\b",
    re.I,
)
_HYPOTHETICAL = re.compile(
    r"\b(?:if|would have|could have|might have|suppose|imagine|hypothetical)\b",
    re.I,
)
_UNCERTAIN = re.compile(
    r"\b(?:maybe|perhaps|probably|possibly|not sure|unsure|might|could)\b",
    re.I,
)
_ONGOING_STATE = re.compile(
    r"\b(?:(?:i(?:'m|\s+am)|we(?:'re|\s+are))\s+"
    r"(?:currently\s+|still\s+)?(?:reading|working|living|playing|learning|"
    r"searching|building|making|running|studying|writing|creating))",
    re.I,
)
_NEGATION = re.compile(
    r"\b(?:not|never|no|none|nobody|nothing|cannot|can't|didn't|doesn't|"
    r"isn't|wasn't|weren't|won't|wouldn't|no longer)\b",
    re.I,
)
_STATE = re.compile(
    r"\b(?:am|is|are|was|were|be|been|being|feel|feels|felt|like|likes|liked|"
    r"love|loves|loved|prefer|prefers|preferred|enjoy|enjoys|enjoyed|"
    r"have|has|had|own|owns|owned|live|lives|lived|work|works|worked|"
    r"believe|believes|believed|want|wants|wanted|plan|plans|planned)\b",
    re.I,
)
_MONTHS = {
    name.casefold(): index
    for index, name in enumerate(
        (
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ),
        1,
    )
}
_MONTH_RE = "|".join(_MONTHS)
_WEEKDAYS = {
    name.casefold(): index
    for index, name in enumerate(
        (
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday",
            "Sunday",
        )
    )
}
_WEEKDAY_RE = (
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun"
)
_EXTRA_CAPITAL_WORDS = {
    "i",
    "we",
    "he",
    "she",
    "they",
    "it",
    "the",
    "a",
    "an",
    "my",
    "our",
    "his",
    "her",
    "their",
    "this",
    "that",
    "these",
    "those",
    "and",
    "but",
    "so",
    "well",
    "then",
    "actually",
    "maybe",
    "yes",
    "yeah",
    "no",
    "oh",
    "wow",
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
    *_MONTHS,
}


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sentence_parts(visible_text: str) -> list[tuple[str, bool]]:
    text = visible_text.strip()
    caption = None
    match = _CAPTION.search(text)
    if match:
        caption = match.group(1).strip()
        text = text[: match.start()].strip()
    result: list[tuple[str, bool]] = []
    if text:
        result.extend(
            (part.strip(), False)
            for part in _SENTENCE.split(text)
            if part.strip()
        )
    if caption:
        result.append((caption, True))
    return result


def _clean_sentence(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = text.removeprefix("- ").strip()
    return text


def _is_claim(text: str) -> bool:
    compact = _clean_sentence(text)
    if (
        not compact
        or "?" in compact
        or _FILLER.fullmatch(compact)
        or _SOCIAL.fullmatch(compact)
        or (
            _GREETING.fullmatch(compact)
            and not re.search(r"\b(?:I|we|my|our|me)\b", compact)
        )
    ):
        return False
    words = re.findall(r"[A-Za-z0-9']+", compact)
    return len(words) >= 2


def _timezone(recorded_at: str | None):
    if not recorded_at:
        return None
    try:
        value = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
        # LoCoMo stores session-local wall-clock timestamps without a numeric
        # offset. Relative-date arithmetic is valid against that local anchor;
        # the source text remains the timezone authority for the fact.
        return value
    except (TypeError, ValueError):
        return None


def _day_span(moment: datetime) -> dict:
    start = datetime.combine(moment.date(), time.min, tzinfo=moment.tzinfo)
    return {
        "raw": None,
        "start": start.isoformat(),
        "end_exclusive": (start + timedelta(days=1)).isoformat(),
        "precision": "day",
        "resolution": "explicit",
    }


def _parse_event_time(text: str, recorded_at: str | None) -> dict | None:
    anchor = _timezone(recorded_at)
    if anchor is None:
        return None
    lower = text.casefold()
    explicit_day = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", text)
    if explicit_day:
        try:
            day = datetime(
                int(explicit_day.group(1)),
                int(explicit_day.group(2)),
                int(explicit_day.group(3)),
                tzinfo=anchor.tzinfo,
            )
        except ValueError:
            return None
        value = _day_span(day)
        value.update(raw=explicit_day.group(0), resolution="explicit")
        return value

    month_day = re.search(
        rf"\b({_MONTH_RE})\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(20\d{{2}})\b",
        text,
        re.I,
    )
    if month_day:
        try:
            day = datetime(
                int(month_day.group(3)),
                _MONTHS[month_day.group(1).casefold()],
                int(month_day.group(2)),
                tzinfo=anchor.tzinfo,
            )
        except ValueError:
            return None
        value = _day_span(day)
        value.update(raw=month_day.group(0), resolution="explicit")
        return value

    month_year = re.search(
        rf"\b({_MONTH_RE})\s+(20\d{{2}})\b",
        text,
        re.I,
    )
    if month_year:
        month = _MONTHS[month_year.group(1).casefold()]
        year = int(month_year.group(2))
        start = datetime(year, month, 1, tzinfo=anchor.tzinfo)
        end = (
            datetime(year + 1, 1, 1, tzinfo=anchor.tzinfo)
            if month == 12
            else datetime(year, month + 1, 1, tzinfo=anchor.tzinfo)
        )
        return {
            "raw": month_year.group(0),
            "start": start.isoformat(),
            "end_exclusive": end.isoformat(),
            "precision": "month",
            "resolution": "explicit",
        }

    year_only = re.search(r"\b(20\d{2})\b", text)
    if year_only:
        year = int(year_only.group(1))
        return {
            "raw": year_only.group(0),
            "start": datetime(year, 1, 1, tzinfo=anchor.tzinfo).isoformat(),
            "end_exclusive": datetime(year + 1, 1, 1, tzinfo=anchor.tzinfo).isoformat(),
            "precision": "year",
            "resolution": "explicit",
        }

    month_day_without_year = re.search(
        rf"\b({_MONTH_RE})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b",
        text,
        re.I,
    )
    if month_day_without_year:
        try:
            day = datetime(
                anchor.year,
                _MONTHS[month_day_without_year.group(1).casefold()],
                int(month_day_without_year.group(2)),
                tzinfo=anchor.tzinfo,
            )
        except ValueError:
            return None
        if day.date() > anchor.date():
            day = day.replace(year=day.year - 1)
        value = _day_span(day)
        value.update(
            raw=month_day_without_year.group(0),
            resolution="relative_year",
        )
        return value

    today = datetime.combine(anchor.date(), time.min, tzinfo=anchor.tzinfo)
    simple_days = {
        "today": 0,
        "yesterday": -1,
        "tomorrow": 1,
    }
    for phrase, delta in simple_days.items():
        if re.search(rf"\b{phrase}\b", lower):
            start = today + timedelta(days=delta)
            return {
                "raw": phrase,
                "start": start.isoformat(),
                "end_exclusive": (start + timedelta(days=1)).isoformat(),
                "precision": "day",
                "resolution": "relative",
            }

    weekday_match = re.search(
        rf"\blast\s+({_WEEKDAY_RE})\b",
        lower,
    )
    if weekday_match:
        token = weekday_match.group(1)
        aliases = {
            "mon": "monday",
            "tue": "tuesday",
            "tues": "tuesday",
            "wed": "wednesday",
            "thu": "thursday",
            "thur": "thursday",
            "thurs": "thursday",
            "fri": "friday",
            "sat": "saturday",
            "sun": "sunday",
        }
        weekday = _WEEKDAYS[aliases.get(token, token)]
        delta = (today.weekday() - weekday) % 7
        delta = 7 if delta == 0 else delta
        start = today - timedelta(days=delta)
        return {
            "raw": weekday_match.group(0),
            "start": start.isoformat(),
            "end_exclusive": (start + timedelta(days=1)).isoformat(),
            "precision": "day",
            "resolution": "relative",
        }

    if re.search(r"\blast week\b", lower):
        start = today - timedelta(days=7)
        return {
            "raw": "last week",
            "start": start.isoformat(),
            "end_exclusive": today.isoformat(),
            "precision": "day",
            "resolution": "relative",
        }
    if re.search(r"\bthis week\b", lower):
        start = today - timedelta(days=today.weekday())
        return {
            "raw": "this week",
            "start": start.isoformat(),
            "end_exclusive": (start + timedelta(days=7)).isoformat(),
            "precision": "day",
            "resolution": "relative",
        }
    month_match = re.search(r"\b(last|this|next) month\b", lower)
    if month_match:
        offset = {"last": -1, "this": 0, "next": 1}[month_match.group(1)]
        month_index = anchor.year * 12 + anchor.month - 1 + offset
        year, month_zero = divmod(month_index, 12)
        start = datetime(year, month_zero + 1, 1, tzinfo=anchor.tzinfo)
        end = (
            datetime(year + 1, 1, 1, tzinfo=anchor.tzinfo)
            if month_zero == 11
            else datetime(year, month_zero + 2, 1, tzinfo=anchor.tzinfo)
        )
        return {
            "raw": month_match.group(0),
            "start": start.isoformat(),
            "end_exclusive": end.isoformat(),
            "precision": "month",
            "resolution": "relative",
        }
    year_match = re.search(r"\b(last|this|next) year\b", lower)
    if year_match:
        offset = {"last": -1, "this": 0, "next": 1}[year_match.group(1)]
        year = anchor.year + offset
        return {
            "raw": year_match.group(0),
            "start": datetime(year, 1, 1, tzinfo=anchor.tzinfo).isoformat(),
            "end_exclusive": datetime(year + 1, 1, 1, tzinfo=anchor.tzinfo).isoformat(),
            "precision": "year",
            "resolution": "relative",
        }
    return None


def _entity(
    text: str,
    speaker: str,
    known_speakers: tuple[str, ...] = (),
) -> str | None:
    if re.search(r"\b(?:i|me|my|mine|myself)\b", text, re.I):
        return speaker
    # Sentence-initial capitalization is not proof of a named entity. Prefer
    # conversation participants, then a genuinely multi-word proper name.
    # Returning null is safer than creating a false subject for history
    # expansion from ordinary openers such as "Started" or "Been".
    mentioned = [
        (match.start(), name)
        for name in known_speakers
        for match in [re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text)]
        if match is not None
    ]
    if mentioned:
        return min(mentioned)[1]
    names = [
        name
        for name in _NAME.findall(text)
        if name.casefold() not in _EXTRA_CAPITAL_WORDS
        and " " in name
    ]
    return names[0] if names else None


def _attribute_and_value(text: str) -> tuple[str | None, str | None]:
    patterns = (
        (r"\b(?:moved|move|living|live|lives|lived)\s+(?:to|in)\s+(.+)$", "residence"),
        (r"\b(?:works?|worked|working)\s+(?:at|for|as|in)\s+(.+)$", "employment"),
        (
            r"\b(?:temporary\s+|part[ -]time\s+)?"
            r"(?:position|job)\s+(?:at|in|with)\s+(.+)$",
            "employment",
        ),
        (
            r"\b(?:lost|left|quit|resigned\s+from)\s+(?:my\s+)?"
            r"(?:job|position|role)\b(.*)$",
            "employment",
        ),
        (r"\b(?:make|made|making)\s+(?:a\s+)?living\s+(?:at|from|by)\s+(.+)$", "employment"),
        (r"\b(?:working|work|worked)\s+on\s+(.+)$", "current_work"),
        (r"\b(?:looking|looked|searching|searched)\s+for\s+(.+)$", "search_target"),
        (r"\b(?:following|followed|pursuing|pursued)\s+(.+)$", "goal"),
        (r"\b(?:starting|startin'|started|start)\s+(.+)$", "venture_status"),
        (r"\b(?:reading|read)\s+(.+)$", "currently_reading"),
        (r"\b(?:playing|play|recommend(?:ing|s|ed)?)\s+(.+)$", "current_game"),
        (r"\b(?:learning|learned|studying|studied)\s+(.+)$", "skill_learning"),
        (r"\b(?:writing|wrote|creating|created|making|made)\s+(.+)$", "creative_project"),
        (r"\b(?:volunteer(?:ed|ing)?|working)\s+(?:at|for)\s+(.+)$", "volunteering"),
        (r"\b(?:loves?|loved|likes?|liked|prefers?|preferred|enjoys?|enjoyed)\s+(.+)$", "preference"),
        (r"\b(?:owns?|owned|bought|purchased)\s+(.+)$", "possession"),
        (r"\b(?:plan(?:s|ned|ning)?|intends?|intended|hopes?|hoped|wants?|wanted)\s+(?:to\s+)?(.+)$", "plan"),
        (r"\b(?:i(?:'m|\s+am)|we(?:'re|\s+are))\s+going\s+to\s+(.+)$", "plan"),
        (r"\b(?:i|we)\s+will\s+(.+)$", "plan"),
        (r"\b(?:getting|get|preparing)\s+ready\s+for\s+(.+)$", "event_preparation"),
        (r"\b(?:won|win|winning|participating\s+in)\s+(.+tournament.+)$", "achievement"),
        (r"\b(?:visited|visiting|travel(?:ed|ing)?\s+to|went\s+to)\s+(.+)$", "activity"),
        (r"\b(?:determined|committed)\s+to\s+(.+)$", "commitment"),
        (r"\b(?:got|became|is|are)\s+(?:engaged|married|divorced)\b\s*(.*)$", "relationship_status"),
        (r"\b(?:is|are)\s+(?:an?\s+|the\s+)?my\s+(.+)$", "relationship"),
        (r"\b(?:is|are|was|were|am)\s+(?:an?\s+|the\s+)?([^.;!?]+)$", "status"),
        (r"\b(?:my|his|her|their|the)\s+([a-z][a-z -]{1,32})\s+(?:is|was|are|were)\s+(.+)$", "property"),
    )
    for pattern, attribute in patterns:
        match = re.search(pattern, text, re.I)
        if not match:
            continue
        if attribute == "property":
            property_name = re.sub(
                r"[^a-z0-9]+",
                "_",
                match.group(1).casefold(),
            ).strip("_")
            return (
                f"property:{property_name}" if property_name else "property",
                match.group(2).strip(" .,!;:") or None,
            )
        return attribute, match.group(1).strip(" .,!;:") or None
    return None, None


def _fact_from_sentence(
    text: str,
    *,
    speaker: str,
    known_speakers: tuple[str, ...],
    source_ref: dict,
    recorded_at: str | None,
    is_caption: bool,
) -> AtomicFactDraft | None:
    content = _clean_sentence(text)
    if not _is_claim(content):
        return None
    if is_caption:
        assertion = "fact"
    elif _PLAN.search(content) and not _ONGOING_STATE.search(content):
        assertion = "plan"
    elif _HYPOTHETICAL.search(content):
        assertion = "hypothetical"
    elif _UNCERTAIN.search(content):
        assertion = "uncertain"
    else:
        assertion = "fact"
    polarity = "negative" if _NEGATION.search(content) else "positive"
    fact_kind = (
        "event"
        if is_caption or not _STATE.search(content)
        else "state"
    )
    attribute, value = _attribute_and_value(content)
    # The source turn keeps speaker and utterance in separate fields, while the
    # embedding and answer paths consume MemoryRecord.content. Preserve the
    # utterance verbatim but carry the speaker into the indexed text so a
    # question that names a participant does not lose its lexical anchor.
    indexed_content = content if is_caption else f"{speaker}: {content}"
    return AtomicFactDraft(
        content=indexed_content,
        entity=(
            None
            if is_caption
            else _entity(content, speaker, known_speakers)
        ),
        attribute=attribute,
        value=value,
        fact_kind=fact_kind,
        assertion_type=assertion,
        polarity=polarity,
        recorded_at=recorded_at,
        event_time=_parse_event_time(content, recorded_at),
        source_refs=[source_ref],
    )
def _extract_facts_payload(payload: object) -> tuple[AtomicFactDraft, ...]:
    try:
        if not isinstance(payload, dict):
            raise ValueError("payload")
        turns = payload["turns"]
        if not isinstance(turns, list):
            raise ValueError("turns")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise ValueError("WINDOW_INPUT_INVALID") from None
    known_speakers = tuple(
        sorted(
            {
                turn["speaker"]
                for turn in turns
                if isinstance(turn, dict)
                and isinstance(turn.get("speaker"), str)
                and turn["speaker"].strip()
            },
            key=str.casefold,
        )
    )
    facts: list[AtomicFactDraft] = []
    seen: set[tuple[str, str, str]] = set()
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        source_ref = turn.get("source_ref")
        speaker = turn.get("speaker")
        visible_text = turn.get("visible_text")
        recorded_at = turn.get("recorded_at")
        if (
            not isinstance(source_ref, dict)
            or not isinstance(source_ref.get("session_id"), str)
            or not isinstance(source_ref.get("dia_id"), str)
            or not isinstance(speaker, str)
            or not isinstance(visible_text, str)
        ):
            continue
        for sentence, is_caption in _sentence_parts(visible_text):
            fact = _fact_from_sentence(
                sentence,
                speaker=speaker,
                known_speakers=known_speakers,
                source_ref=source_ref,
                recorded_at=recorded_at if isinstance(recorded_at, str) else None,
                is_caption=is_caption,
            )
            if fact is None:
                continue
            key = (
                source_ref["session_id"],
                source_ref["dia_id"],
                fact.content.casefold(),
            )
            if key in seen:
                continue
            seen.add(key)
            facts.append(fact)
    return tuple(facts)


def extract_window_auto(window: ExtractionWindow) -> WindowExtractionResult:
    """Extract exact source sentences from one immutable dialogue window."""
    if not isinstance(window, ExtractionWindow):
        raise ValueError("expected an ExtractionWindow")
    try:
        facts = _extract_facts_payload(json.loads(window.input_text))
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ValueError("WINDOW_INPUT_INVALID") from None
    return WindowExtractionResult(
        sample_id=window.sample_id,
        extraction_window_id=window.extraction_window_id,
        facts=facts,
        template_fingerprint=MEMORY_DRAFT_PROMPT.template_fingerprint,
        input_tokens_estimated=max(1, len(window.input_text.encode("utf-8"))),
        cache_status="auto",
    )


@dataclass(frozen=True, slots=True)
class RuleBasedFactExtractionAdapter:
    """Adapter that satisfies the existing runtime contract without network I/O."""

    def generate(self, request: LLMRequest) -> LLMResponse:
        if (
            not isinstance(request, LLMRequest)
            or request.task != "memory_extraction"
            or request.output_mode != "json"
        ):
            raise ValueError("unsupported deterministic extraction request")
        prefix = "Visible conversation window JSON:\n"
        user = request.messages[-1].content
        if not user.startswith(prefix):
            raise ValueError("extraction prompt is not recognizable")
        try:
            payload = json.loads(user[len(prefix) :])
            facts = _extract_facts_payload(payload)
        except Exception:
            raise ValueError("extraction input is invalid") from None
        raw = _canonical({
            "facts": [fact.model_dump(mode="json") for fact in facts]
        })
        return LLMResponse(
            output_mode="json",
            raw_output=raw,
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=0, completion_tokens=0),
            model_id=None,
        )


def _estimate_messages(messages) -> int:
    return sum(len(message.content.encode("utf-8")) + 16 for message in messages)


def create_auto_extraction_runtime(settings) -> LLMRuntime:
    """Create the existing runtime around a deterministic extraction adapter."""
    automation_settings = settings.model_copy(update={
        "models": settings.models.model_copy(update={
            "extraction_max_input_tokens": settings.extraction.window_max_input_tokens,
            "extraction_omit_max_tokens": False,
            "extraction_max_output_tokens": 2048,
            "reasoning_omit_max_tokens": False,
            "reasoning_max_output_tokens": 2048,
            "qa_omit_max_tokens": False,
            "qa_max_output_tokens": 2048,
        })
    })
    return create_runtime(
        automation_settings,
        backend="mock",
        synthetic_adapter=RuleBasedFactExtractionAdapter(),
        token_estimator=_estimate_messages,
        estimator_version="auto-utterance-utf8-v2",
        strict_budget=True,
        cache_enabled=False,
    )
