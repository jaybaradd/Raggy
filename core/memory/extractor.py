"""Structured, provider-backed candidate memory extraction."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from core.llm.base import LLMProvider
from core.memory.models import EntityMemory, KnowledgeAtom, PreferenceMemory, SolutionMemory

EXTRACTION_VERSION = "phase2b-v1"


class MemoryCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["knowledge", "preference", "solution", "entity"]
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
        prompt = self._prompt(session_id, user_content, assistant_content, evidence_refs)
        raw = await self.provider.generate_json(prompt, ExtractionBatch.model_json_schema())
        batch = ExtractionBatch.model_validate(raw)
        validated: list[MemoryCandidate] = []
        for candidate in batch.candidates:
            # Parse each typed payload now; invalid model output is rejected
            # before it reaches durable memory storage.
            payload_type = _PAYLOAD_TYPES[candidate.kind]
            payload = payload_type.model_validate(candidate.payload)
            refs = list(dict.fromkeys([*candidate.evidence_refs, *evidence_refs]))
            validated.append(candidate.model_copy(update={"payload": payload.model_dump(mode="json"), "evidence_refs": refs}))
        return ExtractionBatch(candidates=validated)

    @staticmethod
    def _prompt(session_id: str, user_content: str, assistant_content: str, evidence_refs: list[str]) -> str:
        return f"""Extract only explicit, useful durable memory candidates from this completed turn.
Return JSON matching the supplied schema. Do not infer personal facts. Prefer no candidates over speculation.
Allowed kinds: knowledge (source-backed fact), preference (stated user preference), solution (problem and verified result), entity.
All candidates begin as session-scoped and unconfirmed. Evidence references may only use these IDs: {json.dumps(evidence_refs)}.
Session: {session_id}
USER:\n{user_content}\nASSISTANT:\n{assistant_content}"""
