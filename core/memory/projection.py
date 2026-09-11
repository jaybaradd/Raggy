"""Deterministic memory-to-text projection for semantic retrieval."""

from __future__ import annotations

from core.memory.models import MemoryRecord


def memory_text(record: MemoryRecord) -> str:
    payload = record.payload
    if record.kind == "knowledge":
        return f"{payload.subject} {payload.predicate} {payload.object}."
    if record.kind == "preference":
        conditions = ", ".join(payload.applicability_conditions)
        suffix = f" Applicable when: {conditions}." if conditions else ""
        return f"The user prefers {payload.preferred_behavior}.{suffix}"
    if record.kind == "solution":
        steps = "; ".join(payload.steps)
        environment = ", ".join(f"{key}={value}" for key, value in payload.environment.items())
        return f"Problem: {payload.problem_signature}. Environment: {environment}. Steps: {steps}. Outcome: {payload.outcome or 'not specified'}."
    if record.kind == "event":
        entities = ", ".join(payload.entities)
        locations = ", ".join(payload.locations)
        temporal = f" Timing: {payload.temporal_scope}." if payload.temporal_scope else ""
        return f"Project event ({payload.event_type}): {payload.summary}. Entities: {entities}. Locations: {locations}.{temporal}"
    aliases = ", ".join(payload.aliases)
    return f"Entity: {payload.canonical_name}. Type: {payload.entity_type}. Aliases: {aliases}."
