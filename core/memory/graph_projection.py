"""Backend-neutral, conservative graph projection models for durable memories."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from core.memory.models import MemoryRecord

GRAPH_PROJECTION_VERSION = "v1"
GRAPH_NAME = "raggy_memory_projection_v1"
GraphRelationshipType = Literal["related", "contradiction", "superseded_by"]


@dataclass(frozen=True)
class GraphMemoryCandidate:
    """Derived graph-discovery metadata; never authoritative memory content."""

    memory_id: str
    seed_memory_id: str
    relationship_id: str
    relationship_type: GraphRelationshipType


def is_memory_projectable(record: MemoryRecord, *, now: datetime | None = None) -> bool:
    """Return whether a memory may participate in current graph traversal."""
    current_time = now or datetime.now(timezone.utc)
    return (
        record.status == "active"
        and record.user_confirmed
        and record.valid_from <= current_time
        and (record.valid_to is None or record.valid_to > current_time)
    )


@dataclass(frozen=True)
class MemoryGraphNode:
    """A lossless, derived graph representation of one authoritative memory."""

    memory_id: str
    owner_id: str
    scope: str
    session_id: str | None
    project_id: str | None
    project_scope: str | None
    kind: str
    status: str
    confidence: float
    user_confirmed: bool
    valid_from: datetime
    valid_to: datetime | None
    payload_json: str
    source_turn_id: str | None
    is_active: bool

    @classmethod
    def from_record(cls, record: MemoryRecord, *, now: datetime | None = None) -> "MemoryGraphNode":
        return cls(
            memory_id=record.memory_id,
            owner_id=record.owner_id,
            scope=record.scope,
            session_id=record.session_id,
            project_id=record.project_id,
            project_scope=record.project_scope,
            kind=record.kind,
            status=record.status,
            confidence=record.confidence,
            user_confirmed=record.user_confirmed,
            valid_from=record.valid_from,
            valid_to=record.valid_to,
            payload_json=json.dumps(record.payload.model_dump(mode="json"), sort_keys=True),
            source_turn_id=record.source_turn_id,
            is_active=is_memory_projectable(record, now=now),
        )


@dataclass(frozen=True)
class MemoryGraphRelationship:
    """One authoritative relationship with both source records attached."""

    relationship_id: str
    relationship_type: GraphRelationshipType
    from_record: MemoryRecord
    to_record: MemoryRecord
    created_at: datetime

    @property
    def is_projectable(self) -> bool:
        if not _same_graph_partition(self.from_record, self.to_record):
            return False
        if self.relationship_type == "superseded_by":
            return self.from_record.status == "superseded" and is_memory_projectable(self.to_record)
        return is_memory_projectable(self.from_record) and is_memory_projectable(self.to_record)


def _same_graph_partition(first: MemoryRecord, second: MemoryRecord) -> bool:
    """Return whether two records may safely form one projected graph edge.

    The graph is allowed to provide one-hop retrieval expansion.  An edge must
    therefore never bridge project or session visibility boundaries, even if a
    malformed authoritative relationship is inserted by a future caller.
    """
    if first.owner_id != second.owner_id or first.scope != second.scope:
        return False
    if first.scope == "user":
        return True
    if first.scope == "session":
        return first.session_id is not None and first.session_id == second.session_id
    if first.scope == "project":
        if first.project_id is not None or second.project_id is not None:
            return first.project_id is not None and first.project_id == second.project_id
        return first.project_scope is not None and first.project_scope == second.project_scope
    return False
