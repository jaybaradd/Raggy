"""Provider contract for Raggy's streamed chat and structured LLM calls.

Gemini is currently the production implementation.  Extraction,
reconciliation, and memory-planning code depend on this contract, while the
chat route still selects the configured Gemini singleton directly.  A provider
registry will move that final selection behind this contract in Phase 3.
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
        """Generate structured JSON for extraction or bounded context planning."""
        raise NotImplementedError("This provider does not implement structured JSON generation")
