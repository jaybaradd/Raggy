"""Recover bounded conversation continuity candidates for the current turn."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from core.memory.models import MemoryRecord
from core.retrieval.memory_scope import is_memory_record_eligible

_RECENT_MESSAGE_LIMIT = 10
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UpdateResolution:
    """Conversation evidence supplied to the scoped memory planner.

    A previous response trace is useful evidence for resolving pronouns, but it
    is not an identity decision. The current-turn planner must still select a
    candidate and describe its relationship to the user's message.
    """

    continuity_candidates: list[MemoryRecord]
    recent_messages: list[dict[str, str]]


async def resolve_update_context(*, user_content: str, messages: list[dict[str, Any]], store: Any,
                           owner_id: str, session_id: str, project_id: str | None,
                           project_scope: str | None) -> UpdateResolution:
    """Return eligible events used in the previous answer as candidates only.

    Identifier precedence is enforced by the scoped planner, which can compare
    this evidence with durable exact and semantic candidates.
    """
    recent = [
        {"role": str(message["role"]), "content": str(message["content"])}
        for message in messages[-_RECENT_MESSAGE_LIMIT:]
    ]
    previous_assistant = next((message for message in reversed(messages) if message.get("role") == "assistant"), None)
    if previous_assistant is None or not previous_assistant.get("trace_id"):
        logger.info("Update resolver: no prior assistant trace for session %s", session_id)
        return UpdateResolution([], recent)
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
    logger.info(
        "Update resolver: supplied %d prior-response event candidates for session %s",
        len(candidates), session_id,
    )
    return UpdateResolution(candidates, recent)
