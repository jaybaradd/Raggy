"""Resolve safe conversational targets for proposed event updates."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from core.memory.models import MemoryRecord
from core.memory.projection import memory_text
from core.retrieval.memory_scope import is_memory_record_eligible

_RECENT_MESSAGE_LIMIT = 10
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UpdateResolution:
    """Reply-time decision plus the uniquely traced event for extraction."""

    target: MemoryRecord | None
    recent_messages: list[dict[str, str]]
    ambiguous: bool = False
    # A single event used in the prior assistant answer is a reliable boundary
    # for memory extraction even when the intent classifier says this turn is
    # not an update.  That prevents an indirect change statement from being
    # stored as an unconnected event.
    extraction_target: MemoryRecord | None = None


class _UpdateDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["update", "not_update", "ambiguous"]


async def resolve_update_context(*, user_content: str, messages: list[dict[str, Any]], store: Any,
                           provider: Any, owner_id: str, session_id: str, project_id: str | None,
                           project_scope: str | None) -> UpdateResolution:
    """Classify a message against only the prior answer's real event target.

    The answer planner is deliberately not consulted here: its semantic
    candidates may contain a different project event. The recent window is
    reference only; the prior response trace supplies the actual target.
    """
    recent = [
        {"role": str(message["role"]), "content": str(message["content"])}
        for message in messages[-_RECENT_MESSAGE_LIMIT:]
    ]
    previous_assistant = next((message for message in reversed(messages) if message.get("role") == "assistant"), None)
    if previous_assistant is None or not previous_assistant.get("trace_id"):
        logger.info("Update resolver: no prior assistant trace for session %s", session_id)
        return UpdateResolution(None, recent)
    memory_ids = store.list_injected_memory_ids(
        trace_id=str(previous_assistant["trace_id"]), session_id=session_id,
    )
    candidates: list[MemoryRecord] = []
    for memory_id in memory_ids:
        record = store.get_owned(memory_id, owner_id=owner_id)
        if (record is not None and record.kind == "event" and is_memory_record_eligible(
            record, owner_id=owner_id, session_id=session_id,
            project_id=project_id, project_scope=project_scope,
        )):
            candidates.append(record)
    if len(candidates) != 1:
        logger.info("Update resolver: %d eligible prior-response events for session %s", len(candidates), session_id)
        return UpdateResolution(None, recent, ambiguous=bool(candidates))
    target = candidates[0]
    try:
        raw = await provider.generate_json(_decision_prompt(user_content, recent, target))
        decision = _UpdateDecision.model_validate(raw)
    except Exception:
        logger.warning("Update resolver: decision unavailable for prior event %s", target.memory_id)
        return UpdateResolution(None, recent, ambiguous=True, extraction_target=target)
    if decision.outcome == "update":
        logger.info("Update resolver: update for prior event %s", target.memory_id)
        return UpdateResolution(target, recent, extraction_target=target)
    logger.info("Update resolver: %s for prior event %s", decision.outcome, target.memory_id)
    return UpdateResolution(
        None, recent, ambiguous=decision.outcome == "ambiguous", extraction_target=target,
    )


def _decision_prompt(user_content: str, recent_messages: list[dict[str, str]], target: MemoryRecord) -> str:
    return f"""Decide whether the current user message proposes a change to the one
authoritative event below. Use conversation only to resolve references such as
pronouns. An indirect statement can still assert a change to this event. Do
not treat a question, a hypothetical, or a request for information as an
asserted change. Do not infer a different event, and do not calculate or write
an updated value. Return JSON only: {{"outcome": "update"}},
{{"outcome": "not_update"}}, or {{"outcome": "ambiguous"}}.

Authoritative event:
{memory_text(target)}

Recent conversation reference:
{recent_messages}

Current user message:
{user_content}
"""
