"""Pure scope-selection rules shared by memory retrieval implementations."""
from __future__ import annotations

from dataclasses import dataclass


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

