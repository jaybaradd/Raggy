"""
core/llm/base.py — abstract interface every LLM provider must implement.

Phase 3 will add OpenAI, Anthropic, Groq providers behind this same interface.
The rest of the application only ever touches this ABC — never a concrete class.
"""

from abc import ABC, abstractmethod
from typing import AsyncIterator


class LLMProvider(ABC):
    """
    Common interface for all LLM backends.

    Methods
    -------
    chat_stream(messages)
        Yields text tokens as they arrive from the model.
    """

    @abstractmethod
    async def chat_stream(
        self,
        messages: list[dict],
        system_prompt: str | None = None,
    ) -> AsyncIterator[str]:
        """
        Stream a response token by token.

        Parameters
        ----------
        messages:
            List of {"role": "user"|"assistant", "content": str} dicts in
            chronological order.
        system_prompt:
            Optional system instruction prepended before the conversation.

        Yields
        ------
        str
            Individual text tokens/chunks from the model.
        """
        ...

    async def generate_json(self, prompt: str, schema: dict | None = None) -> dict:
        """Generate a schema-conforming JSON object for background extraction."""
        raise NotImplementedError("This provider does not implement structured JSON generation")
