"""Generalist event-memory contract and capture-policy coverage."""
from __future__ import annotations

import unittest

from core.memory.extractor import MemoryCandidate
from core.memory.models import EventMemory
from core.memory.policy import decide_project_capture


class EventContractTests(unittest.TestCase):
    def test_event_label_is_normalized_without_domain_aliases(self) -> None:
        event = EventMemory(event_type="Shipment", summary="A shipment arrives")
        self.assertEqual(event.event_type, "shipment")
        self.assertEqual(event.event_type_raw, "Shipment")

    def test_event_retains_normalized_identifier_references_and_claims(self) -> None:
        event = EventMemory(
            event_type="delivery delay", summary="EK-420 has a revised arrival",
            identifier_references=[{
                "scheme": "external_reference", "value": "EK-420", "mention": "order EK-420",
                "confidence": 0.98,
            }],
            claims=[{"attribute": "expected arrival", "value": "Thursday", "confidence": 0.95}],
        )
        self.assertEqual(event.event_type, "delivery_delay")
        self.assertEqual(event.identifier_references[0].normalized_value, "ek420")
        self.assertEqual(event.claims[0].normalized_value, "thursday")

    def test_high_confidence_project_event_is_not_gated_by_a_closed_taxonomy(self) -> None:
        candidate = MemoryCandidate(
            kind="event", confidence=0.9,
            payload={"event_type": "release coordination", "summary": "Release window moved", "entities": []},
            evidence_refs=[],
        )
        decision = decide_project_capture(
            candidate=candidate, project_scope="platform", user_content="The release window moved."
        )
        self.assertEqual(decision.scope, "project")
        self.assertEqual(decision.status, "active")


if __name__ == "__main__":
    unittest.main()
