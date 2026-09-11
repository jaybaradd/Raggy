"""Deterministic identity helpers for automatically captured event memories."""

from __future__ import annotations

import hashlib
import re

from core.memory.models import EventMemory, MemoryRecord


def event_identity_key(record: MemoryRecord) -> str:
    """Return a stable key for an exact event claim, independent of its summary."""
    assert isinstance(record.payload, EventMemory)
    payload = record.payload
    parts = (
        record.owner_id,
        record.scope,
        record.session_id or "",
        record.project_scope or "",
        _normalise(payload.event_type),
        "|".join(sorted(_normalise_many(payload.entities))),
        _normalise(payload.locations[0]) if payload.locations else "",
        _normalise(payload.temporal_scope or ""),
    )
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def event_comparison_key(record: MemoryRecord) -> tuple[str, ...] | None:
    """Return the conservative identity shared by potentially conflicting events.

    A conflict requires a named entity. Generic shipments should not be treated
    as contradictory merely because they have the same event type.
    """
    assert isinstance(record.payload, EventMemory)
    entities = tuple(sorted(_normalise_many(record.payload.entities)))
    if not entities:
        return None
    return (
        record.owner_id, record.scope, record.session_id or "", record.project_scope or "",
        _normalise(record.payload.event_type), *entities,
    )


def event_differences(existing: MemoryRecord, incoming: MemoryRecord) -> dict[str, dict[str, str | None]]:
    """Describe material event fields without treating a summary rewrite as a conflict."""
    assert isinstance(existing.payload, EventMemory)
    assert isinstance(incoming.payload, EventMemory)
    differences: dict[str, dict[str, str | None]] = {}
    for field, old, new in (
        ("temporal_scope", existing.payload.temporal_scope, incoming.payload.temporal_scope),
        ("primary_location", existing.payload.locations[0] if existing.payload.locations else None,
         incoming.payload.locations[0] if incoming.payload.locations else None),
    ):
        if _normalise(old or "") != _normalise(new or ""):
            differences[field] = {"existing": old, "incoming": new}
    return differences


def _normalise(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", value.casefold())).strip()


def _normalise_many(values: list[str]) -> set[str]:
    return {normalised for value in values if (normalised := _normalise(value))}
