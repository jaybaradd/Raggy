"""Phase 2A memory contracts: typed claims with scope and provenance."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Union
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

MemoryKind = Literal["knowledge", "preference", "solution", "entity", "event"]
MemoryScope = Literal["session", "project", "user", "organization"]
MemoryStatus = Literal["candidate", "active", "superseded", "rejected", "expired"]


class KnowledgeAtom(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str = Field(min_length=1, max_length=2_000)
    predicate: str = Field(min_length=1, max_length=500)
    object: str = Field(min_length=1, max_length=4_000)
    qualifiers: dict[str, str] = Field(default_factory=dict)
    temporal_scope: str | None = Field(default=None, max_length=500)


class PreferenceMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preferred_behavior: str = Field(min_length=1, max_length=4_000)
    applicability_conditions: list[str] = Field(default_factory=list)
    strength: float = Field(default=0.5, ge=0.0, le=1.0)
    consent: bool = False


class SolutionMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    problem_signature: str = Field(min_length=1, max_length=4_000)
    environment: dict[str, str] = Field(default_factory=dict)
    steps: list[str] = Field(min_length=1)
    outcome: str | None = Field(default=None, max_length=4_000)
    verification_evidence: list[str] = Field(default_factory=list)


class EntityMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    canonical_name: str = Field(min_length=1, max_length=1_000)
    entity_type: str = Field(min_length=1, max_length=500)
    aliases: list[str] = Field(default_factory=list)
    external_ids: dict[str, str] = Field(default_factory=dict)
    graph_links: list[str] = Field(default_factory=list)


class EventMemory(BaseModel):
    """A user-stated, time-bound project event such as a shipment or deadline."""

    model_config = ConfigDict(extra="forbid")
    event_type: str = Field(min_length=1, max_length=500)
    summary: str = Field(min_length=1, max_length=4_000)
    entities: list[str] = Field(default_factory=list, max_length=20)
    locations: list[str] = Field(default_factory=list, max_length=20)
    temporal_scope: str | None = Field(default=None, max_length=500)


MemoryPayload = Union[KnowledgeAtom, PreferenceMemory, SolutionMemory, EntityMemory, EventMemory]


class MemoryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    memory_id: str = Field(default_factory=lambda: str(uuid4()))
    owner_id: str = "default"
    scope: MemoryScope = "session"
    session_id: str | None = None
    project_scope: str | None = None
    kind: MemoryKind
    status: MemoryStatus = "candidate"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    user_confirmed: bool = False
    evidence_refs: list[str] = Field(default_factory=list)
    source_turn_id: str | None = None
    extraction_model: str = ""
    extraction_version: str = ""
    valid_from: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    valid_to: datetime | None = None
    superseded_by: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    payload: MemoryPayload

    @model_validator(mode="after")
    def validate_kind_payload(self) -> "MemoryRecord":
        expected = {"knowledge": KnowledgeAtom, "preference": PreferenceMemory,
                    "solution": SolutionMemory, "entity": EntityMemory,
                    "event": EventMemory}[self.kind]
        if not isinstance(self.payload, expected):
            raise ValueError(f"kind '{self.kind}' requires {expected.__name__} payload")
        if self.scope == "session" and not self.session_id:
            raise ValueError("session-scoped memory requires session_id")
        if self.scope == "project" and not self.project_scope:
            raise ValueError("project-scoped memory requires project_scope")
        return self
