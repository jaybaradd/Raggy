"""Short chat titles derived from the first user message."""

from __future__ import annotations

import re
import logging
from typing import Any


DEFAULT_CHAT_TITLE = "New chat"
_MAX_TITLE_WORDS = 7
_MAX_TITLE_CHARS = 48
logger = logging.getLogger(__name__)


def title_from_first_message(content: str) -> str:
    """Return a compact, single-line title without another model call."""
    clean = re.sub(r"\s+", " ", content).strip()
    if not clean:
        return DEFAULT_CHAT_TITLE

    words = clean.split(" ")
    title = " ".join(words[:_MAX_TITLE_WORDS])
    truncated = len(words) > _MAX_TITLE_WORDS
    if len(title) > _MAX_TITLE_CHARS:
        title = title[:_MAX_TITLE_CHARS].rstrip()
        if " " in title:
            title = title.rsplit(" ", 1)[0]
        truncated = True
    elif len(clean) > len(title):
        truncated = True
    return f"{title}…" if truncated else title


async def generate_chat_title(provider: Any, content: str) -> str:
    """Ask the configured provider for one bounded title, with a local fallback."""
    prompt = f"""Create a concise title for a conversation from its first user message.

Requirements:
- Return one JSON object with exactly one string field named `title`.
- Use two to seven words and no more than 48 characters.
- Preserve the message's language.
- Describe its main subject or intent.
- Do not use quotation marks, labels, commentary, or terminal punctuation.

First user message:
{content}
"""
    try:
        result = await provider.generate_json(prompt)
        value = result.get("title")
        if not isinstance(value, str):
            raise ValueError("title response is missing a string title")
        title = _clean_generated_title(value)
        if not title:
            raise ValueError("title response is empty")
        return title
    except Exception as exc:
        logger.warning("Chat title generation failed; using deterministic fallback: %s", exc)
        return title_from_first_message(content)


def _clean_generated_title(value: str) -> str:
    title = re.sub(r"\s+", " ", value).strip().strip("'\"`#*- ")
    title = title.rstrip(".!?:;, ")
    if not title:
        return ""
    # Enforce the boundary even when a provider ignores the prompt.
    title = " ".join(title.split(" ")[:_MAX_TITLE_WORDS])
    if len(title) > _MAX_TITLE_CHARS:
        title = title[:_MAX_TITLE_CHARS].rstrip()
        if " " in title:
            title = title.rsplit(" ", 1)[0]
    return title
