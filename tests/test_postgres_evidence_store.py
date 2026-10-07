"""Opt-in PostgreSQL tests for immutable assets, evidence, and ingestion state."""
import importlib.util, os, re, unittest, uuid
from core.ingestion.models import AssetRecord, EvidenceSegment
from core.ingestion.parser import SourceLocator

RUN = os.getenv("RUN_POSTGRES_INTEGRATION_TESTS") == "1"; URL = os.getenv("POSTGRES_DATABASE_URL", "")

@unittest.skipUnless(RUN and URL and importlib.util.find_spec("psycopg"), "set Postgres integration environment")
class PostgresEvidenceRepositoryTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from db.postgres_evidence_store import PostgresEvidenceRepository
        self.schema = f"raggy_test_{uuid.uuid4().hex}"
        with psycopg.connect(URL, autocommit=True) as c, c.cursor() as cur: cur.execute(f'CREATE SCHEMA "{self.schema}"')
        self.store = PostgresEvidenceRepository(URL, schema=self.schema, worker_id="a")
        self.asset = AssetRecord(asset_id="asset-1", filename="plan.pdf", media_type="application/pdf", raw_file_uri="file:///tmp/plan.pdf", content_hash="hash-1")
        self.binding_id = self.store.upsert_asset(
            self.asset, owner_id="owner-a", scope="project", session_id=None, project_id="project-1",
        )

    def tearDown(self):
        import psycopg
        if re.fullmatch(r"raggy_test_[0-9a-f]+", self.schema):
            with psycopg.connect(URL, autocommit=True) as c, c.cursor() as cur: cur.execute(f'DROP SCHEMA "{self.schema}" CASCADE')

    def _evidence(self, content="Arrival on Friday"):
        return EvidenceSegment(evidence_id="evidence-1", asset_id="asset-1", modality="text", representation="text", content=content, media_uri="file:///tmp/plan.pdf", source_name="plan.pdf", locator=SourceLocator(page=1), parser_backend="test", retrieval_metadata={"context_prefix":"Schedule"})

    def test_immutable_evidence_status_restart_and_outbox(self):
        run = self.store.start_ingestion("asset-1", binding_id=self.binding_id, modality="pdf")
        self.store.upsert_evidence([self._evidence()], binding_id=self.binding_id); self.store.complete_ingestion(run["run_id"], chunk_count=1)
        self.assertEqual(self.store.get_document_status(self.binding_id, owner_id="owner-a", session_id="s", project_id="project-1")["status"], "done")
        rows = self.store.revalidate_evidence([("evidence-1", self.binding_id)], owner_id="owner-a", session_id="s", project_id="project-1")
        self.assertEqual(rows[("evidence-1", self.binding_id)]["content"], "Arrival on Friday")
        self.store.upsert_evidence([self._evidence()], binding_id=self.binding_id)
        with self.assertRaises(ValueError): self.store.upsert_evidence([self._evidence("Arrival on Monday")], binding_id=self.binding_id)
        jobs = self.store.claim_projection_jobs(); self.assertEqual(len(jobs), 1)
        self.store.complete_projection_job(jobs[0]["job_id"])
        from db.postgres_evidence_store import PostgresEvidenceRepository
        restarted = PostgresEvidenceRepository(URL, schema=self.schema)
        self.assertEqual(restarted.get_document_status(self.binding_id, owner_id="owner-a", session_id="s", project_id="project-1")["chunk_count"], 1)

    def test_owner_and_project_access_is_scoped(self):
        self.store.upsert_evidence([self._evidence()], binding_id=self.binding_id)
        self.assertEqual(self.store.revalidate_evidence([("evidence-1", self.binding_id)], owner_id="owner-b", session_id="s", project_id="project-1"), {})
        self.assertEqual(self.store.revalidate_evidence([("evidence-1", self.binding_id)], owner_id="owner-a", session_id="s", project_id="other"), {})

    def test_same_asset_has_independent_project_and_session_bindings(self):
        session_binding = self.store.upsert_asset(
            self.asset, owner_id="owner-a", scope="session", session_id="session-1", project_id=None,
        )
        self.assertNotEqual(session_binding, self.binding_id)
        self.store.upsert_evidence([self._evidence()], binding_id=self.binding_id)
        self.assertEqual(self.store.enqueue_asset_projections(session_binding), 1)
        project_rows = self.store.revalidate_evidence(
            [("evidence-1", self.binding_id)], owner_id="owner-a", session_id="other", project_id="project-1",
        )
        session_rows = self.store.revalidate_evidence(
            [("evidence-1", session_binding)], owner_id="owner-a", session_id="session-1", project_id=None,
        )
        self.assertIn(("evidence-1", self.binding_id), project_rows)
        self.assertIn(("evidence-1", session_binding), session_rows)
        self.assertEqual(self.store.revalidate_evidence(
            [("evidence-1", session_binding)], owner_id="owner-a", session_id="session-2", project_id=None,
        ), {})

if __name__ == "__main__": unittest.main()
