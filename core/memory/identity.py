"""Stable duplicate fingerprints and generic event comparison helpers."""

from __future__ import annotations

import hashlib
import re

from core.memory.models import EventMemory, MemoryRecord


def event_identity_key(record: MemoryRecord) -> str:
    """Return an exact duplicate fingerprint, not a real-world identity key."""
    assert isinstance(record.payload, EventMemory)
    payload = record.payload
    identifiers = sorted(reference.normalized_value for reference in payload.identifier_references)
    if not identifiers:
        identifiers = sorted(_stable_identifiers(payload.entities))
    claims = sorted((claim.attribute.casefold().strip(), claim.normalized_value or "") for claim in payload.claims)
    if not claims:
        claims = [("temporal_scope", _normalise(payload.temporal_scope or "")),
                  ("locations", "|".join(sorted(_normalise_many(payload.locations))))]
    parts = (record.owner_id, record.scope, record.session_id if record.scope == "session" else "",
             record.project_id or record.project_scope or "", "|".join(identifiers),
             "|".join(f"{attribute}={value}" for attribute, value in claims))
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def events_are_comparable(existing: MemoryRecord, incoming: MemoryRecord) -> bool:
    """Return whether two events share stable scope and identifier anchors."""
    if not isinstance(existing.payload, EventMemory) or not isinstance(incoming.payload, EventMemory):
        return False
    if (existing.owner_id, existing.scope, existing.project_id or existing.project_scope) != (
        incoming.owner_id, incoming.scope, incoming.project_id or incoming.project_scope):
        return False
    if existing.scope == "session" and existing.session_id != incoming.session_id:
        return False
    return bool(_event_identifiers(existing.payload) & _event_identifiers(incoming.payload))


def event_comparison_key(record: MemoryRecord) -> tuple[str, ...] | None:
    """Return an inspectable candidate-discovery key without event taxonomy."""
    assert isinstance(record.payload, EventMemory)
    identifiers = tuple(sorted(_event_identifiers(record.payload)))
    if not identifiers:
        return None
    return (record.owner_id, record.scope, record.session_id if record.scope == "session" else "",
            record.project_id or record.project_scope or "", *identifiers)


def event_differences(existing: MemoryRecord, incoming: MemoryRecord) -> dict[str, dict[str, str | None]]:
    """Compare explicit claims, with a compatibility fallback for old payloads."""
    assert isinstance(existing.payload, EventMemory) and isinstance(incoming.payload, EventMemory)
    old_claims, new_claims = _claims(existing.payload), _claims(incoming.payload)
    return {attribute: {"existing": old_claims.get(attribute), "incoming": new_claims.get(attribute)}
            for attribute in sorted(set(old_claims) | set(new_claims))
            if old_claims.get(attribute) != new_claims.get(attribute)}


def _claims(event: EventMemory) -> dict[str, str]:
    claims = {claim.attribute.casefold().strip(): claim.value for claim in event.claims}
    if claims:
        return claims
    if event.temporal_scope:
        claims["temporal_scope"] = event.temporal_scope
    if event.locations:
        claims["primary_location"] = event.locations[0]
    return claims


def _event_identifiers(event: EventMemory) -> set[str]:
    return {reference.normalized_value for reference in event.identifier_references} or _stable_identifiers(event.entities)


def _normalise(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", value.casefold())).strip()


def _normalise_many(values: list[str]) -> set[str]:
    return {normalised for value in values if (normalised := _normalise(value))}


def _stable_identifiers(values: list[str]) -> set[str]:
    identifiers: set[str] = set()
    for value in values:
        for match in re.findall(r"\b(?:[a-z]+[-_ ]?\d+[a-z\d]*|\d+[a-z]+|\d{2,})\b", value.casefold()):
            identifiers.add(re.sub(r"[^a-z0-9]", "", match))
    return identifiers


def legacy_identifier_values(event: EventMemory) -> set[str]:
    """Return identifier-like values from pre-Slice-1 event entity strings."""
    return _stable_identifiers(event.entities)
