"""Unit tests for Raggy's narrow LiteLLM boundary."""

from __future__ import annotations

from types import SimpleNamespace
import unittest

from core.llm.client import LiteLLMClient


class _Stream:
    def __init__(self, chunks: list[object]) -> None:
        self._chunks = chunks

    def __aiter__(self):
        self._iterator = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._iterator)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class LiteLLMClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_streams_content_and_prepends_system_prompt(self) -> None:
        received: dict = {}

        async def completion(**kwargs):
            received.update(kwargs)
            return _Stream([
                SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="Hello"))]),
                SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None))]),
                SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=" world"))]),
            ])

        client = LiteLLMClient(model="gemini/test", api_key="test-key", completion=completion)
        tokens = [token async for token in client.chat_stream(
            [{"role": "user", "content": "Hi"}], system_prompt="Be concise",
        )]

        self.assertEqual(tokens, ["Hello", " world"])
        self.assertTrue(received["stream"])
        self.assertEqual(received["model"], "gemini/test")
        self.assertEqual(received["messages"][0], {"role": "system", "content": "Be concise"})
        self.assertEqual(received["messages"][1], {"role": "user", "content": "Hi"})

    async def test_parses_json_object_response(self) -> None:
        received: dict = {}

        async def completion(**kwargs):
            received.update(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content='{"candidates": []}'),
            )])

        client = LiteLLMClient(model="gemini/test", completion=completion)

        self.assertEqual(await client.generate_json("extract"), {"candidates": []})
        self.assertEqual(received["response_format"], {"type": "json_object"})
        self.assertEqual(received["messages"], [{"role": "user", "content": "extract"}])

    async def test_rejects_non_object_json(self) -> None:
        async def completion(**kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="[]"))])

        client = LiteLLMClient(model="gemini/test", completion=completion)
        with self.assertRaisesRegex(ValueError, "must be an object"):
            await client.generate_json("extract")


if __name__ == "__main__":
    unittest.main()
