"""Bounded decomposition preserves a single final retrieval ranking."""


import importlib
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

from config import settings
from core.retrieval import decomposition


def _engine_without_ml_runtime():
    """Import the orchestration module without loading local embedding models."""
    stubs = {
        "core.embeddings": types.SimpleNamespace(embedder=object()),
        "core.retrieval.reranker": types.SimpleNamespace(
            reranker=types.SimpleNamespace(rerank=lambda **_kwargs: []),
        ),
        "core.storage.qdrant_store": types.SimpleNamespace(qdrant_store=object()),
    }
    previous = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    sys.modules.pop("core.retrieval.engine", None)
    try:
        return importlib.import_module("core.retrieval.engine")
    finally:
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


engine = _engine_without_ml_runtime()


class QueryDecompositionTests(unittest.IsolatedAsyncioTestCase):
    async def test_postgres_revalidation_replaces_qdrant_content_and_rejects_stale_hits(self) -> None:
        class Evidence:
            def revalidate_evidence(self, references, **scope):
                self.references, self.scope = references, scope
                return {("kept", 7): {
                    "evidence_id": "kept", "binding_id": 7, "asset_id": "asset",
                    "modality": "text", "representation": "text", "content": "authoritative",
                    "source_name": "source.pdf", "media_uri": "file:///source.pdf",
                    "locator_json": {"page": 3},
                }}

        evidence = Evidence()
        candidates = [
            {"evidence_id": "kept", "binding_id": 7, "content": "stale", "score": 0.8},
            {"evidence_id": "rejected", "binding_id": 8, "content": "unauthorized", "score": 0.9},
        ]
        with patch.object(engine, "repositories", types.SimpleNamespace(evidence=evidence)):
            result = engine._revalidate_candidates(
                candidates, owner_id="owner", session_id="session", project_id="project",
            )
        self.assertEqual([item["evidence_id"] for item in result], ["kept"])
        self.assertEqual(result[0]["content"], "authoritative")
        self.assertEqual(result[0]["page"], 3)
        self.assertEqual(evidence.references, [("kept", 7), ("rejected", 8)])

    async def test_compound_question_uses_validated_model_subqueries(self) -> None:
        provider = type("Provider", (), {
            "generate_json": AsyncMock(return_value={
                "subqueries": ["Find the migration design", "Find its rollout risks"],
            }),
        })()
        with patch.object(settings, "query_decomposition_enabled", True):
            result = await decomposition.decompose_query(
                "Compare the migration design with rollout risks and then list mitigations for the release?",
                provider,
            )
        self.assertEqual(result, ["Find the migration design", "Find its rollout risks"])

    async def test_invalid_or_disabled_plan_falls_back_to_original_question(self) -> None:
        provider = type("Provider", (), {"generate_json": AsyncMock(return_value={"subqueries": ["same"]})})()
        query = "Compare the migration design with rollout risks and then list mitigations for the release?"
        with patch.object(settings, "query_decomposition_enabled", True):
            self.assertEqual(await decomposition.decompose_query(query, provider), [query])
        self.assertEqual(await decomposition.decompose_query(query, provider), [query])

    async def test_merged_candidates_are_reranked_against_original_question(self) -> None:
        provider = object()
        first = {"evidence_id": "a", "binding_id": 1, "content": "first"}
        duplicate = {"evidence_id": "a", "binding_id": 1, "content": "duplicate"}
        second = {"evidence_id": "b", "binding_id": 1, "content": "second"}
        with (
            patch("core.retrieval.decomposition.decompose_query", AsyncMock(return_value=["one", "two"])),
            patch("core.retrieval.engine._retrieve_candidates", side_effect=[[first, duplicate], [second]]),
            patch("core.retrieval.engine._revalidate_candidates", side_effect=lambda items, **_: items),
            patch.object(engine.reranker, "rerank", return_value=[second, first]) as rerank,
        ):
            result = await engine.retrieve_with_decomposition(
                "original question", provider=provider, owner_id="owner", session_id="session", project_id=None,
            )

        self.assertEqual(result.chunks, [second, first])
        self.assertEqual(result.context, "[Source 1]\nsecond\n\n[Source 2]\nfirst")
        self.assertEqual(rerank.call_args.kwargs["query"], "original question")
        self.assertEqual(rerank.call_args.kwargs["chunks"], [first, second])


if __name__ == "__main__":
    unittest.main()
