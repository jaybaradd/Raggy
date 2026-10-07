"""Evidence outbox tests independent of a running Qdrant server."""

import unittest

from core.evidence.projections import sync_pending_evidence_projections
from core.ingestion.models import EvidenceSegment
from core.ingestion.parser import SourceLocator


class _FakeQdrant:
    def __init__(self) -> None:
        self.writes: list[tuple[list, list[list[float]]]] = []

    def upsert(self, chunks, vectors, *, binding) -> None:
        self.writes.append((chunks, vectors, binding))


class _FakeEmbedder:
    dimension = 1

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(text))] for text in texts]


class _FakeStore:
    def __init__(self) -> None:
        self.segments = {}
        self.jobs = []

    def upsert_evidence(self, segments, *, binding_id: int) -> None:
        for segment in segments:
            self.segments[segment.evidence_id] = segment
            self.jobs.append({"job_id": len(self.jobs) + 1, "evidence_id": segment.evidence_id,
                              "binding_id": binding_id, "status": "pending"})

    def claim_projection_jobs(self, *, limit=50, binding_id=None):
        rows = [job for job in self.jobs if job["status"] == "pending"
                and (binding_id is None or job["binding_id"] == binding_id)][:limit]
        for row in rows:
            row["status"] = "running"
        return [dict(row) for row in rows]

    def get_projection_input(self, evidence_id, *, binding_id):
        segment = self.segments.get(evidence_id)
        if not segment:
            return None
        return {"segment": segment, "binding_id": binding_id, "owner_id": "owner",
                "scope": "session", "session_id": "session", "project_id": None}

    def complete_projection_job(self, job_id):
        next(job for job in self.jobs if job["job_id"] == job_id)["status"] = "completed"

    def fail_projection_job(self, job_id, _error):
        next(job for job in self.jobs if job["job_id"] == job_id)["status"] = "failed"

    def requeue_failed_projections(self):
        failed = [job for job in self.jobs if job["status"] == "failed"]
        for job in failed:
            job["status"] = "pending"
        return len(failed)


class EvidenceProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = _FakeStore()

    def test_persisted_evidence_is_projected_from_the_durable_outbox(self) -> None:
        self.store.upsert_evidence([EvidenceSegment(
            evidence_id="evidence-1", asset_id="asset-1", modality="text", representation="text",
            content="Shipment AC-42 arrives Friday", media_uri="file:///tmp/arrivals.csv",
            source_name="arrivals.csv", locator=SourceLocator(page=1), parser_backend="test",
            retrieval_metadata={"context_prefix": "Arrival schedule", "embedding_model": "test-model"},
        )], binding_id=7)
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
        )], binding_id=8)

        class BrokenQdrant:
            def upsert(self, *_args, **_kwargs) -> None:
                raise RuntimeError("Qdrant unavailable")

        result = sync_pending_evidence_projections(
            store=self.store, qdrant=BrokenQdrant(), embedder_client=_FakeEmbedder()
        )
        self.assertEqual(result["failed"], 1)
        self.assertEqual(self.store.requeue_failed_projections(), 1)
