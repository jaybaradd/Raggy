"""Structured, provider-backed candidate memory extraction."""

from __future__ import annotations

import json
import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.llm.base import LLMProvider
from core.memory.models import EventMemory, EntityMemory, KnowledgeAtom, PreferenceMemory, SolutionMemory

EXTRACTION_VERSION = "phase2b-v3"
logger = logging.getLogger(__name__)


class MemoryCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["knowledge", "preference", "solution", "entity", "event"]
    confidence: float = Field(ge=0.0, le=1.0)
    payload: dict
    evidence_refs: list[str] = Field(default_factory=list)


class ExtractionBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidates: list[MemoryCandidate] = Field(default_factory=list, max_length=20)


_PAYLOAD_TYPES = {
    "knowledge": KnowledgeAtom,
    "preference": PreferenceMemory,
    "solution": SolutionMemory,
    "entity": EntityMemory,
    "event": EventMemory,
}


class MemoryExtractor:
    def __init__(self, provider: LLMProvider, *, version: str = EXTRACTION_VERSION) -> None:
        self.provider = provider
        self.version = version

    async def extract_turn(
        self,
        *,
        session_id: str,
        user_content: str,
        assistant_content: str,
        evidence_refs: list[str],
    ) -> ExtractionBatch:
        # Assistant wording may speculate about, summarize, or merely confirm a
        # fact. Durable chat memory is grounded in the user's turn only.
        prompt = self._prompt(session_id, user_content, assistant_content, evidence_refs)
        raw = await self.provider.generate_json(prompt)
        batch = ExtractionBatch.model_validate(raw)
        validated: list[MemoryCandidate] = []
        for candidate in batch.candidates:
            try:
                normalized = _normalize_payload(candidate.kind, candidate.payload)
                payload_type = _PAYLOAD_TYPES[candidate.kind]
                payload = payload_type.model_validate(normalized)
                refs = list(dict.fromkeys([*candidate.evidence_refs, *evidence_refs]))
                validated.append(candidate.model_copy(update={"payload": payload.model_dump(mode="json"), "evidence_refs": refs}))
            except ValidationError as exc:
                logger.warning("Skipping invalid %s memory candidate: %s", candidate.kind, exc.errors())
        return ExtractionBatch(candidates=validated)

    @staticmethod
    def _prompt(session_id: str, user_content: str, assistant_content: str, evidence_refs: list[str]) -> str:
        return f"""You extract durable memory candidates from one completed conversation turn.

Return JSON only. The top-level object must contain exactly one field, `candidates`.
`candidates` must be an array of zero or more objects. Each candidate must contain exactly:
`kind`, `confidence`, `payload`, and `evidence_refs`.

Allowed candidate shapes:
- knowledge payload: `subject` (string), `predicate` (string), `object` (string),
  `qualifiers` (object), `temporal_scope` (string or null).
- preference payload: `preferred_behavior` (string), `applicability_conditions` (array of strings),
  `strength` (number from 0 to 1), `consent` (boolean).
- solution payload: `problem_signature` (string), `environment` (object of strings),
  `steps` (array of strings), `outcome` (string or null), `verification_evidence` (array of strings).
- entity payload: `canonical_name` (string), `entity_type` (string), `aliases` (array of strings),
  `external_ids` (object of strings), `graph_links` (array of strings).
- event payload: `event_type` (descriptive string), `summary` (string), `entities` (array of strings),
  `locations` (array of strings), `temporal_scope` (string or null), `identifier_references` (array),
  `claims` (array). Each identifier reference has `scheme`, `value`, `mention`, and `confidence`.
  Each claim has `attribute`, `value`, and `confidence`. Preserve descriptive labels; do not invent
  a closed event taxonomy.

Representative output examples (do not copy their content):
{{
  "candidates": [
    {{
      "kind": "preference",
      "confidence": 0.92,
      "payload": {{
        "preferred_behavior": "Use concise explanations",
        "applicability_conditions": ["Technical questions"],
        "strength": 0.8,
        "consent": true
      }},
      "evidence_refs": []
    }},
    {{
      "kind": "knowledge",
      "confidence": 0.86,
      "payload": {{
        "subject": "A named system",
        "predicate": "uses",
        "object": "a named component",
        "qualifiers": {{}},
        "temporal_scope": null
      }},
      "evidence_refs": ["evidence-id-from-input"]
    }},
    {{
      "kind": "solution",
      "confidence": 0.78,
      "payload": {{
        "problem_signature": "A reproducible technical problem",
        "environment": {{"platform": "example"}},
        "steps": ["First verified step", "Second verified step"],
        "outcome": "The problem was resolved",
        "verification_evidence": ["evidence-id-from-input"]
      }},
      "evidence_refs": ["evidence-id-from-input"]
    }},
    {{
      "kind": "entity",
      "confidence": 0.9,
      "payload": {{
        "canonical_name": "A named entity",
        "entity_type": "organization",
        "aliases": [],
        "external_ids": {{}},
        "graph_links": []
      }},
      "evidence_refs": ["evidence-id-from-input"]
    }},
    {{
      "kind": "event",
      "confidence": 0.9,
      "payload": {{
        "event_type": "shipment",
        "summary": "A shipment is arriving by air cargo from a named city",
        "entities": ["shipment", "air cargo"],
        "locations": ["a named city"],
        "temporal_scope": "in two days",
        "identifier_references": [],
        "claims": [{{"attribute": "expected_time", "value": "in two days", "confidence": 0.9}}]
      }},
      "evidence_refs": []
    }}
  ]
}}

Rules:
- Extract only explicit statements or claims directly supported by the turn/evidence.
- For conversation memory, extract only durable statements made explicitly by the USER below. The assistant response is intentionally not supplied. Questions, acknowledgements, requests for status, and requests to update memory are not operational-event assertions.
- Document-derived knowledge atoms belong to the document-ingestion pipeline, not this post-chat user-memory extraction job.
- Create one atomic subject-predicate-object claim per knowledge candidate; never put a nested resume, profile, list, or document object in a payload.
- Use `event` only for concrete, user-stated operational events such as shipments, deliveries, meetings, deadlines, reservations, or incidents. Do not use it for a question, a rumour, or a fact found only in documents.
- Use only the exact field names above. Do not wrap a payload inside another kind-named object.
- `evidence_refs` may contain only these IDs: {json.dumps(evidence_refs)}. Use [] when there is no evidence reference.
- Do not output scope or status fields; application policy decides whether a validated user-stated candidate remains session-scoped or becomes active project memory.
- If no valid candidate can be formed, return {{"candidates": []}}.

Session: {session_id}
USER:
{user_content}
"""


