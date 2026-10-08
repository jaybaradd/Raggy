"""Bounded commands for authoritative memory lifecycle changes."""

from __future__ import annotations

from typing import Any
from uuid import NAMESPACE_URL, uuid5

from core.memory.identity import event_differences
from core.memory.models import EventMemory, MemoryRecord
from core.retrieval.memory_scope import is_memory_record_eligible


_CLAIM_CHANGE_COMMAND = "propose_event_claim_change"
_CLAIM_CHANGE_VERSION = "v1"


def propose_event_claim_change(
    *,
    store: Any,
    incoming: MemoryRecord,
    target_memory_id: str,
    owner_id: str,
    session_id: str,
    project_id: str | None,
    project_scope: str | None,
    actor_id: str = "system",
    confidence: float,
    reason: str,
    matched_identifier_values: list[str] | None = None,
    details: dict | None = None,
):
    """Propose one scoped event-claim replacement for human review.

    The caller may select a target, but it cannot grant access to it. This
    command rehydrates the target from the authoritative repository, checks
    current chat visibility, and delegates the atomic candidate/conflict write
    to the existing reconciliation transaction. It never supersedes a record
    directly.
    """
    if incoming.kind != "event" or not isinstance(incoming.payload, EventMemory):
        raise ValueError("claim changes require an event memory")
    if incoming.owner_id != owner_id:
        raise PermissionError("claim change owner does not match the trusted request owner")

    target = store.get_owned(target_memory_id, owner_id=owner_id)
    if target is None:
        raise KeyError(f"Memory '{target_memory_id}' not found")
    if target.kind != "event" or not isinstance(target.payload, EventMemory):
        raise ValueError("claim change target must be an event memory")
    if not is_memory_record_eligible(
        target,
        owner_id=owner_id,
        session_id=session_id,
        project_id=project_id,
        project_scope=project_scope,
    ):
        raise PermissionError("claim change target is not active in the current scope")
    if incoming.subject_id is not None and target.subject_id is not None:
        if incoming.subject_id != target.subject_id:
            raise ValueError("claim change cannot move an event between subjects")

    changed_claims = event_differences(target, incoming)
    outcome = "update" if changed_claims else "duplicate"

    # A correction is another version of the target, so its durable visibility
    # must come from the authoritative target rather than model output or
    # capture-policy inference.
    incoming.subject_id = target.subject_id
    incoming.scope = target.scope
    incoming.session_id = target.session_id
    incoming.project_id = target.project_id
    incoming.project_scope = target.project_scope

    # Extraction retries should address the same proposed version. Repository
    # reconciliation also checks for an existing conflict before inserting it.
    if incoming.source_turn_id:
        incoming.memory_id = str(uuid5(
            NAMESPACE_URL,
            f"raggy:{_CLAIM_CHANGE_COMMAND}:{incoming.source_turn_id}:{target.memory_id}",
        ))

    command_details = {
        **(details or {}),
        "command": _CLAIM_CHANGE_COMMAND,
        "command_version": _CLAIM_CHANGE_VERSION,
        "target_memory_id": target.memory_id,
        "target_subject_id": target.subject_id,
        "changed_claims": changed_claims,
        "matched_identifier_values": matched_identifier_values or [],
        "reconciliation_confidence": confidence,
        "reconciliation_reason": reason,
        "incoming_source_turn_id": incoming.source_turn_id,
        "incoming_event": incoming.payload.model_dump(mode="json"),
    }
    return store.apply_event_reconciliation(
        incoming,
        outcome=outcome,
        existing_memory_id=target.memory_id,
        details=command_details,
        actor_id=actor_id,
    )
