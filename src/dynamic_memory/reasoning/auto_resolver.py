"""Deterministic state resolution over the frozen candidate set."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone

from dynamic_memory.contracts import (
    MemoryDecision,
    MemoryRecord,
    RelationEdge,
    ResolutionResult,
    SourceRef,
)

from .common import DynamicReasoningError, checked_inputs, validate_resolution

AUTO_RESOLVER_VERSION = "deterministic-state-v5-state-chain"

_CHANGE = re.compile(
    r"\b(?:now|currently|instead|changed?|became|become|moved|switch(?:ed)?|"
    r"replaced?|no longer|not anymore|cancel(?:led|ed)?|ended|stopped)\b",
    re.I,
)
_PLAN = re.compile(
    r"\b(?:will|would|going to|gonna|plan(?:ned|ning)?|intend(?:ed|ing)?|"
    r"hope|hoping|want|wants|wanted|aim|aiming)\b",
    re.I,
)
_STOP = frozenset(
    "the a an and or but if then than to of in on at for from with about as is was "
    "were are be been being do did does have has had i me my we our you your he she "
    "his her they their it this that these those what when where who whom which why "
    "how current currently latest still now before after state status".split()
)
_STATEFUL_ATTRIBUTES = frozenset(
    {
        "residence",
        "employment",
        "current_work",
        "search_target",
        "goal",
        "venture_status",
        "currently_reading",
        "current_game",
        "plan",
        "relationship_status",
        "commitment",
        "event_preparation",
        "achievement",
    }
)
_ATTRIBUTE_COMPATIBILITY = {
    "employment": frozenset({"employment", "current_work", "venture_status"}),
    "current_work": frozenset({"employment", "current_work", "venture_status"}),
    "plan": frozenset({"plan", "goal", "venture_status"}),
    "goal": frozenset({"goal", "plan", "search_target"}),
    "gaming": frozenset({"gaming", "current_game"}),
    "achievement": frozenset({"achievement", "tournament_wins"}),
    "event_preparation": frozenset({"event_preparation"}),
    "creative_project": frozenset({"creative_project"}),
}
_STATE_ATTRIBUTE_GROUPS = {
    "employment": "work",
    "current_work": "work",
    "venture_status": "work",
    "plan": "planning",
    "goal": "planning",
    "search_target": "planning",
    "event_preparation": "planning",
    "achievement": "competition",
}
_MONTHS = {
    name.casefold(): index
    for index, name in enumerate(
        (
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ),
        1,
    )
}


def _norm(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).casefold()


def _stem(token: str) -> str:
    aliases = {
        "biz": "business",
        "businesses": "business",
        "gameing": "gaming",
        "job": "employment",
        "jobs": "employment",
        "reccomend": "recommend",
        "reading": "read",
        "books": "book",
        "won": "win",
        "winning": "win",
        "playing": "play",
        "played": "play",
        "recommends": "recommend",
        "recommended": "recommend",
        "creating": "create",
        "created": "create",
        "working": "work",
        "worked": "work",
        "projects": "project",
        "determined": "determine",
    }
    return aliases.get(token, token)


def _terms(value: str) -> set[str]:
    return {
        _stem(token)
        for token in re.findall(r"[a-z][a-z0-9'-]{1,}", value.casefold())
        if token not in _STOP
    }


def _when(memory: MemoryRecord) -> datetime | None:
    value = (
        memory.event_time.start
        if memory.event_time is not None
        else memory.recorded_at
    )
    return value


def _comparable(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _sort_time(memory: MemoryRecord) -> tuple[int, datetime]:
    value = _when(memory)
    return (1, _comparable(value)) if value is not None else (0, datetime.min)


def _ordered(left: MemoryRecord, right: MemoryRecord) -> bool:
    left_at, right_at = _when(left), _when(right)
    if left_at is None or right_at is None:
        return (
            left.recorded_at is not None
            and right.recorded_at is not None
            and _comparable(left.recorded_at) <= _comparable(right.recorded_at)
        )
    try:
        return _comparable(left_at) <= _comparable(right_at)
    except (TypeError, ValueError, OverflowError):
        return False


def _shared_refs(left: MemoryRecord, right: MemoryRecord) -> list[SourceRef]:
    left_refs = {(ref.session_id, ref.dia_id): ref for ref in left.source_refs}
    right_refs = {(ref.session_id, ref.dia_id): ref for ref in right.source_refs}
    return [
        left_refs[key]
        for key in sorted(left_refs.keys() & right_refs.keys())
    ]


def _relation_refs(left: MemoryRecord, right: MemoryRecord) -> list[SourceRef]:
    refs = {
        (ref.session_id, ref.dia_id): ref
        for memory in (left, right)
        for ref in memory.source_refs
    }
    return [refs[key] for key in sorted(refs)]


def _explicit_change(text: str) -> bool:
    return bool(_CHANGE.search(text))


def _is_plan(memory: MemoryRecord) -> bool:
    if memory.assertion_type == "fact":
        return False
    if memory.assertion_type in {"plan", "hypothetical", "uncertain"}:
        return True
    return bool(_PLAN.search(memory.content))


def _compatible_attributes(candidate: str | None, hints: list[str]) -> bool:
    if not candidate or not hints:
        return False
    normalized = _norm(candidate)
    for hint in hints:
        normalized_hint = _norm(hint)
        accepted = _ATTRIBUTE_COMPATIBILITY.get(
            normalized_hint,
            frozenset({normalized_hint}),
        )
        if normalized == normalized_hint or normalized in accepted:
            return True
        if normalized.startswith("property:") and normalized_hint == "property":
            return True
    return False


def _question_date(question: str) -> date | None:
    match = re.search(
        r"\b(?:on|by)?\s*(" + "|".join(_MONTHS) + r")\s+"
        r"(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b",
        question,
        re.I,
    )
    if match:
        try:
            return date(
                int(match.group(3)),
                _MONTHS[match.group(1).casefold()],
                int(match.group(2)),
            )
        except ValueError:
            return None
    iso = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", question)
    if iso:
        try:
            return date(*(int(value) for value in iso.groups()))
        except ValueError:
            return None
    return None


def _state_attribute_group(attribute: str) -> str:
    normalized = _norm(attribute)
    return _STATE_ATTRIBUTE_GROUPS.get(normalized, normalized)


def _entity_key(memory: MemoryRecord) -> str | None:
    if memory.entity:
        return _norm(memory.entity)
    match = re.match(r"^\s*([^:\n]{1,80}):\s*", memory.content)
    if match:
        return _norm(match.group(1))
    return None


def _required_predicate_terms(question: str) -> set[str]:
    value = question.casefold()
    required = set()
    if re.search(r"\bread(?:ing|s)?\b", value):
        required.update({"read", "book"})
    if re.search(r"\bwon\b", value):
        required.add("win")
    if re.search(r"\bplaying\b|\brecommends?\b", value):
        required.update({"play", "game"})
    if re.search(r"\bcreating\b", value):
        required.add("create")
    if re.search(r"\bworking\b", value):
        required.add("work")
    return required


def _relevance_terms(question: str) -> set[str]:
    terms = _terms(question)
    value = question.casefold()
    if "movie" in terms or "script" in terms:
        terms.update({"movie", "script", "screenplay", "story", "screen"})
    if "job" in terms or "duty" in terms or "duties" in terms:
        terms.update({"job", "work", "determine"})
    return terms


def _relevant(memory: MemoryRecord, analysis, question: str) -> bool:
    text = memory.content.casefold()
    entities = [
        entity.casefold()
        for entity in analysis.entity_hints
        if isinstance(entity, str) and entity.strip()
    ]
    memory_entity = _norm(memory.entity)
    if entities and not any(
        entity in text or entity == memory_entity
        for entity in entities
    ):
        return False
    target_date = analysis.resolved_as_of or _question_date(question)
    if (
        target_date is not None
        and memory.recorded_at is not None
        and _comparable(memory.recorded_at).date() > target_date
    ):
        return False
    key_terms = _relevance_terms(question) - set(entities)
    memory_terms = _terms(
        " ".join(
            value
            for value in (
                memory.content,
                memory.value,
                memory.attribute,
            )
            if value
        )
    )
    overlaps = key_terms & memory_terms
    required = _required_predicate_terms(question)
    if required and not required & memory_terms:
        return False
    attribute_match = bool(memory.attribute and _compatible_attributes(
        memory.attribute,
        analysis.attribute_hints,
    ))
    if analysis.attribute_hints:
        return attribute_match or len(overlaps) >= 2
    exact_date = (
        target_date is not None
        and memory.recorded_at is not None
        and _comparable(memory.recorded_at).date() == target_date
    )
    if required:
        attribute_terms = _terms(memory.attribute or "")
        if required & attribute_terms:
            return True
        if exact_date and len(overlaps) >= 1:
            return True
    return len(overlaps) >= 2 or (exact_date and len(overlaps) >= 1)


class DeterministicStateResolver:
    """Resolve state with explicit lexical, temporal, and provenance rules.

    The resolver fails closed: a memory is excluded only when a newer record is
    provably an update/replacement for the same entity and attribute. Ambiguous
    contradictions remain unresolved. Candidate order is preserved.
    """

    def __init__(self, *, conversation_cutoff=None, on_diagnostic=None):
        if conversation_cutoff is not None and not callable(conversation_cutoff):
            raise DynamicReasoningError("DEPENDENCY_INVALID")
        if on_diagnostic is not None and not callable(on_diagnostic):
            raise DynamicReasoningError("DEPENDENCY_INVALID")
        self.conversation_cutoff = conversation_cutoff
        self.on_diagnostic = on_diagnostic

    def __call__(self, request, analysis, candidates):
        return self.resolve(request, analysis, candidates)

    def resolve(self, request, analysis, candidates) -> ResolutionResult:
        request, analysis, memories = checked_inputs(request, analysis, candidates)
        if not memories:
            return validate_resolution(
                ResolutionResult(query_id=request.query_id),
                request,
                memories,
            )
        if analysis.time_mode == "conflict":
            result = ResolutionResult(
                query_id=request.query_id,
                decisions=[
                    MemoryDecision(
                        memory_id=memory.memory_id,
                        action="UNRESOLVED",
                        reason="The query has an unresolved explicit time conflict.",
                    )
                    for memory in memories
                ],
                uncertainty="The query time conditions conflict.",
            )
            return validate_resolution(result, request, memories)

        cutoff = None
        if self.conversation_cutoff is not None:
            try:
                cutoff = self.conversation_cutoff(request.sample_id)
                if cutoff is not None and not isinstance(cutoff, datetime):
                    raise ValueError
            except Exception:
                raise DynamicReasoningError("CUTOFF_INVALID") from None

        actions = {
            memory.memory_id: (
                "RETAIN"
                if _relevant(memory, analysis, request.question)
                else "EXCLUDE"
            )
            for memory in memories
        }
        strong_sources = {
            (ref.session_id, ref.dia_id)
            for memory in memories
            if actions[memory.memory_id] == "RETAIN"
            for ref in memory.source_refs
        }
        for memory in memories:
            if (
                actions[memory.memory_id] == "EXCLUDE"
                and any(
                    (ref.session_id, ref.dia_id) in strong_sources
                    for ref in memory.source_refs
                )
            ):
                # A source turn is the answer context unit. If any fact from
                # that turn is relevant, keep all reviewed facts from it.
                actions[memory.memory_id] = "RETAIN"
        reasons = {
            memory.memory_id: (
                "Candidate is relevant to the requested fact."
                if actions[memory.memory_id] == "RETAIN"
                else "Candidate does not match the question's subject or state terms."
            )
            for memory in memories
        }
        relations: list[RelationEdge] = []
        unresolved_conflicts: set[str] = set()

        grouped: dict[tuple[str, str], list[MemoryRecord]] = {}
        for memory in memories:
            entity = _entity_key(memory)
            if (
                entity
                and memory.attribute
                and (
                    _norm(memory.attribute) in _STATEFUL_ATTRIBUTES
                    or _norm(memory.attribute).startswith("property:")
                )
            ):
                grouped.setdefault(
                    (
                        entity,
                        _state_attribute_group(memory.attribute),
                    ),
                    [],
                ).append(memory)

        for (entity, attribute), group in grouped.items():
            ordered = sorted(group, key=_sort_time)
            for index, newer in enumerate(ordered):
                for older in reversed(ordered[:index]):
                    if older.memory_id == newer.memory_id:
                        continue
                    refs = _relation_refs(older, newer)
                    if not refs:
                        continue
                    if not _ordered(older, newer):
                        continue
                    same_value = _norm(older.value) == _norm(newer.value)
                    if same_value and older.polarity == newer.polarity:
                        continue
                    explicit = _explicit_change(newer.content) or (
                        _is_plan(older) and not _is_plan(newer)
                    )
                    if not explicit:
                        older_terms = _terms(older.value or older.content)
                        newer_terms = _terms(newer.value or newer.content)
                        if not older_terms & newer_terms:
                            # Co-existing projects or preferences are not a
                            # chronological replacement merely because they
                            # share a broad attribute label.
                            continue
                    if (
                        actions[older.memory_id] != "RETAIN"
                        or actions[newer.memory_id] != "RETAIN"
                    ):
                        continue
                    relation = "SUPERSEDE" if explicit else "UPDATE"
                    if relation == "SUPERSEDE" and _is_plan(newer):
                        relation = "UPDATE"
                    if (
                        analysis.query_intent in {"current_state", "historical_state"}
                        and not explicit
                        and older.polarity != newer.polarity
                    ):
                        relation = "CONFLICT"
                    if relation == "CONFLICT":
                        unresolved_conflicts.update(
                            {older.memory_id, newer.memory_id}
                        )
                        actions[older.memory_id] = "UNRESOLVED"
                        actions[newer.memory_id] = "UNRESOLVED"
                        reasons[older.memory_id] = (
                            "Mutually incompatible claims share the same entity "
                            "and attribute without a proven chronological replacement."
                        )
                        reasons[newer.memory_id] = reasons[older.memory_id]
                    elif (
                        analysis.query_intent == "current_state"
                        and not _is_plan(newer)
                    ):
                        actions[older.memory_id] = "EXCLUDE"
                        reasons[older.memory_id] = (
                            "A newer candidate for the same entity and attribute "
                            "supersedes this candidate for a current-state question."
                        )
                        reasons[newer.memory_id] = (
                            "This is the latest relevant candidate for the entity "
                            "and attribute requested."
                        )
                    from_id, to_id = older.memory_id, newer.memory_id
                    if relation == "CONFLICT" and from_id > to_id:
                        from_id, to_id = to_id, from_id
                    relations.append(
                        RelationEdge(
                            from_memory_id=from_id,
                            to_memory_id=to_id,
                            relation=relation,
                            scope_attribute="state_transition",
                            evidence_refs=refs,
                            reason=(
                                "Deterministic same-subject state transition "
                                + ("with explicit change language." if explicit else "ordered by source time.")
                            ),
                        )
                    )
                    # Gold state transitions form a chain. Link each state to
                    # its immediately preceding distinct state instead of
                    # emitting every pairwise edge in the candidate window.
                    break

        # A date/as-of boundary is a hard evidence boundary for state queries.
        target_date: date | None = (
            analysis.resolved_as_of
            if analysis.time_mode == "as_of"
            else cutoff.date()
            if cutoff is not None and analysis.query_intent in {"current_state", "historical_state"}
            else None
        )
        if target_date is not None and analysis.query_intent in {
            "current_state",
            "historical_state",
        }:
            for memory in memories:
                if actions[memory.memory_id] == "EXCLUDE":
                    continue
                when = _when(memory)
                if when is None:
                    continue
                try:
                    if _comparable(when).date() > target_date:
                        actions[memory.memory_id] = "EXCLUDE"
                        reasons[memory.memory_id] = (
                            "The candidate's evidenced time is after the query cutoff."
                        )
                except (TypeError, ValueError, OverflowError):
                    actions[memory.memory_id] = "UNRESOLVED"
                    reasons[memory.memory_id] = (
                        "The candidate time cannot be compared with the query cutoff."
                    )

        if analysis.query_intent == "current_state":
            for memory in memories:
                if _is_plan(memory) and not any(
                    token in request.question.casefold()
                    for token in ("plan", "going to", "intend", "want", "hope")
                ):
                    if actions[memory.memory_id] == "RETAIN":
                        actions[memory.memory_id] = "EXCLUDE"
                        reasons[memory.memory_id] = (
                            "A plan is not evidence of a completed current state."
                        )

        for conflict_id in unresolved_conflicts:
            actions[conflict_id] = "UNRESOLVED"

        decisions = [
            MemoryDecision(
                memory_id=memory.memory_id,
                action=actions[memory.memory_id],
                reason=reasons[memory.memory_id],
            )
            for memory in memories
        ]
        effective = [
            memory.memory_id
            for memory in memories
            if actions[memory.memory_id] == "RETAIN"
        ]
        uncertainty = None
        if unresolved_conflicts:
            uncertainty = (
                "Conflicting candidates remain unresolved because the evidence "
                "does not establish which claim applies."
            )
        elif any(action == "UNRESOLVED" for action in actions.values()):
            uncertainty = (
                "One or more candidates cannot be compared to the requested time."
            )
        result = ResolutionResult(
            query_id=request.query_id,
            relations=relations,
            decisions=decisions,
            effective_memory_ids=effective,
            unresolved_conflict_ids=sorted(unresolved_conflicts),
            uncertainty=uncertainty,
        )
        return validate_resolution(result, request, memories)
