"""Chat-title generation remains short, safe, and failure tolerant."""

from __future__ import annotations

import asyncio
import unittest

from core.conversation.titles import generate_chat_title, title_from_first_message


class _Provider:
    def __init__(self, response):
        self.response = response
        self.prompt = ""

    async def generate_json(self, prompt: str):
        self.prompt = prompt
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class ChatTitleTests(unittest.TestCase):
    def test_provider_title_is_cleaned_and_bounded(self) -> None:
        provider = _Provider({"title": '"A compact generated conversation title."'})

        title = asyncio.run(generate_chat_title(provider, "A first message with enough context"))

        self.assertEqual(title, "A compact generated conversation title")
        self.assertLessEqual(len(title), 48)
        self.assertIn("First user message:", provider.prompt)

    def test_provider_failure_uses_short_local_fallback(self) -> None:
        content = "One two three four five six seven eight nine ten"

        title = asyncio.run(generate_chat_title(_Provider(RuntimeError("offline")), content))

        self.assertEqual(title, title_from_first_message(content))
        self.assertLessEqual(len(title), 49)  # 48 characters plus an ellipsis.


if __name__ == "__main__":
    unittest.main()
