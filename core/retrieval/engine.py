"""
core/retrieval/engine.py — Phase 0 naive RAG retrieval pipeline.

Pipeline (Phase 0):
  1. Embed the user query with the active EmbeddingBackend.
  2. Dense vector search in Qdrant (top-k chunks).
  3. Build a context string from the retrieved chunks.
  4. Return the context and the raw chunk metadata (for future observability panel).

Phase 1 will insert: hybrid BM25+dense fusion, reranking.
Phase 3 will insert: query decomposition.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from config import settings
from core.embeddings import embedder
from core.storage.qdrant_store import qdrant_store

logger = logging.getLogger(__name__)


@dataclass
class RetrievalResult:
    """
    Output of one retrieval pass.

    Attributes
    ----------
    context     : Pre-formatted string ready to be stuffed into the LLM prompt.
    chunks      : Raw retrieved chunk payloads (content, score, page, etc.)
                  — used by the observability panel in Phase 5.
    """

    context: str
    chunks: list[dict]


def retrieve(
    query: str,
    top_k: int | None = None,
    doc_id_filter: str | None = None,
) -> RetrievalResult:
    """
    Run the Phase 0 retrieval pipeline for *query*.

    Parameters
    ----------
    query         : The user's question.
    top_k         : Override the default top-k from settings.
    doc_id_filter : Restrict search to a specific document (optional).

    Returns
    -------
    RetrievalResult with the formatted context string and raw chunk list.
    """
    k = top_k or settings.retrieval_top_k

    # Step 1: Embed the query
    query_vector = embedder.encode_query(query)

    # Step 2: Dense search
    chunks = qdrant_store.search(
        query_vector=query_vector,
        top_k=k,
        doc_id_filter=doc_id_filter,
    )

    if not chunks:
        logger.debug("No chunks found for query: %s", query[:80])
        return RetrievalResult(context="", chunks=[])

    # Step 3: Format context
    # Each chunk is labelled with its source page for citation clarity.
    context_parts: list[str] = []
    for i, chunk in enumerate(chunks, start=1):
        page_label = f" (page {chunk['page']})" if chunk.get("page") else ""
        context_parts.append(
            f"[Source {i}{page_label}]\n{chunk['content']}"
        )

    context = "\n\n".join(context_parts)

    logger.debug("Retrieved %d chunks for query: %s", len(chunks), query[:80])
    return RetrievalResult(context=context, chunks=chunks)


def build_rag_prompt(query: str, context: str) -> str:
    """
    Combine the retrieved context and the user query into a single prompt string.

    The system prompt is returned separately in GeminiProvider.chat_stream;
    this function builds the *user turn* content only.

    Keeping this as a standalone function makes it easy to unit-test and easy
    to modify the prompt template without touching the retrieval logic.
    """
    if not context:
        # No relevant documents — fall back to general knowledge.
        return query

    return (
        "Use the following retrieved sources to answer the question. "
        "If the answer is not contained in the sources, say so honestly.\n\n"
        f"{context}\n\n"
        f"Question: {query}"
    )


RAG_SYSTEM_PROMPT = (
    "You are Raggy, a helpful AI assistant with access to a knowledge base. "
    "When answering questions based on retrieved source material, be thorough and "
    "include all relevant facts, items, and details present in the sources. "
    "Cite [Source N] labels accurately. If sources are incomplete, acknowledge that concisely."
)
