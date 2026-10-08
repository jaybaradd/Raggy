"""Exact, scope-safe identifier candidate lookup for memory reconciliation."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.memory.models import EventMemory, IdentifierReference, MemoryRecord
from core.storage.memory_store import MemoryStore


class MemoryIdentifierCandidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(str(Path(self.temp.name) / "memory.sqlite3"))

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _event(*, memory_id: str, project_id: str | None, project_scope: str = "imports",
               references: list[dict] | None = None, entities: list[str] | None = None,
               status: str = "active", valid_to=None) -> MemoryRecord:
        return MemoryRecord(
            memory_id=memory_id, owner_id="owner-a", scope="project", project_id=project_id,
            project_scope=project_scope, kind="event", status=status, user_confirmed=status == "active",
            valid_to=valid_to,
            payload=EventMemory(
                event_type="operational update", summary=f"Record {memory_id}",
                entities=entities or [], identifier_references=references or [],
            ),
        )

    @staticmethod
    def _query_reference() -> list[IdentifierReference]:
        return [IdentifierReference(value="EK420", mention="order EK420")]

    def test_exact_project_identifier_matches_cross_chat_and_legacy_fallback(self) -> None:
        explicit = self._event(
            memory_id="explicit", project_id="imports-id",
            references=[{"value": "EK-420", "mention": "order EK-420", "confidence": 0.98}],
        )
        legacy = self._event(memory_id="legacy", project_id=None, entities=["order EK420"])
        same_name_other_project = self._event(memory_id="other", project_id="other-imports-id")
        expired = self._event(
            memory_id="expired", project_id="imports-id",
            references=[{"value": "EK420"}],
            valid_to=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        for record in (explicit, legacy, same_name_other_project, expired):
            self.store.upsert(record)

        candidates = self.store.find_memory_candidates(
            owner_id="owner-a", session_id="new-chat", project_id="imports-id", project_scope="imports",
            identifier_references=self._query_reference(),
        )

        self.assertEqual([record.memory_id for record in candidates], ["explicit", "legacy"])

    def test_identifier_index_is_replaced_atomically_on_upsert(self) -> None:
        record = self._event(memory_id="mutable", project_id="imports-id", references=[{"value": "EK420"}])
        self.store.upsert(record)
        record.payload.identifier_references = [IdentifierReference(value="PO-99")]
        self.store.upsert(record, event_type="edited")

        old = self.store.find_memory_candidates(
            owner_id="owner-a", session_id="chat", project_id="imports-id", project_scope="imports",
            identifier_references=self._query_reference(),
        )
        new = self.store.find_memory_candidates(
            owner_id="owner-a", session_id="chat", project_id="imports-id", project_scope="imports",
            identifier_references=[IdentifierReference(value="PO99")],
        )
        self.assertEqual(old, [])
        self.assertEqual([item.memory_id for item in new], ["mutable"])

    def test_identifier_scheme_is_part_of_an_exact_match(self) -> None:
        record = self._event(
            memory_id="invoice", project_id="imports-id",
            references=[{"scheme": "invoice_number", "value": "EK420"}],
        )
        self.store.upsert(record)

        candidates = self.store.find_memory_candidates(
            owner_id="owner-a", session_id="chat", project_id="imports-id", project_scope="imports",
            identifier_references=[IdentifierReference(scheme="external_reference", value="EK420")],
        )
        self.assertEqual(candidates, [])


if __name__ == "__main__":
    unittest.main()
