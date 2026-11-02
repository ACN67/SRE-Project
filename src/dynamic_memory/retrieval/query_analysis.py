"""Conservative, offline query analysis. No facts, Gold or machine date used."""

from __future__ import annotations

import calendar
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from dynamic_memory.contracts.models import QueryAnalysis, QueryIntent, QueryRequest, TimeIssue

QUERY_ANALYSIS_VERSION = "c-query-rules-v2"

_MONTHS = {
    name.casefold(): number
    for number in range(1, 13)
    for name in (calendar.month_name[number], calendar.month_abbr[number])
}
_MONTH = "(?:" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + ")"
_DATE_PATTERNS = (
    re.compile(r"(?<!\d)(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})(?!\d)"),
    re.compile(r"(?P<y>\d{4})年\s*(?P<m>\d{1,2})月\s*(?P<d>\d{1,2})[日号]?"),
    re.compile(rf"\b(?P<month>{_MONTH})\.?\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?[,]?\s+(?P<y>\d{{4}})\b", re.I),
    re.compile(rf"\b(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<month>{_MONTH})\.?[,]?\s+(?P<y>\d{{4}})\b", re.I),
    re.compile(r"(?<!\d)(?P<y>\d{4})-(?P<m>\d{1,2})(?![-\d])"),
    re.compile(r"(?P<y>\d{4})年\s*(?P<m>\d{1,2})月"),
    re.compile(rf"\b(?P<month>{_MONTH})\.?\s+(?P<y>\d{{4}})\b", re.I),
    re.compile(r"(?<!\d)(?P<y>\d{4})年?(?!\d)"),
)
_RELATIVE = re.compile(
    r"\b(?:today|yesterday|tomorrow|(?:last|next|this)\s+(?:week|weekend|month|year|spring|summer|autumn|fall|winter|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)|(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|a|an)\s+(?:days?|weeks?|months?|years?)\s+ago)\b"
    r"|\bon\s+(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b"
    r"|今天|昨天|明天|去年|今年|明年|上周|下周|本周|上个月|下个月|这个月|\d+[天周年月]前", re.I,
)
_RANGE = re.compile(
    rf"\b(?:before|after|since|until|between|during|through|around|approximately)\b"
    rf"|\bby\s+(?:\d{{4}}|{_MONTH}\b)|\bnot\s+on\b"
    r"|之前|之后|以前|以后|截至|期间|之间|自从|左右|从\s*\d{4}", re.I,
)
_AMBIGUOUS_NUMERIC_DATE = re.compile(r"\d{1,4}/\d{1,2}/\d{1,4}")
_UNSUPPORTED_DATE = re.compile(r"\d{1,4}/\d{1,2}/\d{1,4}|\d{1,2}月|\b" + _MONTH + r"\b", re.I)
_ATTRIBUTES = (
    ("employment", r"\b(?:work|works|worked|working|job|jobs|career|employer|employed|employment|resign|resigned|quit|hired)\b|工作|职业|入职|离职|就职"),
    ("residence", r"\b(?:live|lives|lived|living|reside|residence|move|moved)\b|居住|住在|搬家|搬到"),
    ("preference", r"\b(?:favorite|favourite|prefer|prefers|like|likes|liked)\b|最喜欢|偏好|喜爱"),
    ("education", r"\b(?:study|studied|school|university|college|graduate|graduated|degree)\b|毕业|学校|大学|学位"),
    ("adoption_stage", r"\b(?:adopt|adopted|adoption)\b|收养|领养"),
)
_STOP_NAMES = set("What When Where Who Why How Which Does Did Do Is Was Were Has Have Had Can Could Will Would Tell List In On At As Of Now Currently Previously Before After Since Until Between From The A An I He She They His Her Their Please".split())
_STOP_NAMES.update(name.title() for name in _MONTHS)
_STOP_NAMES.update(calendar.day_name)
_NAME = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b")


@dataclass(frozen=True)
class _QuestionTime:
    raw: str
    start: date | None
    end: date | None  # Inclusive for comparing an explicit as_of.
    exact: bool


def _question_times(text: str) -> tuple[list[_QuestionTime], str]:
    remaining = text
    times = []
    for pattern in _DATE_PATTERNS:
        for match in list(pattern.finditer(remaining)):
            fields = match.groupdict()
            try:
                year = int(fields["y"])
                month = int(fields["m"]) if fields.get("m") else _MONTHS.get((fields.get("month") or "").casefold())
                day = int(fields["d"]) if fields.get("d") else None
                if day is not None:
                    start = end = date(year, month, day)
                elif month is not None:
                    start = date(year, month, 1)
                    end = date(year, month, calendar.monthrange(year, month)[1])
                else:
                    start, end = date(year, 1, 1), date(year, 12, 31)
            except (ValueError, TypeError):
                start = end = None
            times.append(_QuestionTime(text[match.start():match.end()], start, end, bool(fields.get("d"))))
            remaining = remaining[:match.start()] + " " * (match.end() - match.start()) + remaining[match.end():]
    return times, remaining


