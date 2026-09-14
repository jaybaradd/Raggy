"""Pre-response memory planning remains bounded, scoped, and fail-safe."""
from __future__ import annotations

import asyncio
import tempfile
import types
import unittest
from pathlib import Path

from core.memory.models import EventMemory, IdentifierReference, MemoryRecord
from core.memory.planner import build_memory_context, extract_identifier_references
from core.storage.memory_store import MemoryStore


class _Planner:
    def __init__(self, response: dict | Exception) -> None:
        self.response = response

    async def generate_json(self, prompt: str, schema: dict | None = None) -> dict:
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class MemoryContextPlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(str(Path(self.temp.name) / "memory.sqlite3"))

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _record(memory_id: str, *, project_id: str = "imports-id", reference: str = "EK420",
                confidence: float = 0.95) -> MemoryRecord:
        return MemoryRecord(
            memory_id=memory_id, owner_id="default", scope="project", project_id=project_id,
            project_scope="imports", kind="event", status="active", user_confirmed=True,
            confidence=confidence,
            payload=EventMemory(event_type="operational update", summary=f"Order {reference} arrives Tuesday",
                                entities=[reference], identifier_references=[IdentifierReference(value=reference)]),
        )

    @staticmethod
    def _semantic(*hits: dict):
        return lambda *args, **kwargs: types.SimpleNamespace(context="", memories=list(hits))

    def test_identifier_extraction_is_neutral_and_normalized(self) -> None:
        references = extract_identifier_references("Review AC-42, EK420, EXP-2026-01, and ticket 123456.")
        self.assertEqual(
            [item.normalized_value for item in references], ["ac42", "ek420", "exp202601", "123456"],
        )

    def test_selected_exact_candidate_is_rendered_with_relationship_metadata(self) -> None:
        self.store.upsert(self._record("exact"))
        result = asyncio.run(build_memory_context(
            query="EK-420 has moved to Thursday", owner_id="default", session_id="new-chat",
            project_id="imports-id", project_scope="imports", store=self.store,
            semantic_retriever=self._semantic(), planner_provider=_Planner({
                "selected_memory_ids": ["exact"],
                "relationships": [{"memory_id": "exact", "relation": "updates", "confidence": 0.98}],
                "rationale": "Same operational reference.",
            }),
        ))
        self.assertEqual(result.planner_status, "selected")
        self.assertIn("Order EK420", result.context)
        self.assertEqual(result.memories[0]["selection_relation"], "updates")
        self.assertEqual(result.memories[0]["candidate_source"], "exact_identifier")

    def test_unapproved_semantic_candidate_is_not_injected(self) -> None:
        self.store.upsert(self._record("semantic", reference="OTHER-99"))
        result = asyncio.run(build_memory_context(
            query="What is the latest arrival?", owner_id="default", session_id="new-chat",
            project_id="imports-id", project_scope="imports", store=self.store,
            semantic_retriever=self._semantic({"memory_id": "semantic", "score": 0.93}),
            planner_provider=_Planner({"selected_memory_ids": [], "relationships": []}),
        ))
        self.assertEqual(result.planner_status, "no_selection")
        self.assertEqual(result.memories, [])

    def test_invalid_planner_output_falls_back_to_high_confidence_exact_candidate(self) -> None:
        self.store.upsert(self._record("exact"))
        result = asyncio.run(build_memory_context(
            query="EK420 has changed", owner_id="default", session_id="new-chat",
            project_id="imports-id", project_scope="imports", store=self.store,
            semantic_retriever=self._semantic(), planner_provider=_Planner({
                "selected_memory_ids": ["not-a-candidate"], "relationships": [],
            }),
        ))
        self.assertEqual(result.planner_status, "exact_fallback")
        self.assertEqual([item["memory_id"] for item in result.memories], ["exact"])
        self.assertEqual(result.memories[0]["selection_source"], "exact_fallback")

    def test_semantic_hit_for_another_project_cannot_be_injected(self) -> None:
        self.store.upsert(self._record("other-project", project_id="other-imports-id"))
        result = asyncio.run(build_memory_context(
            query="What is the latest arrival?", owner_id="default", session_id="new-chat",
            project_id="imports-id", project_scope="imports", store=self.store,
            semantic_retriever=self._semantic({"memory_id": "other-project", "score": 0.99}),
            planner_provider=_Planner({"selected_memory_ids": ["other-project"], "relationships": []}),
        ))
        self.assertEqual(result.planner_status, "no_selection")
        self.assertEqual(result.memories, [])


if __name__ == "__main__":
    unittest.main()
