"""Deterministic identity helpers for automatically captured event memories."""

from __future__ import annotations

import hashlib
import re

from core.memory.event_types import canonical_event_type
from core.memory.models import EventMemory, MemoryRecord


def event_identity_key(record: MemoryRecord) -> str:
    """Return a stable key for an exact event claim, independent of its summary."""
    assert isinstance(record.payload, EventMemory)
    payload = record.payload
    parts = (
        record.owner_id,
        record.scope,
        record.session_id if record.scope == "session" else "",
        record.project_id or record.project_scope or "",
        canonical_event_type(payload.event_type),
        "|".join(sorted(_identity_entities(payload.entities))),
        _normalise(payload.locations[0]) if payload.locations else "",
        _normalise(payload.temporal_scope or ""),
    )
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def events_are_comparable(existing: MemoryRecord, incoming: MemoryRecord) -> bool:
    """Match comparable events by scope, canonical type, and stable ID overlap."""
    if not isinstance(existing.payload, EventMemory) or not isinstance(incoming.payload, EventMemory):
        return False
    if (existing.owner_id, existing.scope, existing.project_id or existing.project_scope) != (
        incoming.owner_id, incoming.scope, incoming.project_id or incoming.project_scope,
    ):
        return False
    if existing.scope == "session" and existing.session_id != incoming.session_id:
        return False
    if canonical_event_type(existing.payload.event_type) != canonical_event_type(incoming.payload.event_type):
        return False
    return bool(_stable_identifiers(existing.payload.entities) & _stable_identifiers(incoming.payload.entities))


def event_comparison_key(record: MemoryRecord) -> tuple[str, ...] | None:
    """Return an inspectable key for a single event's stable identity anchors."""
    assert isinstance(record.payload, EventMemory)
    entities = tuple(sorted(_stable_identifiers(record.payload.entities)))
    if not entities:
        return None
    return (
        record.owner_id, record.scope, record.session_id if record.scope == "session" else "", record.project_id or record.project_scope or "",
        canonical_event_type(record.payload.event_type), *entities,
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
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", value.casefold())).strip()


def _normalise_many(values: list[str]) -> set[str]:
    return {normalised for value in values if (normalised := _normalise(value))}


def _stable_identifiers(values: list[str]) -> set[str]:
    """Keep identifier-like entities such as AC-42, invoice numbers, and IDs."""
    identifiers: set[str] = set()
    for value in values:
        for match in re.findall(r"\b(?:[a-z]+[-_ ]?\d+[a-z\d]*|\d+[a-z]+|\d{2,})\b", value.casefold()):
            identifiers.add(re.sub(r"[^a-z0-9]", "", match))
    return identifiers


def _identity_entities(values: list[str]) -> set[str]:
    return _stable_identifiers(values) or _normalise_many(values)