def _intent(text: str, dated: bool, attributes: list[str]) -> QueryIntent:
    if re.search(r"\bwhen\b|\bwhat (?:year|date|happened)\b|什么时候|何时|哪一年|哪天|先后|发生了什么|\b(?:did|has|have)\b.*\b(?:happen|happened|occur|occurred|start|started|quit|resign|resigned|move|moved|graduate|graduated|adopt|adopted|open|opened|close|closed|marry|married|join|joined)\b", text, re.I):
        return "event_fact"
    if re.search(r"\bever\b|\bused to\b|\bover time\b|\b(?:previous|past|all)\s+(?:jobs|employers|roles|places|stages)\b|曾经|曾在哪|工作过|住过|历任|历次|变化历程|变化过程", text, re.I):
        return "state_history"
    if re.search(r"\b(?:now|currently|current|latest)\b|现在|目前|当前|最新", text, re.I):
        return "current_state"
    if re.search(r"\b(?:favorite|favourite|prefer|prefers|like|likes)\b|最喜欢|偏好|喜爱", text, re.I):
        return "general_fact"
    if dated or re.search(r"\b(?:previously|formerly|at that time)\b|当时|过去", text, re.I):
        return "historical_state"
    if any(attr in attributes for attr in ("employment", "residence")) and re.search(r"\bdid\b", text, re.I):
        return "state_history"
    if any(attr in attributes for attr in ("employment", "residence")) and re.search(r"\b(?:was|were)\b", text, re.I):
        return "historical_state"
    if any(attr in attributes for attr in ("employment", "residence")) and re.search(r"\b(?:does|is|are)\b|在哪|哪里|什么工作", text, re.I):
        return "current_state"
    if re.search(r"^(?:what|who|where|which|why|how)\b|什么|谁|哪里|哪个|为什么|如何", text, re.I):
        return "general_fact"
    return "unknown"


class QueryAnalyzer:
    """Optional sample-local entity vocabulary improves hints without guessing aliases.

    Hints are advisory; consumers must not use them as mandatory filters.
    With no vocabulary, only conservative English proper-name hints are emitted.
    """

    def __init__(self, entity_names: Sequence[str] = ()):
        if isinstance(entity_names, str) or any(not isinstance(name, str) or not name.strip() for name in entity_names):
            raise ValueError("entity_names must contain nonempty strings")
        self._entity_names = tuple(dict.fromkeys(entity_names))

    def analyze(self, request: QueryRequest) -> QueryAnalysis:
        text = request.question.strip()
        attributes = [name for name, pattern in _ATTRIBUTES if re.search(pattern, text, re.I)]
        times, remaining = _question_times(text)
        relative = bool(_RELATIVE.search(text))
        # Inspect slash dates before date masking can consume their year.
        unsupported = bool(_UNSUPPORTED_DATE.search(remaining) or _AMBIGUOUS_NUMERIC_DATE.search(text))
        dated = bool(times or relative or unsupported or request.as_of)
        entities = []
        if self._entity_names:
            for name in self._entity_names:
                # ASCII word boundaries avoid Sam matching Samantha; Chinese names
                # can be directly adjacent to other Chinese words.
                pattern = re.escape(name)
                if name.isascii():
                    pattern = r"(?<!\w)" + pattern + r"(?!\w)"
                if re.search(pattern, text, re.I):
                    entities.append(name)
        else:
            for match in _NAME.finditer(remaining):
                parts = match.group().split()
                parts = [part for part in parts if part not in _STOP_NAMES]
                if parts:
                    entities.append(" ".join(parts))
        intent = _intent(text, dated, attributes)
        result = dict(query_id=request.query_id, sample_id=request.sample_id,
                      query_intent=intent, entity_hints=list(dict.fromkeys(entities)),
                      attribute_hints=attributes)

        # Only a reliably interpreted question time can establish a conflict.
        if request.as_of:
            conflicts = [item for item in times if item.start and not item.start <= request.as_of <= item.end]
            # Range endpoints are bounds, not separate requested dates.
            distinct_times = {(item.start, item.end) for item in times}
            if (conflicts and len(distinct_times) == 1 and not _RANGE.search(text)
                    and not relative and not unsupported):
                return QueryAnalysis(**result, time_mode="conflict", time_issue=TimeIssue(
                    code="QUERY_TIME_CONFLICT", message="Explicit as_of disagrees with the question time.",
                    explicit_as_of=request.as_of.isoformat(), question_time="; ".join(item.raw for item in conflicts),
                ))
        uncertain = relative or unsupported or any(item.start is None or not item.exact for item in times)
        uncertain = uncertain or len({item.start for item in times}) > 1 or bool(_RANGE.search(text))
        if uncertain:
            return QueryAnalysis(**result, time_mode="unknown", time_issue=TimeIssue(
                code="TIME_PARSE_UNCERTAIN",
                message="Relative, ambiguous, invalid or interval time needs further interpretation; no date was guessed.",
                explicit_as_of=request.as_of.isoformat() if request.as_of else None,
                question_time=text,
            ))
        resolved = request.as_of or (times[0].start if times else None)
        if resolved:
            return QueryAnalysis(**result, time_mode="as_of", resolved_as_of=resolved)
        return QueryAnalysis(**result, time_mode="latest_known")


def analyze_query(request: QueryRequest) -> QueryAnalysis:
    """Shared C/A/D entry point; deterministic and independent of the computer date."""
    return QueryAnalyzer().analyze(request)
