"""Uvicorn entry point used only by the opt-in Postgres API integration test."""
from __future__ import annotations

from typing import AsyncIterator

from main import app


class DeterministicTestClient:
    """No-network provider that makes the HTTP persistence test reproducible."""

    async def chat_stream(self, messages: list[dict], system_prompt: str | None = None) -> AsyncIterator[str]:
        del system_prompt
        yield "Test reply: "
        yield messages[-1]["content"]

    async def generate_json(self, prompt: str, schema: dict | None = None) -> dict:
        del prompt, schema
        return {"candidates": []}


# The router resolves this module variable when each request streams, so the
# replacement happens before the server accepts its first request.
from api.routers import messages as message_router
message_router.llm_client = DeterministicTestClient()
