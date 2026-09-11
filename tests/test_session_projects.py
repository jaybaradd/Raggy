"""Durable session and project assignment behaviour."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from db.session_store import SessionStore


class SessionProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "sessions.sqlite3")
        self.store = SessionStore(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_new_session_keeps_its_project_and_can_be_moved(self) -> None:
        session = self.store.create_session(project_scope="logistics")

        self.assertEqual(session["project_scope"], "logistics")

        moved = self.store.update_project_scope(session["session_id"], "operations")
        self.assertEqual(moved["project_scope"], "operations")
        self.assertEqual(self.store.get_session(session["session_id"])["project_scope"], "operations")

    def test_messages_survive_a_repository_restart_in_turn_order(self) -> None:
        session = self.store.create_session(title="Durable chat", project_scope="imports")
        self.store.append_message(session["session_id"], "user", "First message", trace_id="trace-1")
        self.store.append_message(session["session_id"], "assistant", "First answer")
        self.store.append_message(
            session["session_id"], "user", "Second message",
            attachments=[{"type": "file", "filename": "invoice.pdf"}],
        )

        restarted_store = SessionStore(self.db_path)
        restored = restarted_store.get_session(session["session_id"])
        messages = restarted_store.get_messages(session["session_id"])

        self.assertIsNotNone(restored)
        self.assertEqual(restored["project_scope"], "imports")
        self.assertEqual([message["content"] for message in messages], [
            "First message", "First answer", "Second message",
        ])
        self.assertEqual(messages[0]["trace_id"], "trace-1")
        self.assertEqual(messages[2]["attachments"][0]["filename"], "invoice.pdf")

    def test_owner_cannot_read_another_owners_session(self) -> None:
        session = self.store.create_session(owner_id="owner-a")
        self.store.append_message(session["session_id"], "user", "Private", owner_id="owner-a")

        self.assertIsNone(self.store.get_session(session["session_id"], owner_id="owner-b"))
        with self.assertRaises(KeyError):
            self.store.get_messages(session["session_id"], owner_id="owner-b")


if __name__ == "__main__":
    unittest.main()
