"""
core/llm/gemini.py — Gemini implementation of LLMProvider.

Uses google-genai's async streaming API.  The provider is constructed once and
shared across requests (stateless — no per-request state is kept here).
"""

import json
from typing import AsyncIterator

import google.genai as genai
import google.genai.types as genai_types

from config import settings
from core.llm.base import LLMProvider


class GeminiProvider(LLMProvider):
    """
    Streams responses from Google Gemini via the google-genai SDK.

    The client is initialised once at construction time.  Use the module-level
    ``gemini_provider`` singleton rather than instantiating this directly.
    """

    def __init__(self) -> None:
        self._client = genai.Client(api_key=settings.gemini_api_key)
        self._model = settings.gemini_chat_model

    async def chat_stream(
        self,
        messages: list[dict],
        system_prompt: str | None = None,
    ) -> AsyncIterator[str]:
        """
        Translate the generic messages list into Gemini's Content format and
        stream the response back token by token.

        Gemini expects:
          - system_instruction: str  (separate, not a message)
          - contents: list[Content]  (user/model turns only)
        """
        contents: list[genai_types.Content] = []

        for msg in messages:
            role = "user" if msg["role"] == "user" else "model"
            contents.append(
                genai_types.Content(
                    role=role,
                    parts=[genai_types.Part(text=msg["content"])],
                )
            )

        config = genai_types.GenerateContentConfig(
            system_instruction=system_prompt,
        )

        # generate_content_stream is the async streaming iterator
        async for chunk in await self._client.aio.models.generate_content_stream(
            model=self._model,
            contents=contents,
            config=config,
        ):
            if chunk.text:
                yield chunk.text

    async def generate_json(self, prompt: str, schema: dict | None = None) -> dict:
        """Run one non-streaming structured generation request."""
        # Gemini Developer API does not support every JSON Schema keyword
        # emitted by Pydantic (notably ``additionalProperties``). Keep the
        # response constrained to JSON here and validate it with Pydantic in
        # the extraction layer instead of sending the incompatible schema.
        config = genai_types.GenerateContentConfig(response_mime_type="application/json")
        response = await self._client.aio.models.generate_content(
            model=self._model,
            contents=prompt,
            config=config,
        )
        if getattr(response, "parsed", None) is not None:
            return response.parsed
        return json.loads(response.text or "{}")


# Module-level singleton — import and use this object everywhere.
gemini_provider = GeminiProvider()
