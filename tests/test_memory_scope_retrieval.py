"""Regression coverage for cross-chat project-memory eligibility."""
from __future__ import annotations

import unittest

from core.retrieval.memory_scope import applicable_memory_scopes


class MemoryScopeRetrievalTests(unittest.TestCase):
    def test_project_memory_is_eligible_from_a_different_chat_in_same_project(self) -> None:
        scopes = applicable_memory_scopes(
            owner_id="default", session_id="new-chat", project_id="imports-id",
            project_scope="imports",
        )

        self.assertEqual(scopes[0].visibility_scope, "session")
        self.assertEqual(scopes[0].constraint_field, "session_id")
        self.assertEqual(scopes[1].visibility_scope, "user")
        self.assertIn(
            ("project", "project_id", "imports-id"),
            [(item.visibility_scope, item.constraint_field, item.constraint_value) for item in scopes],
        )
        self.assertIn(
            ("project", "project_scope", "imports"),
            [(item.visibility_scope, item.constraint_field, item.constraint_value) for item in scopes],
        )


if __name__ == "__main__":
    unittest.main()