def _normalize_payload(kind: str, payload: dict) -> dict:
    """Map common model naming variants to the canonical typed contracts."""
    normalized = dict(payload)
    # Gemini may repeat the candidate kind around the typed payload.
    if isinstance(normalized.get(kind), dict):
        normalized = dict(normalized[kind])
    if kind == "preference" and "preferred_behavior" not in normalized:
        for alias in ("preferred_behaviour", "phrase", "description", "requirement"):
            if normalized.get(alias):
                normalized["preferred_behavior"] = normalized[alias]
                break
    if kind == "preference":
        for alias in ("preferred_behaviour", "phrase", "description", "requirement"):
            normalized.pop(alias, None)
        if isinstance(normalized.get("applicability_conditions"), str):
            normalized["applicability_conditions"] = [normalized["applicability_conditions"]]
        strength = normalized.get("strength")
        if isinstance(strength, str):
            normalized["strength"] = {"low": 0.3, "medium": 0.6, "high": 0.9}.get(
                strength.lower(), 0.5
            )
        consent = normalized.get("consent")
        if isinstance(consent, str):
            normalized["consent"] = consent.lower() in {"true", "yes", "explicit", "confirmed"}
    if kind == "entity" and "canonical_name" not in normalized and normalized.get("name"):
        normalized["canonical_name"] = normalized["name"]
    if kind == "entity":
        normalized.pop("name", None)
    if kind == "event":
        if "identifier_references" not in normalized and isinstance(normalized.get("identifier_refs"), list):
            normalized["identifier_references"] = normalized["identifier_refs"]
        if "claims" not in normalized and isinstance(normalized.get("event_claims"), list):
            normalized["claims"] = normalized["event_claims"]
        normalized.pop("identifier_refs", None)
        normalized.pop("event_claims", None)
    return normalized
