"""Session project assignment behaviour."""

from __future__ import annotations

import unittest

from db.session_store import SessionStore


class SessionProjectTests(unittest.TestCase):
    def test_new_session_keeps_its_project_and_can_be_moved(self) -> None:
        store = SessionStore()
        session = store.create_session(project_scope="logistics")

        self.assertEqual(session["project_scope"], "logistics")

        moved = store.update_project_scope(session["session_id"], "operations")
        self.assertEqual(moved["project_scope"], "operations")
        self.assertEqual(store.get_session(session["session_id"])["project_scope"], "operations")


if __name__ == "__main__":
    unittest.main()
