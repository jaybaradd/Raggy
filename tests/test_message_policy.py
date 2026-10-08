"""Message-level retrieval controls should default to context-safe behavior."""

from __future__ import annotations

import unittest

from api.schemas import SendMessageRequest


class MessagePolicyTests(unittest.TestCase):
    def test_knowledge_base_search_is_opt_in(self) -> None:
        self.assertFalse(SendMessageRequest(content="general question").use_knowledge_base)
        self.assertTrue(SendMessageRequest(content="document question", use_knowledge_base=True).use_knowledge_base)


if __name__ == "__main__":
    unittest.main()
