"""B-04: deterministic, offline planning of session-local extraction windows."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import ValidationError

from dynamic_memory.contracts import ConversationSample, SourceRef, render_visible_turn
from dynamic_memory.pipeline.settings import ExtractionSettings


class WindowPlanningError(ValueError):
    """A sanitized, diagnosable planning failure; never contains conversation text."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class WindowTurn:
    session_id: str
    dia_id: str
    speaker: str
    visible_text: str = field(repr=False)
    recorded_at: str | None

    @property
    def source_ref(self) -> SourceRef:
        return SourceRef(session_id=self.session_id, dia_id=self.dia_id)


@dataclass(frozen=True, slots=True)
class ExtractionWindow:
    sample_id: str
    session_id: str
    extraction_window_id: str
    turns: tuple[WindowTurn, ...] = field(repr=False)
    input_text: str = field(repr=False)
    max_input_tokens: int
    reserved_prompt_tokens: int
    estimated_payload_tokens: int

    @property
    def source_refs(self) -> tuple[SourceRef, ...]:
        return tuple(turn.source_ref for turn in self.turns)

    @property
    def visible_turns(self) -> tuple[str, ...]:
        return tuple(turn.visible_text for turn in self.turns)

    @property
    def estimated_total_tokens(self) -> int:
        return self.reserved_prompt_tokens + self.estimated_payload_tokens


def _serialize(sample_id: str, session_id: str, turns: tuple[WindowTurn, ...]) -> str:
    payload = {
        "sample_id": sample_id,
        "session_id": session_id,
        "turns": [
            {
                "source_ref": {"session_id": turn.session_id, "dia_id": turn.dia_id},
                "speaker": turn.speaker,
                "recorded_at": turn.recorded_at,
                "visible_text": turn.visible_text,
            }
            for turn in turns
        ],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def plan_extraction_windows(
    sample: ConversationSample,
    settings: ExtractionSettings,
    *,
    token_estimator: Callable[[str], int | None] | None = None,
    estimator_version: str | None = None,
    reserved_prompt_tokens: int = 512,
) -> list[ExtractionWindow]:
    """Plan windows without calling a model; the final prompt needs a later budget check."""
    if not isinstance(sample, ConversationSample):
        raise WindowPlanningError("INVALID_SAMPLE", "expected a ConversationSample")
    if not isinstance(settings, ExtractionSettings):
        raise WindowPlanningError("WINDOW_BUDGET_INVALID", "expected extraction settings")
    try:
        sample = ConversationSample.model_validate(sample.model_dump(mode="python", warnings="error"))
        settings = ExtractionSettings.model_validate(settings.model_dump(mode="python", warnings="error"))
    except (ValidationError, ValueError, TypeError, AttributeError):
        raise WindowPlanningError("INVALID_SAMPLE_OR_SETTINGS", "sample or settings failed validation") from None
    if (isinstance(reserved_prompt_tokens, bool) or not isinstance(reserved_prompt_tokens, int)
            or not 0 < reserved_prompt_tokens < settings.window_max_input_tokens):
        raise WindowPlanningError("WINDOW_BUDGET_INVALID", "prompt reserve leaves no input budget")
    if token_estimator is not None and not callable(token_estimator):
        raise WindowPlanningError("TOKEN_ESTIMATE_FAILED", "token estimator is not callable")
    if token_estimator is not None and (not isinstance(estimator_version, str) or not estimator_version.strip()):
        raise WindowPlanningError("TOKEN_ESTIMATE_FAILED", "token estimator needs a version")
    if token_estimator is None and estimator_version is not None:
        raise WindowPlanningError("TOKEN_ESTIMATE_FAILED", "default estimator has a fixed version")
    estimator = token_estimator or (lambda content: len(content.encode("utf-8")))
    version = estimator_version or "utf8-bytes-v1"

    def estimate(content: str) -> int:
        try:
            value = estimator(content)
        except Exception:
            raise WindowPlanningError("TOKEN_ESTIMATE_FAILED", "token estimator raised an error") from None
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise WindowPlanningError("TOKEN_ESTIMATE_FAILED", "token estimate must be a positive integer")
        return value

    result: list[ExtractionWindow] = []
    for session in sample.sessions:
        turns = tuple(
            WindowTurn(
                session_id=session.session_id, dia_id=turn.dia_id,
                speaker=turn.speaker, visible_text=render_visible_turn(turn),
                recorded_at=turn.recorded_at.isoformat() if turn.recorded_at else None,
            )
            for turn in session.turns
        )
        if not turns:
            continue
        start = 0
        previous_end = 0
        while start < len(turns):
            end = start
            chosen_text = ""
            chosen_estimate = 0
            while end < len(turns):
                candidate = _serialize(sample.sample_id, session.session_id, turns[start:end + 1])
                candidate_estimate = estimate(candidate)
                if candidate_estimate + reserved_prompt_tokens > settings.window_max_input_tokens:
                    break
                chosen_text, chosen_estimate = candidate, candidate_estimate
                end += 1
            if end == start:
                raise WindowPlanningError("WINDOW_INPUT_EXCEEDS_BUDGET", "one turn exceeds extraction budget")
            if end <= previous_end:
                raise WindowPlanningError("WINDOW_OVERLAP_NO_PROGRESS", "overlap leaves no budget for a new turn")
            window_turns = turns[start:end]
            identity = json.dumps(
                {
                    "input": chosen_text,
                    "max_input_tokens": settings.window_max_input_tokens,
                    "overlap_turns": settings.window_overlap_turns,
                    "reserved_prompt_tokens": reserved_prompt_tokens,
                    "estimator_version": version,
                    "planner_version": 1,
                },
                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            )
            window_id = "window_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
            result.append(ExtractionWindow(
                sample_id=sample.sample_id, session_id=session.session_id,
                extraction_window_id=window_id, turns=window_turns, input_text=chosen_text,
                max_input_tokens=settings.window_max_input_tokens,
                reserved_prompt_tokens=reserved_prompt_tokens,
                estimated_payload_tokens=chosen_estimate,
            ))
            if end == len(turns):
                break
            next_start = end - settings.window_overlap_turns
            if next_start <= start:
                raise WindowPlanningError("WINDOW_OVERLAP_NO_PROGRESS", "overlap cannot advance to a new turn")
            previous_end = end
            start = next_start
    return result
