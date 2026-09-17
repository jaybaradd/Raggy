"""Small in-process LiteLLM client used by chat and memory services."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from config import settings

Completion = Callable[..., Awaitable[Any]]


class LiteLLMClient:
    """Expose Raggy's two LLM operations through LiteLLM without routing policy."""

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        completion: Completion | None = None,
    ) -> None:
        self._model = model or settings.litellm_model or f"gemini/{settings.gemini_chat_model}"
        self._api_key = api_key if api_key is not None else settings.gemini_api_key
        self._completion = completion

    async def chat_stream(
        self,
        messages: list[dict[str, str]],
        system_prompt: str | None = None,
    ) -> AsyncIterator[str]:
        response = await self._request(
            messages=self._with_system_prompt(messages, system_prompt),
            stream=True,
        )
        async for chunk in response:
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            content = getattr(getattr(choices[0], "delta", None), "content", None)
            if content:
                yield content

    async def generate_json(self, prompt: str, schema: dict | None = None) -> dict[str, Any]:
        """Generate JSON and leave schema validation to the calling Pydantic model."""
        del schema  # Gemini's API does not accept every Pydantic JSON Schema keyword.
        response = await self._request(
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        choices = getattr(response, "choices", None) or []
        if not choices:
            raise ValueError("LiteLLM returned no choices for JSON generation")
        content = getattr(getattr(choices[0], "message", None), "content", None)
        if not isinstance(content, str) or not content.strip():
            raise ValueError("LiteLLM returned an empty JSON response")
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            raise ValueError("LiteLLM JSON response must be an object")
        return parsed

    async def _request(self, **kwargs: Any) -> Any:
        completion = self._completion or self._load_completion()
        return await completion(model=self._model, api_key=self._api_key, **kwargs)

    @staticmethod
    def _with_system_prompt(
        messages: list[dict[str, str]], system_prompt: str | None,
    ) -> list[dict[str, str]]:
        if not system_prompt:
            return messages
        return [{"role": "system", "content": system_prompt}, *messages]

    @staticmethod
    def _load_completion() -> Completion:
        from litellm import acompletion

        return acompletion


llm_client = LiteLLMClient()
