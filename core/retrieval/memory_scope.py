"""Pure scope-selection rules shared by memory retrieval implementations."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class MemoryScopeConstraint:
    """A visibility scope plus its optional ownership/location constraint."""

    visibility_scope: str
    constraint_field: str | None
    constraint_value: str


def applicable_memory_scopes(*, owner_id: str, session_id: str,
                             project_id: str | None, project_scope: str | None) -> list[MemoryScopeConstraint]:
    """Return all memory visibility domains available to the current chat.

    ``project_id`` and ``project_scope`` select *which* project records are
    visible; they are not themselves values of a record's ``scope`` field.
    """
    constraints = [
        MemoryScopeConstraint("session", "session_id", session_id),
        MemoryScopeConstraint("user", None, owner_id),
    ]
    if project_id:
        constraints.append(MemoryScopeConstraint("project", "project_id", project_id))
    if project_scope:
        constraints.append(MemoryScopeConstraint("project", "project_scope", project_scope))
    return constraints


def is_memory_record_eligible(record: Any, *, owner_id: str, session_id: str,
                              project_id: str | None, project_scope: str | None,
                              now: datetime | None = None) -> bool:
    """Authoritatively re-check whether a record can enter this chat's prompt.

    Qdrant is a projection and can be briefly stale. The planner therefore
    performs this check against the source repository before injecting a
    semantic hit.
    """
    current_time = now or datetime.now(timezone.utc)
    if record.owner_id != owner_id or record.status != "active" or not record.user_confirmed:
        return False
    if record.valid_from > current_time or (record.valid_to is not None and record.valid_to <= current_time):
        return False
    if record.scope == "session":
        return record.session_id == session_id
    if record.scope == "user":
        return True
    if record.scope != "project":
        return False
    if project_id and record.project_id == project_id:
        return True
    return record.project_id is None and bool(project_scope) and record.project_scope == project_scope
