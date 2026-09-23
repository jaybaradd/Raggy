"""Regression coverage for cross-chat project-memory eligibility."""
from __future__ import annotations

import unittest

from pydantic import ValidationError

from api.schemas import MemoryPromotionRequest
from core.memory.models import MemoryRecord, PreferenceMemory
from core.retrieval.memory_scope import applicable_memory_scopes, is_memory_record_eligible


class MemoryScopeRetrievalTests(unittest.TestCase):
    def test_project_memory_is_eligible_from_a_different_chat_in_same_project(self) -> None:
        scopes = applicable_memory_scopes(
            owner_id="default", session_id="new-chat", project_id="imports-id",
            project_scope="imports",
        )

        self.assertEqual(scopes[0].visibility_scope, "session")
        self.assertEqual(scopes[0].constraint_field, "session_id")
        self.assertIn(
            ("project", "project_id", "imports-id"),
            [(item.visibility_scope, item.constraint_field, item.constraint_value) for item in scopes],
        )
        self.assertIn(
            ("project", "project_scope", "imports"),
            [(item.visibility_scope, item.constraint_field, item.constraint_value) for item in scopes],
        )

    def test_personal_chat_has_only_its_own_session_scope(self) -> None:
        scopes = applicable_memory_scopes(
            owner_id="default", session_id="personal-chat", project_id=None, project_scope=None,
        )

        self.assertEqual(
            [(item.visibility_scope, item.constraint_field, item.constraint_value) for item in scopes],
            [("session", "session_id", "personal-chat")],
        )

    def test_legacy_user_memory_is_never_eligible_for_context(self) -> None:
        legacy = MemoryRecord(
            owner_id="default", scope="user", kind="preference", status="active", user_confirmed=True,
            payload=PreferenceMemory(preferred_behavior="Use short answers"),
        )

        self.assertFalse(is_memory_record_eligible(
            legacy, owner_id="default", session_id="personal-chat", project_id=None, project_scope=None,
        ))

    def test_promotion_api_rejects_global_user_scope(self) -> None:
        with self.assertRaises(ValidationError):
            MemoryPromotionRequest(scope="user")


if __name__ == "__main__":
    unittest.main()
