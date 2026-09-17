"""Bounded decomposition preserves a single final retrieval ranking."""

from __future__ import annotations

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
        first = {"evidence_id": "a", "content": "first"}
        duplicate = {"evidence_id": "a", "content": "duplicate"}
        second = {"evidence_id": "b", "content": "second"}
        with (
            patch("core.retrieval.decomposition.decompose_query", AsyncMock(return_value=["one", "two"])),
            patch("core.retrieval.engine._retrieve_candidates", side_effect=[[first, duplicate], [second]]),
            patch.object(engine.reranker, "rerank", return_value=[second, first]) as rerank,
        ):
            result = await engine.retrieve_with_decomposition("original question", provider=provider)

        self.assertEqual(result.chunks, [second, first])
        self.assertEqual(result.context, "[Source 1]\nsecond\n\n[Source 2]\nfirst")
        self.assertEqual(rerank.call_args.kwargs["query"], "original question")
        self.assertEqual(rerank.call_args.kwargs["chunks"], [first, second])


if __name__ == "__main__":
    unittest.main()
