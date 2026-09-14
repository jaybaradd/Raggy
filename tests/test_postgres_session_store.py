"""Opt-in integration coverage for the PostgreSQL session repository.

Run against the local Podman service with RUN_POSTGRES_INTEGRATION_TESTS=1.
"""
from __future__ import annotations

import importlib.util
import os
import re
import unittest
import uuid


RUN_INTEGRATION = os.getenv("RUN_POSTGRES_INTEGRATION_TESTS") == "1"
POSTGRES_URL = os.getenv("POSTGRES_DATABASE_URL", "")


@unittest.skipUnless(RUN_INTEGRATION and POSTGRES_URL and importlib.util.find_spec("psycopg"),
                     "set RUN_POSTGRES_INTEGRATION_TESTS=1 and POSTGRES_DATABASE_URL to run")
class PostgresSessionRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        import psycopg
        from db.postgres_session_store import PostgresSessionRepository
        self.schema = f"raggy_test_{uuid.uuid4().hex}"
        with psycopg.connect(POSTGRES_URL, autocommit=True) as connection, connection.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{self.schema}"')
        self.store = PostgresSessionRepository(POSTGRES_URL, schema=self.schema)

    def tearDown(self) -> None:
        import psycopg
        if re.fullmatch(r"raggy_test_[0-9a-f]+", self.schema):
            with psycopg.connect(POSTGRES_URL, autocommit=True) as connection, connection.cursor() as cursor:
                cursor.execute(f'DROP SCHEMA "{self.schema}" CASCADE')

    def test_session_message_attachment_restart_and_owner_isolation(self) -> None:
        project = self.store.create_project("Imports", owner_id="owner-a")
        session = self.store.create_session(title="Arrival", project_id=project["project_id"], owner_id="owner-a")
        self.store.append_message(session["session_id"], "user", "Shipment AC-42 arrives Monday",
                                  trace_id="trace-1", attachments=[{"filename": "arrival.pdf"}], owner_id="owner-a")
        self.store.append_message(session["session_id"], "assistant", "Noted.", owner_id="owner-a")

        from db.postgres_session_store import PostgresSessionRepository
        restarted = PostgresSessionRepository(POSTGRES_URL, schema=self.schema)
        restored = restarted.get_session(session["session_id"], owner_id="owner-a")
        messages = restarted.get_messages(session["session_id"], owner_id="owner-a")

        self.assertEqual(restored["project_id"], project["project_id"])
        self.assertEqual([message["content"] for message in messages], ["Shipment AC-42 arrives Monday", "Noted."])
        self.assertEqual(messages[0]["trace_id"], "trace-1")
        self.assertEqual(messages[0]["attachments"], [{"filename": "arrival.pdf"}])
        self.assertIsNone(restarted.get_session(session["session_id"], owner_id="owner-b"))
        with self.assertRaises(KeyError):
            restarted.get_messages(session["session_id"], owner_id="owner-b")

    def test_migrations_are_idempotent_and_project_membership_is_scoped(self) -> None:
        from db.postgres_session_store import PostgresSessionRepository
        restarted = PostgresSessionRepository(POSTGRES_URL, schema=self.schema)
        project = restarted.create_project("Operations", owner_id="owner-a")
        self.assertEqual(restarted.create_project(" operations ", owner_id="owner-a")["project_id"], project["project_id"])
        self.assertIsNone(restarted.get_project(project["project_id"], owner_id="owner-b"))
        with self.assertRaises(KeyError):
            restarted.create_session(project_id=project["project_id"], owner_id="owner-b")


if __name__ == "__main__":
    unittest.main()
