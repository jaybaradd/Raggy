"""
core/ingestion/chunker.py — token-based recursive chunker.

Strategy (from the Roadmap §1):
  - Target: ~512 tokens per chunk, ~10% overlap (50 tokens).
  - Split priority: paragraph → sentence → word boundary (never mid-word).
  - Never split mid-table / mid-code-block (Docling already hands us discrete
    items, so each item is chunked independently here).
  - context_prefix is NOT added here; that is Phase 1's contextual-retrieval step.

We use tiktoken for token counting because it is fast, deterministic, and uses
the same BPE vocabulary as OpenAI/Gemini embedding models (close enough for
budget purposes).  sentence-transformers' tokenizer is slower for this task.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

import tiktoken

from config import settings
from core.ingestion.parser import ParsedChunk, SourceLocator


# Use the cl100k_base encoding (GPT-4 / text-embedding-3) as a reasonable
# proxy for token counts.  The exact count varies per model but the budget
# difference is small enough not to matter for chunking purposes.
_ENCODING = tiktoken.get_encoding("cl100k_base")

# Paragraph separator pattern
_PARA_SEP = re.compile(r"\n{2,}")

# Sentence boundary (naive but good enough for chunking — not NLP)
_SENT_SEP = re.compile(r"(?<=[.!?])\s+")


def _token_count(text: str) -> int:
    return len(_ENCODING.encode(text))


def _split_into_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in _PARA_SEP.split(text) if p.strip()]


def _split_into_sentences(paragraph: str) -> list[str]:
    return [s.strip() for s in _SENT_SEP.split(paragraph) if s.strip()]


def _chunk_text(
    text: str,
    max_tokens: int,
    overlap_tokens: int,
) -> list[str]:
    """
    Recursively split *text* into chunks that each fit within *max_tokens*.

    Algorithm
    ---------
    1. Try paragraph boundaries first.
    2. If any paragraph still exceeds max_tokens, fall back to sentence boundaries.
    3. If a single sentence exceeds max_tokens, split by whitespace (word boundary).

    Overlap is implemented as a trailing carry-over: after each chunk is
    emitted, the last *overlap_tokens* worth of text is prepended to the next
    accumulation window.
    """
    paragraphs = _split_into_paragraphs(text)
    sentences: list[str] = []

    for para in paragraphs:
        if _token_count(para) <= max_tokens:
            sentences.append(para)
        else:
            sentences.extend(_split_into_sentences(para))

    # Now bin sentences into chunks with overlap
    chunks: list[str] = []
    current_parts: list[str] = []
    current_tokens = 0
    carry: str = ""  # overlap carry-over from previous chunk

    for sent in sentences:
        sent_tokens = _token_count(sent)

        # If a single sentence exceeds budget, hard-split by words
        if sent_tokens > max_tokens:
            words = sent.split()
            for word in words:
                wt = _token_count(word + " ")
                if current_tokens + wt > max_tokens and current_parts:
                    chunk_text = (carry + " " + " ".join(current_parts)).strip()
                    chunks.append(chunk_text)
                    # Build overlap carry from end of this chunk
                    carry = _build_carry(chunk_text, overlap_tokens)
                    current_parts = []
                    current_tokens = _token_count(carry)
                current_parts.append(word)
                current_tokens += wt
            continue

        if current_tokens + sent_tokens > max_tokens and current_parts:
            chunk_text = (carry + " " + " ".join(current_parts)).strip()
            chunks.append(chunk_text)
            carry = _build_carry(chunk_text, overlap_tokens)
            current_parts = []
            current_tokens = _token_count(carry)

        current_parts.append(sent)
        current_tokens += sent_tokens

    if current_parts:
        chunk_text = (carry + " " + " ".join(current_parts)).strip()
        chunks.append(chunk_text)

    return chunks


def _build_carry(text: str, overlap_tokens: int) -> str:
    """
    Return the last *overlap_tokens* tokens of *text* as a carry-over string.
    """
    tokens = _ENCODING.encode(text)
    if len(tokens) <= overlap_tokens:
        return text
    return _ENCODING.decode(tokens[-overlap_tokens:])


def chunk_parsed_chunks(raw_chunks: list[ParsedChunk]) -> list[ParsedChunk]:
    """
    Take a list of ParsedChunks (as produced by a Parser) and apply
    token-based chunking to each one, returning a flat list of smaller chunks.

    The source_locator and all metadata are inherited from the parent chunk.
    chunk_id is regenerated as a deterministic UUID for each sub-chunk.
    """
    max_tokens = settings.chunk_size_tokens
    overlap_tokens = settings.chunk_overlap_tokens
    result: list[ParsedChunk] = []

    for parent in raw_chunks:
        sub_texts = _chunk_text(parent.content, max_tokens, overlap_tokens)

        if not sub_texts:
            continue

        # If the content already fits in one chunk, keep it as-is
        if len(sub_texts) == 1 and _token_count(sub_texts[0]) <= max_tokens:
            result.append(parent)
            continue

        for i, text in enumerate(sub_texts):
            child_id = str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"{parent.chunk_id}:{i}")
            )
            result.append(
                ParsedChunk(
                    chunk_id=child_id,
                    doc_id=parent.doc_id,
                    modality=parent.modality,
                    content=text,
                    context_prefix=None,   # Phase 1
                    source_locator=parent.source_locator,
                    raw_file_uri=parent.raw_file_uri,
                    source_name=parent.source_name,
                    parser_backend=parent.parser_backend,
                    embedding_model=parent.embedding_model,
                    evidence_id=child_id,
                    representation=parent.representation,
                    created_at=datetime.now(timezone.utc),
                )
            )

    return result
