"""Evidence outbox tests independent of a running Qdrant server."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.evidence.projections import sync_pending_evidence_projections
from core.ingestion.models import EvidenceSegment
from core.ingestion.parser import SourceLocator
from core.storage.evidence_store import EvidenceStore


class _FakeQdrant:
    def __init__(self) -> None:
        self.writes: list[tuple[list, list[list[float]]]] = []

    def upsert(self, chunks, vectors) -> None:
        self.writes.append((chunks, vectors))


class _FakeEmbedder:
    dimension = 1

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(text))] for text in texts]


class EvidenceProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = EvidenceStore(str(Path(self.temp.name) / "evidence.sqlite3"))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_persisted_evidence_is_projected_from_the_durable_outbox(self) -> None:
        self.store.upsert_evidence([EvidenceSegment(
            evidence_id="evidence-1", asset_id="asset-1", modality="text", representation="text",
            content="Shipment AC-42 arrives Friday", media_uri="file:///tmp/arrivals.csv",
            source_name="arrivals.csv", locator=SourceLocator(page=1), parser_backend="test",
            retrieval_metadata={"context_prefix": "Arrival schedule", "embedding_model": "test-model"},
        )])
        qdrant = _FakeQdrant()
        result = sync_pending_evidence_projections(
            store=self.store, qdrant=qdrant, embedder_client=_FakeEmbedder()
        )

        self.assertEqual(result, {"completed": 1, "failed": 0, "skipped": 0})
        self.assertEqual(len(qdrant.writes), 1)
        chunk, vector = qdrant.writes[0][0][0], qdrant.writes[0][1][0]
        self.assertEqual(chunk.evidence_id, "evidence-1")
        self.assertEqual(chunk.context_prefix, "Arrival schedule")
        self.assertEqual(vector, [29.0])
        self.assertEqual(self.store.claim_projection_jobs(), [])

    def test_failed_projection_can_be_requeued_without_rewriting_evidence(self) -> None:
        self.store.upsert_evidence([EvidenceSegment(
            evidence_id="evidence-2", asset_id="asset-2", modality="text", representation="text",
            content="Retry me", locator=SourceLocator(), parser_backend="test",
        )])

        class BrokenQdrant:
            def upsert(self, *_args) -> None:
                raise RuntimeError("Qdrant unavailable")

        result = sync_pending_evidence_projections(
            store=self.store, qdrant=BrokenQdrant(), embedder_client=_FakeEmbedder()
        )
        self.assertEqual(result["failed"], 1)
        self.assertEqual(self.store.requeue_failed_projections(), 1)
