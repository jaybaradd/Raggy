"""Opt-in verification of the evidence worker against a fresh Qdrant collection.

Run with ``RUN_QDRANT_E2E=1``.  The test uses Qdrant's isolated in-memory
client, but the production QdrantStore implementation and payload format.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(os.getenv("RUN_QDRANT_E2E") == "1", "set RUN_QDRANT_E2E=1")
class QdrantEvidenceProjectionE2ETests(unittest.TestCase):
    def test_worker_writes_the_expected_point_to_a_fresh_collection(self) -> None:
        from qdrant_client import QdrantClient
        from core.evidence.projections import sync_pending_evidence_projections
        from core.ingestion.models import EvidenceSegment
        from core.ingestion.parser import SourceLocator
        from core.storage.evidence_store import EvidenceStore
        from core.storage.qdrant_store import QdrantStore

        class Embedder:
            dimension = 1
            def encode(self, texts): return [[float(len(text))] for text in texts]

        class Values:
            def __init__(self, values): self._values = values
            def tolist(self): return self._values

        class SparseItem:
            indices, values = Values([1]), Values([1.0])

        class Bm25:
            def embed(self, _texts): return [SparseItem()]

        with tempfile.TemporaryDirectory() as directory:
            store = EvidenceStore(str(Path(directory) / "evidence.sqlite3"))
            store.upsert_evidence([EvidenceSegment(
                evidence_id="evidence-qdrant", asset_id="asset-qdrant", modality="text",
                representation="text", content="Fresh vector check", locator=SourceLocator(),
                parser_backend="test",
            )])
            client = QdrantClient(":memory:")
            qdrant = QdrantStore(
                "evidence_projection_e2e", client=client, bm25_encoder=Bm25(),
                embedding_backend=Embedder(),
            )
            result = sync_pending_evidence_projections(
                store=store, qdrant=qdrant, embedder_client=Embedder()
            )
            points, _ = client.scroll(collection_name="evidence_projection_e2e", limit=10)

        self.assertEqual(result, {"completed": 1, "failed": 0, "skipped": 0})
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].payload["evidence_id"], "evidence-qdrant")
        self.assertEqual(points[0].payload["content"], "Fresh vector check")

