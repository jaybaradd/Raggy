"""Opt-in fresh-Postgres authority test without external LLM dependencies."""
from __future__ import annotations
import importlib.util, os, re, tempfile, unittest, uuid
from pathlib import Path
from core.ingestion.models import AssetRecord, EvidenceSegment
from core.ingestion.parser import SourceLocator
from core.memory.models import MemoryRecord, PreferenceMemory

RUN = os.getenv("RUN_POSTGRES_E2E") == "1"; URL = os.getenv("POSTGRES_DATABASE_URL", "")

@unittest.skipUnless(RUN and URL and importlib.util.find_spec("psycopg"), "set RUN_POSTGRES_E2E=1 and POSTGRES_DATABASE_URL")
class PostgresBackendE2ETests(unittest.TestCase):
    def setUp(self):
        import psycopg
        self.schema = f"raggy_e2e_{uuid.uuid4().hex}"
        self.temp = tempfile.TemporaryDirectory()
        with psycopg.connect(URL, autocommit=True) as c, c.cursor() as cur: cur.execute(f'CREATE SCHEMA "{self.schema}"')

    def tearDown(self):
        import psycopg
        if re.fullmatch(r"raggy_e2e_[0-9a-f]+", self.schema):
            with psycopg.connect(URL, autocommit=True) as c, c.cursor() as cur: cur.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        self.temp.cleanup()

    def _repositories(self):
        from config import Settings
        from db.repository_factory import create_repositories
        return create_repositories(Settings(AUTHORITATIVE_DB_BACKEND="postgres", POSTGRES_DATABASE_URL=URL,
                                            POSTGRES_SCHEMA=self.schema, GRAPH_DB_PATH=str(Path(self.temp.name) / "graph.sqlite3")))

    def test_fresh_authority_persists_sessions_memories_and_evidence_after_restart(self):
        repositories = self._repositories()
        project = repositories.sessions.create_project("Imports")
        session = repositories.sessions.create_session(project_id=project["project_id"])
        repositories.sessions.append_message(session["session_id"], "user", "Shipment AC-42 arrives Friday", trace_id="e2e")
        asset = AssetRecord(asset_id="asset-e2e", filename="arrivals.csv", media_type="text/csv", raw_file_uri="file:///tmp/arrivals.csv", content_hash="asset-e2e")
        repositories.evidence.upsert_asset(asset)
        run = repositories.evidence.start_ingestion("asset-e2e", modality="csv")
        evidence = EvidenceSegment(evidence_id="evidence-e2e", asset_id="asset-e2e", modality="text", representation="text", content="AC-42 arrives Friday", media_uri="file:///tmp/arrivals.csv", source_name="arrivals.csv", locator=SourceLocator(), parser_backend="e2e")
        repositories.evidence.upsert_evidence([evidence]); repositories.evidence.complete_ingestion(run["run_id"], chunk_count=1)
        memory = MemoryRecord(owner_id="default", scope="project", project_id=project["project_id"], project_scope="Imports", kind="preference", status="active", user_confirmed=True, payload=PreferenceMemory(preferred_behavior="Use concise arrival updates"))
        repositories.memories.upsert(memory)

        restarted = self._repositories()
        self.assertEqual(restarted.sessions.get_messages(session["session_id"])[0]["content"], "Shipment AC-42 arrives Friday")
        self.assertEqual(restarted.evidence.get_document_status("asset-e2e")["status"], "done")
        self.assertEqual(restarted.evidence.get_evidence("evidence-e2e").content, "AC-42 arrives Friday")
        self.assertEqual(restarted.memories.get(memory.memory_id).status, "active")
        self.assertEqual(len(restarted.evidence.claim_projection_jobs()), 1)
        self.assertEqual({job["target"] for job in restarted.memories.claim_projection_jobs()}, {"qdrant", "graph"})

if __name__ == "__main__": unittest.main()
